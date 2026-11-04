"""把「一份课件」匹配到「B 站上一门用它讲课的课程」。

核心洞察（2026-11-04 实测得出）：多 P 网课的分 P 标题常常就是教材章节编号，
例如 ``3.1.1栈的定义 / 3.1.2 栈的顺序存储 / 3.1.3 栈的链式存储``。
这等于 B 站免费送了一份结构化大纲，用它和课件的章节序列做层级前缀对齐，
是零成本、不下载视频、不依赖 OCR 的第一层匹配。

三层证据，逐层才加成本：

======  ==========================================  ==========
层级    判据                                        成本
======  ==========================================  ==========
L1      分 P 标题 ↔ 课件章节序列（层级前缀对齐）      0
L2      字幕 ↔ 课件正文（有字幕才走）                 极低
L3      画面关键帧 ↔ 课件页（需多模态模型，默认关）    API
======  ==========================================  ==========

边界：B 站不下发课件文件，所以**无法证明两个文件 100% 相同**。
本模块只给「内容对齐」的强推断，置信度低于阈值时标记 ``need_confirm``，
把判断权交回用户。
"""

from __future__ import annotations

from .bilibili import BiliVideo, chapter_id, chapter_prefixes, search_terms
from .slide_fingerprint import DeckFingerprint, lcs_length, normalize


def _has(haystack: str, needle: str) -> bool:
    plain = normalize(haystack)
    target = normalize(needle)
    return bool(target) and target in plain


def scan_deck(deck: DeckFingerprint, course_name_hint: str = "") -> dict:
    """读出课件身份：课程名、章节序列、术语、检索词。"""
    chapters = [dict(item) for item in deck.chapter_seq]
    course = (course_name_hint or "").strip() or _guess_course_name(deck, chapters)
    names = []
    for item in chapters:
        title = str(item.get("title") or "").strip()
        if title and title != course and title not in names:
            names.append(title)
    return {
        "course": course,
        "pages": len(deck.pages),
        "chapters": chapters,
        "glossary": list(deck.glossary),
        "signature": deck.signature,
        "keywords": search_terms(course, names, list(deck.glossary)),
    }


def _guess_course_name(deck: DeckFingerprint, chapters: list[dict]) -> str:
    """单份课件常常只有章节名没有课程名，退而求其次用首章标题。"""
    if chapters:
        return str(chapters[0].get("title") or "").strip()
    if deck.pages:
        first = deck.pages[0].text.splitlines()
        if first:
            return first[0].strip()[:24]
    return deck.filename.rsplit(".", 1)[0][:24]


