"""L3 视觉确认：视频画面里到底是不是这套课件。

前两层（分 P 标题对齐、字幕对齐）都是文本证据，能证明「讲的内容像」，
但证明不了「屏幕上放的就是同一套 PPT」。这一层补上画面证据。

做法刻意选简单的一边：**不让模型比对两张图**，而是让它把画面里的字读出来，
再拿这段文字和课件页做字符 3-gram 相似度。
理由是比对两图要求同时具备课件页图片（知页只有 PDF 才有预览图，PPTX 没有），
而「读字再比文本」对两边都只要一份文本，且结果可解释、可复算。

没有配置多模态模型时直接返回 degraded，不假装做过视觉确认。
"""

from __future__ import annotations

import json
import re

from .slide_fingerprint import jaccard, shingles

FRAME_PROMPT = """你在读一张网课视频的截图。任务只有一件：把画面里**真实出现**的课件文字读出来。

规则：
1. 只输出你能看清的字，看不清就留空，不要猜、不要补全。
2. 忽略播放器控件、弹幕、字幕条、头像、页码、台标。
3. 如果画面里没有成段的课件文字（比如老师在镜头前），把 is_slide 设为 false。
4. 不要解释，不要总结。

输出 JSON：{"is_slide": true, "text": "画面里逐行的课件文字"}"""

MAX_FRAMES = 6
MAX_DATA_URL = 1_800_000  # 约 1.8MB，超过就不送模型


def page_texts(deck) -> list[dict]:
    return [{"page": page.index, "label": page.label, "text": page.text} for page in deck.pages]


def match_text(text: str, pages: list[dict], threshold: float = 0.25) -> dict | None:
    """把一段画面文字对到最像的一页课件。"""
    probe = shingles(text)
    if not probe:
        return None
    best = None
    for page in pages:
        score = jaccard(probe, shingles(page["text"]))
        if best is None or score > best["similarity"]:
            best = {"page": page["page"], "label": page["label"], "similarity": round(score, 3)}
    if best and best["similarity"] >= threshold:
        return best
    return None


def _parse_json(text: str) -> dict:
    body = str(text or "").strip().replace("```json", "```")
    body = re.sub(r"^```|```$", "", body).strip()
    try:
        return json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                pass
    return {"is_slide": False, "text": str(text or "").strip()[:500]}


def _read_frame(data_url: str, llm) -> dict:
    messages = [
        {"role": "system", "content": FRAME_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": "读取这张截图里的课件文字。"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]
    raw = llm.complete(messages, json_mode=True)
    return _parse_json(raw)


def check_frames(frames: list[dict], deck, llm=None, *, part: int = 0) -> dict:
    """逐帧读字 → 对页。返回每一帧的结论与整体置信。"""
    pages = page_texts(deck)
    usable = [
        {"time": str(item.get("time") or ""), "dataUrl": str(item.get("dataUrl") or "")}
        for item in (frames or [])
        if str(item.get("dataUrl") or "").startswith("data:image") and len(str(item.get("dataUrl"))) <= MAX_DATA_URL
    ][:MAX_FRAMES]

    base = {
        "part": part,
        "frames_total": len(frames or []),
        "frames_used": len(usable),
        "degraded": False,
        "pages": len(pages),
    }

    if not pages:
        return {**base, "degraded": True,
                "note": "没有可用的课件页文本，无法做视觉确认", "matches": [], "confidence": 0}
    if llm is None or not getattr(llm, "configured", False):
        return {**base, "degraded": True,
                "note": "没有配置多模态模型，这一层没有执行。前两层（章节对齐、字幕对齐）的结果仍然有效。",
                "matches": [], "confidence": 0}
    if not usable:
        return {**base, "degraded": True, "note": "没有收到可用的画面（data URL 为空或超过 1.8MB）",
                "matches": [], "confidence": 0}

    matches, failures = [], 0
    for frame in usable:
        try:
            payload = _read_frame(frame["dataUrl"], llm)
        except Exception:  # noqa: BLE001 - 单帧失败不该中断整轮
            failures += 1
            continue
        text = str(payload.get("text") or "").strip()
        if not payload.get("is_slide", True) or len(text) < 8:
            matches.append({"time": frame["time"], "page": None, "label": "", "similarity": 0,
                            "reason": "画面里没有成段课件文字"})
            continue
        hit = match_text(text, pages)
        if hit is None:
            matches.append({"time": frame["time"], "page": None, "label": "", "similarity": 0,
                            "reason": "读到的文字和任何一页都不像", "excerpt": text[:60]})
            continue
        matches.append({"time": frame["time"], **hit, "excerpt": text[:60]})

    hits = [item for item in matches if item.get("page")]
    confidence = round(sum(item["similarity"] for item in hits) / len(hits), 3) if hits else 0
    covered = sorted({item["page"] for item in hits})

    if hits and confidence >= 0.35:
        verdict = "画面确认：视频里放的就是这套课件"
    elif hits:
        verdict = "画面部分吻合，可能是改过的同一套课件"
    else:
        verdict = "画面没能对上任何一页，这一讲的视觉证据不成立"

    return {
        **base,
        "matches": matches,
        "failures": failures,
        "pages_hit": covered,
        "confidence": confidence,
        "verdict": verdict,
    }
