"""「PPT 反查整门课」的 HTTP 接口。

挂在知页现有的 FastAPI 应用上，不改动它的鉴权、会话与检索约定。
所有端点都以 ``/courses/{course_id}/finder/…`` 开头，因此前端可以按课程隔离。

设计上保持一件事：**接口只描述事实，不下结论**。
置信度、需要人工确认的标记、每一讲的映射依据都由服务层给出，界面照原样展示。
"""

from __future__ import annotations

import base64
import hashlib
import json
from threading import Lock

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .bili_auth import BiliAuth, get_auth
from .bilibili import BilibiliClient, BilibiliError
from .course_finder import find_courses, map_parts, scan_deck, score_course
from .models import AppMeta, Block, Chapter, Course, Document, new_id
from .slide_fingerprint import build_deck, compare_decks
from .finder_digest import collect_lines, digest_part
from .finder_visual import check_frames
from .transcript_ingest import build_blocks, document_name, frames_to_lines

_client: BilibiliClient | None = None
_client_lock = Lock()
_deck_cache: dict[str, tuple[int, object]] = {}


class FinderSearchInput(BaseModel):
    course_name: str = Field(default="", max_length=60)
    document_id: str | None = None
    limit: int = Field(default=8, ge=1, le=20)


class FinderInspectInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    document_id: str | None = None


class FinderCompareInput(BaseModel):
    document_id: str | None = None
    other_document_id: str = Field(min_length=1)


class FinderTranscriptInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    cid: int = Field(gt=0)


class FinderImportTranscriptInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    cid: int = Field(gt=0)
    part: int = Field(ge=1)
    part_title: str = ""
    chapter_id: str | None = None
    # 浏览器扩展在页面上下文带着登录态抓到的字幕可以直接传进来，
    # 不必让服务端再抓一次（免登录抓不到 AI 字幕是常态）。
    lines: list[dict] | None = None


class FinderDigestInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    part: int = Field(ge=1)
    part_title: str = ""


class FinderVisualInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    part: int = Field(ge=1)
    frames: list[dict] = Field(default_factory=list)


class FinderOcrInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    part: int = Field(ge=1)
    part_title: str = ""
    chapter_id: str | None = None
    frames: list[dict] = Field(default_factory=list)


class FinderAdoptInput(BaseModel):
    bvid: str = Field(min_length=6, max_length=20)
    title: str = ""
    owner: str = ""
    score: float = 0.0
    need_confirm: bool = True
    mapping: list[dict] = Field(default_factory=list)


def _require(db, model, identity):
    found = db.get(model, identity)
    if found is None:
        raise HTTPException(404, "未找到该记录")
    return found


_client_cookie = ""
_settings = None
_auth: BiliAuth | None = None


def _auth_or_raise() -> BiliAuth:
    global _auth
    if _auth is None:
        if _settings is None:
            raise HTTPException(500, "服务尚未初始化完成")
        _auth = get_auth(_settings.data_dir)
    return _auth


def _client_or_raise() -> BilibiliClient:
    """带登录态就带，没登录就匿名——不替任何人登录。"""
    global _client, _client_cookie
    cookie = ""
    try:
        cookie = _auth_or_raise().cookie()
    except HTTPException:
        cookie = ""
    with _client_lock:
        if _client is None or cookie != _client_cookie:
            if _client is not None:
                _client.close()
            _client = BilibiliClient(cookie=cookie)
            _client_cookie = cookie
    return _client


def close_finder_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _blocks_of(db, document_id: str):
    return list(db.scalars(select(Block).where(Block.document_id == document_id).order_by(Block.order, Block.id)))


def _deck_for(db, course_id: str, document_id: str | None = None):
    """挑一份「文字最多的课件」来建指纹，而不是盲取第一份。

    一门课里常有课件 + 笔记 + 往年卷，课程反查要用课件；
    笔记和试卷的章节结构不可靠，拿它们做指纹会给出错误的课程匹配。
    """
    _require(db, Course, course_id)
    query = select(Document).where(
        Document.course_id == course_id,
        Document.status == "ready",
        Document.kind.notin_(["generated", "practice", "note"]),
    )
    documents = [doc for doc in db.scalars(query) if not document_id or doc.id == document_id]
    if not documents:
        raise HTTPException(400, "这门课还没有解析完成的课件，先上传一份 PPT 或 PDF")

    best = None
    best_score = -1
    for doc in documents:
        blocks = _blocks_of(db, doc.id)
        if not blocks:
            continue
        cached = _deck_cache.get(doc.id)
        if cached and cached[0] == len(blocks):
            deck = cached[1]
        else:
            deck = build_deck(blocks, doc.filename, doc.id)
            _deck_cache[doc.id] = (len(blocks), deck)
        if not deck.pages:
            continue
        score = len(deck.pages) + sum(1 for page in deck.pages if page.chapters)
        if score > best_score:
            best, best_score = deck, score
    if best is None:
        raise HTTPException(400, "课件里没有可提取的文字（可能是纯图片 PPT），先开启 OCR 或换一份带文字的课件")
    return best