def score_course(video: BiliVideo, deck: DeckFingerprint, course_name: str = "") -> dict:
    """L1 匹配：分 P 标题的章节体系、顺序、术语，以及规模合理性。"""
    chapters = [dict(item) for item in deck.chapter_seq]
    deck_prefixes: set[str] = set()
    for item in chapters:
        deck_prefixes.update(chapter_prefixes(str(item.get("id") or "")))
    deck_ids = [str(item.get("id") or "") for item in chapters]
    terms = list(deck.glossary)[:30]

    part_ids = [chapter_id(page.part) for page in video.pages]
    informative = [pid for pid in part_ids if pid]
    info_ratio = len(informative) / len(video.pages) if video.pages else 0.0

    # 双向覆盖：只算「分 P 命中率」会把正确课程压下去。
    # 实测算《数字集成电路基础》时，课件只有前 3 章、B 站是 51 讲全书，
    # 分 P 命中率 24%，看着像不匹配；真正该问的是「课件章节被覆盖了多少」。
    hits = 0
    for pid in informative:
        if any(prefix in deck_prefixes for prefix in chapter_prefixes(pid)):
            hits += 1
    part_cover = hits / len(informative) if informative else 0.0

    deck_id_set = {str(item.get("id") or "") for item in chapters if item.get("id")}
    hit_deck = 0
    for did in deck_id_set:
        wanted = set(chapter_prefixes(did))
        if any(wanted & set(chapter_prefixes(pid)) for pid in informative):
            hit_deck += 1
    deck_cover = hit_deck / len(deck_id_set) if deck_id_set else 0.0
    # 课件覆盖优先：学生的课件往往只是全书的一部分
    chapter_cover = 0.7 * deck_cover + 0.3 * part_cover

    if len(informative) > 1 and deck_ids:
        order_ratio = lcs_length(deck_ids, informative) / max(1, min(len(deck_ids), len(informative)))
    else:
        order_ratio = 0.0

    all_parts = " ".join(page.part for page in video.pages)
    term_hits = [term for term in terms if _has(all_parts, term)]
    term_cover = min(1.0, len(term_hits) / min(6, len(terms))) if terms else 0.0

    title_match = 1.0 if course_name and _has(video.title, course_name) else 0.0
    count = len(video.pages)
    volume_ok = 1.0 if 3 <= count <= 120 else (0.2 if count == 1 else 0.5)

    score = (
        0.36 * chapter_cover
        + 0.18 * order_ratio
        + 0.18 * term_cover
        + 0.12 * title_match
        + 0.10 * volume_ok
        + 0.12 * info_ratio
    )

    reasons: list[str] = []
    if deck_id_set:
        reasons.append(f"课件章节被覆盖 {round(deck_cover * 100)}%")
    if chapter_cover > 0:
        reasons.append(f"章节体系综合重合 {round(chapter_cover * 100)}%（分 P 命中 {round(part_cover * 100)}%）")
    if order_ratio > 0.3:
        reasons.append(f"章节顺序一致 {round(order_ratio * 100)}%")
    if term_hits:
        reasons.append("分 P 标题命中术语 " + " / ".join(term_hits[:4]))
    if title_match:
        reasons.append("视频标题含课程名")
    if not informative:
        reasons.append("分 P 标题没有章节编号，无法做零成本对齐，需要人工确认")
    reasons.append(f"共 {count} 讲，其中 {len(informative)} 讲有章节编号")

    return {
        "score": round(score, 3),
        "chapter_cover": round(chapter_cover, 3),
        "deck_cover": round(deck_cover, 3),
        "part_cover": round(part_cover, 3),
        "order_ratio": round(order_ratio, 3),
        "term_hits": term_hits[:6],
        "informative_parts": len(informative),
        "reasons": reasons,
        "need_confirm": score < 0.55 or not informative,
    }


