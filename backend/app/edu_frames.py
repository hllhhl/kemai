"""把 Video Note Helper 截到的画面接进课脉：存图、自动建课、生成带图笔记。

插件（D:\\try\\tabbit\\video-note-helper-master）的截帧机制：
    canvas.drawImage(video) → toDataURL(image/jpeg, 0.75)，最大宽 720；
    自动模式按时长均分 3~6 张，seek 后截；帧长这样 {time, dataUrl}；
    笔记里用 ![配图说明](frame_N.png) 引用，图片作为附件一起送。

课脉这边对应做三件事：
1. 收到 frames 后把 dataUrl 解码落成真实图片文件（不再只是 OCR 成文字）；
2. 没有课程时按标题自动建一门课——用户要的是「零课程起步」；
3. 生成一份带图 Markdown 并作为资料导入课程，之后 notes_build 能引用它。
"""

from __future__ import annotations

import base64
import json
import re
import time
from io import BytesIO
from pathlib import Path

from sqlalchemy import select

from .config import Settings
from .models import Course


def data_dir() -> Path:
    return Path(Settings.from_env().data_dir)


def ensure_course(services, name: str, course_id: str = "") -> tuple[str, bool]:
    """有课程就复用，没有就按名字建一门。返回 (course_id, 是否新建)。"""
    with services.lock, services.sessions() as db:
        if course_id:
            course = db.get(Course, course_id)
            if course is not None:
                return course.id, False
        existing = db.scalar(select(Course).where(Course.name == name))
        if existing is not None:
            return existing.id, False
        course = Course(name=name)
        db.add(course)
        db.commit()
        return course.id, True


def _decode(data_url: str) -> tuple[bytes, str]:
    """data:image/jpeg;base64,... → (bytes, 扩展名)。"""
    text = str(data_url or "")
    match = re.match(r"data:image/([a-zA-Z0-9+]+);base64,(.*)", text, re.DOTALL)
    if not match:
        raise ValueError("不是 data:image/...;base64,... 格式的画面")
    mime = match.group(1).lower()
    ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}.get(mime, "jpg")
    return base64.b64decode(match.group(2)), ext


def _safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "-", str(text or "未命名"))[:60].strip() or "未命名"


def import_frames(services, payload: dict) -> dict:
    """接收一批帧，存成图片，自动建课，生成并导入带图笔记。"""
    title = _safe_name(payload.get("title") or "视频图文笔记")
    bvid = str(payload.get("bvid") or "").strip()
    course_name = _safe_name(payload.get("course_name") or title)
    frames = [item for item in (payload.get("frames") or []) if isinstance(item, dict)][:24]
    if not frames:
        raise ValueError("没有收到画面（frames 为空）")

    course_id, created = ensure_course(services, course_name, str(payload.get("course_id") or ""))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = data_dir() / "notes-images" / course_id / f"{stamp}-{_safe_name(bvid or title)}"
    folder.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for index, frame in enumerate(frames, start=1):
        try:
            raw, ext = _decode(str(frame.get("dataUrl") or ""))
        except (ValueError, Exception) as exc:  # noqa: BLE001 - 单帧坏不拖垮整批
            saved.append({"index": index, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        # 统一转成一张不超过 1.2MB 的 jpg，和插件的压缩策略对齐
        try:
            from PIL import Image

            image = Image.open(BytesIO(raw)).convert("RGB")
            if image.width > 720:
                ratio = 720 / image.width
                image = image.resize((720, max(1, int(image.height * ratio))))
            path = folder / f"frame_{index}.jpg"
            image.save(path, "JPEG", quality=75)
        except Exception:  # noqa: BLE001 - PIL 不可用时就直接落原始字节
            path = folder / f"frame_{index}.{ext}"
            path.write_bytes(raw)
        saved.append({"index": index, "ok": True, "file": str(path),
                      "time": str(frame.get("time") or ""), "bytes": path.stat().st_size})

    good = [item for item in saved if item.get("ok")]
    if not good:
        raise ValueError("所有画面都解码失败")

    NL = chr(10)
    lines = [
        f"# {title} · 图文笔记",
        "",
        f"- 来源：{bvid or '浏览器插件'}",
        f"- 课程：{course_name}",
        f"- 画面：{len(good)} 张（自动按时间点截取，或手动插入当前帧）",
        "",
        "## 画面与要点",
        "",
    ]
    for item in good:
        lines += [
            f"### 画面 {item['index']}" + (f"（{item['time']}）" if item["time"] else ""),
            "",
            f"![画面 {item['index']}]({Path(item['file']).name})",
            "",
            "（此处由课脉结合字幕整理该画面涉及的知识点与要点。）",
            "",
        ]
    body = payload.get("body")
    if isinstance(body, str) and body.strip():
        lines += ["## 字幕/讲义整理", "", body.strip()[:20000], ""]
    markdown = NL.join(lines)
    note_path = folder / "图文笔记.md"
    note_path.write_text(markdown, encoding="utf-8")

    # 把带图笔记作为资料导入课程，笔记生成时检索得到它。
    try:
        from . import course_tools

        state = course_tools._import_text_to_course(course_id, title + " 图文笔记", markdown)
    except Exception as exc:  # noqa: BLE001 - 资料导入失败不影响图片已落盘
        state = f"资料导入失败：{type(exc).__name__}: {exc}"

    receipt = {
        "at": stamp, "course_id": course_id, "course_created": created,
        "title": title, "bvid": bvid, "frames": len(good),
        "folder": str(folder), "note": str(note_path), "state": state,
    }
    log = data_dir() / "frames-receipts.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False) + NL)

    return {
        "course_id": course_id, "course_created": created, "course_name": course_name,
        "frames": len(good), "folder": str(folder), "note_path": str(note_path),
        "files": [item["file"] for item in good], "state": state,
        "markdown": markdown[:3000],
    }


def recent_receipts(limit: int = 5) -> list[dict]:
    path = data_dir() / "frames-receipts.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