def _match_chapter(db, course_id: str, part_title: str) -> str | None:
    """把一讲挂到已有章节上：取「章节标题出现在分 P 标题里」的最长匹配。

    不做模糊匹配——挂错章节比不挂更糟，检索时会把学生带到错的章节范围。
    挂不上就留空，靠向量检索自己找。
    """
    text = str(part_title or "").strip()
    if not text:
        return None
    best, best_length = None, 0
    for chapter in db.scalars(select(Chapter).where(Chapter.course_id == course_id)):
        title = str(chapter.title or "").strip()
        if len(title) < 2 or title not in text:
            continue
        if len(title) > best_length:
            best, best_length = chapter.id, len(title)
    return best


def _ingest_lines(services, course_id: str, *, bvid: str, part: int, part_title: str,
                  lines: list[dict], chapter_id: str | None, source: str) -> dict:
    """把一段时间轴文字入库成知页资料。字幕与画面 OCR 共用这一条。"""
    # OCR 出来的每一行本身就是「一屏」，不要按 45 秒窗口再合并，
    # 否则两屏不同内容会被塞进同一个块，引用也定位不到具体哪一屏。
    window = 1.0 if source == "OCR" else 45.0
    blocks = build_blocks(lines, bvid=bvid, cid=0, part=part,
                          part_title=part_title, chapter_id=chapter_id,
                          window_seconds=window)
    if not blocks:
        raise HTTPException(422, "解析后没有可用内容")

    document_id = new_id()
    name = document_name(part, part_title, bvid)
    if source != "transcript":
        name = f"{name}·{source}"
    sidecar = services.upload_dir / (document_id + ".transcript.json")
    sidecar.write_text(json.dumps({
        "bvid": bvid, "part": part, "part_title": part_title,
        "source": source, "lines": lines,
    }, ensure_ascii=False), encoding="utf-8")

    # 同一讲同一来源重复导入要幂等：先清掉旧的那份，而不是每点一次多一条
    file_hash = hashlib.sha1(f"{bvid}:{part}:{source}".encode()).hexdigest()
    with services.sessions() as db:
        stale = db.scalar(select(Document).where(Document.course_id == course_id,
                                                Document.file_hash == file_hash))
        stale_id = stale.id if stale is not None else None
    if stale_id:
        try:
            services.remove_document(stale_id)
        except Exception:  # noqa: BLE001 - 删不掉就继续，用新记录覆盖
            pass

    with services.sessions() as db:
        db.add(Document(
            id=document_id, course_id=course_id, filename=name, kind="transcript",
            file_hash=file_hash,
            path=str(sidecar), status="queued", warnings=[],
            units_total=len(blocks), units_with_text=len(blocks),
        ))
        db.commit()

    prepared = [{
        "order": block.order, "text": block.text, "locator": block.locator,
        "heading": block.heading, "chapter_id": chapter_id,
    } for block in blocks]
    try:
        stats = services.ingest_transcript(document_id, prepared)
    except Exception as exc:  # noqa: BLE001 - 把失败原因如实带回界面
        raise HTTPException(500, f"入库失败：{exc}") from exc
    return {"document_id": document_id, "filename": name, "status": "ready",
            "chapter_id": chapter_id, "reused": False, **stats}