def map_parts(video: BiliVideo, deck: DeckFingerprint) -> list[dict]:
    """每个分 P → 对应课件页。

    先按章节编号的层级前缀找（3.1.1 → 3.1 → 3），找不到再用页正文的词面匹配，
    都不行就按顺序均分并标低置信。**允许多个分 P 指向同一页**——一页讲不透时本来就该多讲几遍。
    """
    if not deck.pages or not video.pages:
        return []

    by_prefix: dict[str, list[int]] = {}
    for page in deck.pages:
        for chapter in page.chapters:
            for prefix in chapter_prefixes(str(chapter.get("id") or "")):
                by_prefix.setdefault(prefix, [])
                if page.index not in by_prefix[prefix]:
                    by_prefix[prefix].append(page.index)

    used: set[int] = set()
    mapped: list[dict] = []

    for index, part in enumerate(video.pages, start=1):
        pid = chapter_id(part.part)
        pages: list[int] = []
        how = ""
        confidence = 0.0

        if pid:
            for prefix in reversed(chapter_prefixes(pid)):
                candidates = by_prefix.get(prefix) or []
                if candidates:
                    pages = list(candidates)
                    how = f"章节编号 {pid} ↔ 课件 {prefix}"
                    confidence = 0.9 if prefix == pid else (0.8 if len(prefix.split(".")) >= 2 else 0.6)
                    break

        if not pages:
            query = set(normalize(part.part)[i : i + 2] for i in range(max(1, len(normalize(part.part)) - 1)))
            best = (0.0, [])
            for page in deck.pages:
                plain = normalize(page.text)
                overlap = sum(1 for token in query if token and token in plain)
                ratio = overlap / max(1, len(query))
                if ratio > best[0]:
                    best = (ratio, [page.index])
            if best[1] and best[0] >= 0.3:
                pages = best[1]
                how = "页正文词面匹配"
                confidence = 0.55

        if not pages:
            per = max(1, -(-len(deck.pages) // len(video.pages)))
            pages = [p.index for p in deck.pages[(index - 1) * per : index * per]]
            how = "按顺序均分（低置信）"
            confidence = 0.3

        used.update(pages)
        labels = [p.label for p in deck.pages if p.index in pages]
        mapped.append({
            "part": part.page,
            "cid": part.cid,
            "title": part.part,
            "duration": part.duration,
            "chapter_id": pid,
            "pages": pages,
            "labels": labels,
            "how": how,
            "confidence": confidence,
            "evidence": [f"video:{video.bvid}:P{part.page}"] + [f"doc:p{p}" for p in pages],
        })

    return mapped


def find_courses(
    deck: DeckFingerprint,
    client,
    *,
    course_name: str = "",
    limit: int = 8,
    per_keyword: int = 18,
    inspect: int = 14,
) -> dict:
    """端到端：课件身份 → 候选课程（含逐讲映射与置信度）。"""
    identity = scan_deck(deck, course_name)
    terms = identity["keywords"]
    if not terms:
        raise ValueError("课件里没抽出可用的课程名或术语，无法构造检索词")

    merged: dict[str, dict] = {}
    keyword_log: list[dict] = []
    for keyword in terms:
        try:
            items = client.search(keyword, limit=per_keyword)
            keyword_log.append({"keyword": keyword, "hits": len(items)})
            for item in items:
                merged.setdefault(item["bvid"], item)
        except Exception as exc:  # noqa: BLE001 - 单组关键词失败不该中断整轮
            keyword_log.append({"keyword": keyword, "error": str(exc)})

    if not merged:
        raise ValueError("B 站没返回结果。可能被限流，或检索词太窄；可以手动贴 BV 号")

    scored_hits = [(hit, _rough_score(hit, identity)) for hit in merged.values()]
    # 排序必须确定：分数相同时按播放量与 bvid 兜底，
    # 否则同一份课件两次检索会给出不同第一名，用户没法信任结果。
    scored_hits.sort(key=lambda pair: (-pair[1], -int(pair[0].get("play") or 0), pair[0]["bvid"]))

    # 标题里直接带课程名的候选强制进精排：粗排只看标题与简介，
    # 长标题（「【复旦大学 叶凡】数字集成电路设计原理」）容易被挤出前 N。
    forced = [pair for pair in scored_hits if identity["course"] and _has(pair[0].get("title", ""), identity["course"])]
    rough = scored_hits[:inspect]
    for pair in forced:
        if pair not in rough:
            rough.append(pair)
    rough = rough[: max(inspect, 14)]

    candidates: list[dict] = []
    for hit, rough_score in rough:
        try:
            video = client.view(hit["bvid"])
            scored = score_course(video, deck, identity["course"])
            candidates.append({
                "bvid": video.bvid,
                "title": video.title,
                "owner": video.owner,
                "cover": video.cover,
                "videos": len(video.pages),
                "parts": [
                    {"part": p.page, "cid": p.cid, "title": p.part, "duration": p.duration}
                    for p in video.pages
                ],
                "rough_score": round(rough_score, 3),
                **scored,
                "mapping": map_parts(video, deck),
            })
        except Exception as exc:  # noqa: BLE001 - 单个视频拿不到不该整轮失败
            candidates.append({
                "bvid": hit["bvid"], "title": hit["title"], "owner": hit.get("author", ""),
                "videos": 0, "parts": [], "score": rough_score, "rough_score": round(rough_score, 3),
                "reasons": [f"拿不到分 P 列表：{exc}"], "need_confirm": True, "mapping": [],
            })

    candidates.sort(key=lambda item: (-item["score"], item["bvid"]))
    top = candidates[:limit]

    # 两门课都是 100% 覆盖时，0.01 的分差说明不了谁更对。
    # 这种时候要给用户「有歧义」的提示，而不是假装排出了唯一答案。
    ambiguous = bool(len(top) >= 2 and abs(top[0]["score"] - top[1]["score"]) < 0.05)

    return {
        "identity": identity,
        "keywords": keyword_log,
        "ambiguous": ambiguous,
        "candidates": top,
    }


def _rough_score(hit: dict, identity: dict) -> float:
    """还没拿分 P 时的粗排：只看标题/简介里命中了多少。"""
    text = f"{hit.get('title', '')} {hit.get('description', '')}"
    score = 0.0
    if identity["course"] and _has(text, identity["course"]):
        score += 0.32
    hits = sum(1 for term in identity["glossary"][:20] if _has(text, term))
    if hits:
        score += min(0.28, hits * 0.07)
    if any(_has(text, str(item.get("title") or "")) for item in identity["chapters"][:6] if item.get("title")):
        score += 0.18
    if hit.get("play", 0) > 50000:
        score += 0.06
    return min(1.0, score)