def register_finder_routes(app, services) -> None:
    global _settings
    _settings = services.settings
    @app.get("/courses/{course_id}/finder/scan")
    def finder_scan(course_id: str, document_id: str | None = None):
        with services.sessions() as db:
            deck = _deck_for(db, course_id, document_id)
            identity = scan_deck(deck)
            return {
                "identity": identity,
                "deck": {
                    "document_id": deck.document_id,
                    "filename": deck.filename,
                    "pages": len(deck.pages),
                    "signature": deck.signature,
                },
                "preview": [
                    {"page": page.index, "label": page.label, "lines": len(page.text.splitlines()),
                     "chapters": list(page.chapters)}
                    for page in deck.pages[:12]
                ],
            }

    @app.post("/courses/{course_id}/finder/search")
    def finder_search(course_id: str, payload: FinderSearchInput):
        with services.sessions() as db:
            deck = _deck_for(db, course_id, payload.document_id)
            try:
                return find_courses(deck, _client_or_raise(), course_name=payload.course_name, limit=payload.limit)
            except BilibiliError as exc:
                raise HTTPException(502, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.post("/courses/{course_id}/finder/inspect")
    def finder_inspect(course_id: str, payload: FinderInspectInput):
        with services.sessions() as db:
            deck = _deck_for(db, course_id, payload.document_id)
            try:
                video = _client_or_raise().view(payload.bvid.strip())
            except BilibiliError as exc:
                raise HTTPException(502, str(exc)) from exc
            scored = score_course(video, deck, scan_deck(deck)["course"])
            return {
                "video": {
                    "bvid": video.bvid, "title": video.title, "owner": video.owner,
                    "cover": video.cover, "videos": len(video.pages),
                    "parts": [{"part": p.page, "cid": p.cid, "title": p.part, "duration": p.duration} for p in video.pages],
                },
                **scored,
                "mapping": map_parts(video, deck),
            }

    @app.post("/courses/{course_id}/finder/compare")
    def finder_compare(course_id: str, payload: FinderCompareInput):
        with services.sessions() as db:
            left = _deck_for(db, course_id, payload.document_id)
            right_doc = _require(db, Document, payload.other_document_id)
            blocks = _blocks_of(db, right_doc.id)
            if not blocks:
                raise HTTPException(400, "另一份课件还没有解析出文字")
            right = build_deck(blocks, right_doc.filename, right_doc.id)
            result = compare_decks(left, right)
            return {
                "left": {"filename": left.filename, "pages": len(left.pages)},
                "right": {"filename": right.filename, "pages": len(right.pages)},
                **result,
                "note": "只有两份文件都在本地且内容哈希一致时才能说「文件级完全一样」；B 站不下发课件文件，线上只能给内容级结论。",
            }

    @app.post("/courses/{course_id}/finder/transcript")
    def finder_transcript(course_id: str, payload: FinderTranscriptInput):
        _ = course_id
        try:
            return _client_or_raise().transcript(payload.bvid.strip(), payload.cid)
        except BilibiliError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/courses/{course_id}/finder/import-transcript", status_code=201)
    def finder_import_transcript(course_id: str, payload: FinderImportTranscriptInput):
        """把一讲字幕做成可检索资料。

        字幕块与课件块在检索、引用回查上完全同权：引用里会显示
        「第 3 讲 03:24–04:10」这样的位置，而不是一句没有出处的转述。
        """
        bvid = payload.bvid.strip()
        file_hash = hashlib.sha1(f"{bvid}:{payload.cid}".encode("utf-8")).hexdigest()

        chapter_id = None
        with services.sessions() as db:
            _require(db, Course, course_id)
            chapter_id = payload.chapter_id or _match_chapter(db, course_id, payload.part_title)
            existing = db.scalar(select(Document).where(
                Document.course_id == course_id, Document.file_hash == file_hash))
            if existing is not None and existing.status == "ready":
                return {
                    "document_id": existing.id, "filename": existing.filename, "status": "ready",
                    "reused": True, "blocks": existing.units_total, "chapter_id": chapter_id,
                }
            stale_id = existing.id if existing is not None else None

        if stale_id:
            try:
                services.remove_document(stale_id)
            except Exception:  # noqa: BLE001 - 旧记录删不掉时继续，用新 id 覆盖
                pass

        provided = [line for line in (payload.lines or []) if str(line.get("text") or "").strip()]
        if provided:
            lines = [{
                "from": float(line.get("from") or 0),
                "to": float(line.get("to") or line.get("from") or 0),
                "text": str(line.get("text") or "").strip(),
            } for line in provided]
            diagnosis = {"via": "extension"}
        else:
            try:
                fetched = _client_or_raise().transcript(bvid, int(payload.cid))
            except BilibiliError as exc:
                raise HTTPException(502, str(exc)) from exc
            lines = fetched.get("lines") or []
            diagnosis = fetched.get("diagnosis") or {}
        if not lines:
            raise HTTPException(422, {
                "message": "这一讲没有可用字幕。AI 字幕需要浏览器已登录 B 站；"
                           "桌面端免登录取不到是常态，可以用浏览器扩展在页面上下文抓取后，"
                           "把字幕随请求体一起传进来（lines 字段）。",
                "diagnosis": diagnosis,
            })

        blocks = build_blocks(
            lines, bvid=bvid, cid=int(payload.cid), part=payload.part,
            part_title=payload.part_title, chapter_id=chapter_id,
        )
        if not blocks:
            raise HTTPException(422, "字幕解析后没有可用内容")

        document_id = new_id()
        name = document_name(payload.part, payload.part_title, bvid)
        sidecar = services.upload_dir / (document_id + ".transcript.json")
        sidecar.write_text(json.dumps({
            "bvid": bvid, "cid": payload.cid, "part": payload.part,
            "title": payload.part_title, "lines": lines,
        }, ensure_ascii=False), encoding="utf-8")

        with services.sessions() as db:
            db.add(Document(
                id=document_id, course_id=course_id, filename=name, kind="transcript",
                file_hash=file_hash, path=str(sidecar), status="queued", warnings=[],
                units_total=len(blocks), units_with_text=len(blocks),
            ))
            db.commit()

        prepared = [{
            "order": block.order, "text": block.text, "locator": block.locator,
            "heading": block.heading, "chapter_id": chapter_id,
        } for block in blocks]
        try:
            stats = services.ingest_transcript(document_id, prepared)
        except Exception as exc:  # noqa: BLE001 - 把失败原因如实带回界面
            raise HTTPException(500, f"字幕入库失败：{exc}") from exc

        return {
            "document_id": document_id, "filename": name, "status": "ready",
            "chapter_id": chapter_id, "reused": False, **stats,
        }

    @app.post("/courses/{course_id}/finder/digest-part")
    def finder_digest(course_id: str, payload: FinderDigestInput):
        """把一讲字幕压成带时间戳的要点。

        每条要点的 ``at`` 都会和字幕的真实时间戳逐个核对，对不上就丢掉——
        模型编一个「第 12 分钟讲到」是最容易发生的错误，也最伤信任。
        """
        with services.sessions() as db:
            _require(db, Course, course_id)
            document = db.scalar(select(Document).where(
                Document.course_id == course_id,
                Document.kind == "transcript",
                Document.filename.contains(f"P{payload.part} "),
                Document.filename.contains(payload.bvid.strip()),
            ))
            if document is None:
                raise HTTPException(404, "这一讲还没有导入字幕。先在逐讲表里点「导入字幕」。")
            blocks = list(db.scalars(select(Block).where(
                Block.document_id == document.id).order_by(Block.order)))

        lines = collect_lines(blocks)
        try:
            result = digest_part(lines, part=payload.part,
                                 part_title=payload.part_title or document.filename,
                                 llm=services.llm)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        key = f"finder_digest:{course_id}:{payload.bvid.strip()}:{payload.part}"
        with services.sessions() as db:
            marker = db.get(AppMeta, key)
            value = json.dumps(result, ensure_ascii=False)
            if marker is None:
                db.add(AppMeta(key=key, value=value))
            else:
                marker.value = value
            db.commit()
        return result

    @app.get("/courses/{course_id}/finder/digest")
    def finder_digest_read(course_id: str, bvid: str, part: int):
        with services.sessions() as db:
            _require(db, Course, course_id)
            marker = db.get(AppMeta, f"finder_digest:{course_id}:{bvid.strip()}:{part}")
            if marker is None:
                raise HTTPException(404, "这一讲还没有生成要点")
            return json.loads(marker.value)

    @app.post("/courses/{course_id}/finder/visual-check")
    def finder_visual_check(course_id: str, payload: FinderVisualInput):
        """画面确认（L3）：读视频帧里的课件文字，再和课件页文本对齐。

        没有配置多模态模型时返回 degraded，并说明前两层仍然有效——
        不把「没做」说成「做过」。
        """
        with services.sessions() as db:
            deck = _deck_for(db, course_id)
        return check_frames(payload.frames, deck, services.llm, part=payload.part)

    @app.post("/courses/{course_id}/finder/ocr-frames", status_code=201)
    def finder_ocr_frames(course_id: str, payload: FinderOcrInput):
        """画面转文字：不需要 B 站登录，也不需要模型 Key。

        这条是「没有字幕」的兜底，也是唯一一条**完全本地**的取证方式：
        扩展抽帧 → 这里用知页自带的 rapidocr 识别 → 拼回时间轴 → 当资料入库。
        """
        with services.sessions() as db:
            _require(db, Course, course_id)
            chapter_id = payload.chapter_id or _match_chapter(db, course_id, payload.part_title)

        from io import BytesIO

        from PIL import Image

        frames = [item for item in payload.frames if str(item.get("dataUrl") or "").startswith("data:image")][:8]
        if not frames:
            raise HTTPException(422, "没有收到可用画面（data URL 为空）")

        recognized, failures = [], 0
        for item in frames:
            data_url = str(item["dataUrl"])
            try:
                encoded = data_url.split(",", 1)[1]
                image = Image.open(BytesIO(base64.b64decode(encoded)))
                result = services.visual.recognize(image, context=payload.part_title or "")
                lines = list(getattr(result, "lines", []) or [])
                recognized.append({"time": str(item.get("time") or ""), "lines": lines})
            except Exception:  # noqa: BLE001 - 单帧失败不该中断整轮
                failures += 1

        lines = frames_to_lines(recognized)
        if not lines:
            raise HTTPException(422, {
                "message": "这些画面里没识别出文字。可能是纯板书/人物镜头，或画面太小。",
                "frames": len(frames), "failures": failures,
            })
        result = _ingest_lines(services, course_id, bvid=payload.bvid.strip(), part=payload.part,
                               part_title=payload.part_title, lines=lines,
                               chapter_id=chapter_id, source="OCR")
        return {**result, "frames": len(frames), "failures": failures}

    # ── B 站扫码登录（官方 passport 流程） ──
    # 用户在自己手机上确认，我们只展示二维码与接收回调；不接触密码。

    @app.post("/bilibili/login/qrcode")
    def bilibili_login_qrcode():
        try:
            return _auth_or_raise().start(_client_or_raise()._client)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/bilibili/login/poll")
    def bilibili_login_poll(key: str):
        auth = _auth_or_raise()
        client = _client_or_raise()
        try:
            state = auth.poll(client._client, key)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        if state.state == "ok":
            # 凭证已落盘，下次请求会自动换上带 Cookie 的客户端
            client.cookie = auth.cookie()
            global _client_cookie
            _client_cookie = ""
        return {"state": state.state, "message": state.message, "uname": state.uname, "mid": state.mid}

    @app.get("/bilibili/login/status")
    def bilibili_login_status():
        payload = _auth_or_raise().status()
        payload["mode"] = "cookie" if payload["loggedIn"] else "anonymous"
        return payload

    @app.post("/bilibili/logout")
    def bilibili_logout():
        _auth_or_raise().clear()
        global _client_cookie
        _client_cookie = "__logged_out__"
        return {"ok": True, "message": "已退出，本地凭证已删除"}

    @app.get("/courses/{course_id}/finder/sign")
    def finder_sign(course_id: str, bvid: str, cid: int):
        """给浏览器扩展用的 WBI 签名。

        扩展里不必再塞一份 MD5 实现，签名统一由后端出。
        """
        _ = course_id
        try:
            return {"query": _client_or_raise().sign({"bvid": bvid, "cid": cid})}
        except BilibiliError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/courses/{course_id}/finder/adopt", status_code=201)
    def finder_adopt(course_id: str, payload: FinderAdoptInput):
        with services.sessions() as db:
            _require(db, Course, course_id)
            key = f"finder:{course_id}"
            marker = db.get(AppMeta, key)
            record = {
                "bvid": payload.bvid.strip(),
                "title": payload.title,
                "owner": payload.owner,
                "score": payload.score,
                "need_confirm": payload.need_confirm,
                "parts": payload.mapping,
                "adopted_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }
            if marker is None:
                db.add(AppMeta(key=key, value=__import__("json").dumps(record, ensure_ascii=False)))
            else:
                marker.value = __import__("json").dumps(record, ensure_ascii=False)
            db.commit()
            return record

    @app.get("/courses/{course_id}/finder/adopted")
    def finder_adopted(course_id: str):
        import json

        with services.sessions() as db:
            _require(db, Course, course_id)
            marker = db.get(AppMeta, f"finder:{course_id}")
            return json.loads(marker.value) if marker else None
