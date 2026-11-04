"""逐讲消化：把一讲字幕压成带时间戳的要点。

这是「看完一整门课」的最小单元。判据只有一条：
**每条要点必须带一个真实存在的时间戳**，程序逐个核对，对不上就丢掉。

为什么这么严：字幕是自动生成的，模型很容易编出「老师在第 12 分钟说……」。
时间戳可核对，就意味着学生点一下能跳回去看——否则这条要点没有价值。

没配模型时退化成机械摘要（按句式打分挑句子），并明确告诉界面这是降级结果。
"""

from __future__ import annotations

import json
import re

MAX_KEYPOINTS = 8
MAX_GOTCHAS = 4

DIGEST_PROMPT = """你是把网课一讲压成复习要点的编辑。

铁律：
1. 只依据我给的字幕。资料里没有的一个字都不许写，不许补充外部知识。
2. 每条要点必须带 at 字段，值必须是「时间戳」里出现过的一个数字（秒）。
3. 要点写成结论句，不要写成「老师讲了……」这种转述。
4. 资料不够就少写几条，不要凑数。
5. 术语保留「中文（English）」对照。

输出 JSON：
{"keypoints":[{"text":"要点","at":123}],
 "gotchas":[{"text":"易错点","at":123}],
 "terms":["中文（English）"]}

keypoints 最多 8 条，gotchas 最多 4 条。只输出 JSON。"""

_SIGNAL = re.compile(r"定义|特点|性质|区别|等于|必须|注意|易错|判据|公式|所以|因此|总结|一句话")
_STOP_LINE = re.compile(r"^(大家好|同学们好|我们来看|那么|好的|嗯|这一讲我们讲)")


def collect_lines(blocks) -> list[dict]:
    """把 Block 行还原成 {from,to,text}，按时间排序。"""
    lines = []
    for block in blocks:
        locator = getattr(block, "locator", None) or {}
        if "from" not in locator:
            continue
        text = str(getattr(block, "text", "") or "").strip()
        if not text:
            continue
        lines.append({
            "from": float(locator.get("from") or 0),
            "to": float(locator.get("to") or 0),
            "text": text,
        })
    lines.sort(key=lambda item: item["from"])
    return lines


def timeline(lines: list[dict], limit: int = 40) -> str:
    """给模型看的时间轴。每条前面是秒数，方便它原样引用。"""
    out = []
    for line in lines[:limit]:
        int_from = int(line["from"])
        out.append(f"[{int_from}] {line['text']}")
    return "\n".join(out)


def mechanical(lines: list[dict], limit: int = MAX_KEYPOINTS) -> list[dict]:
    """无模型时的降级：按句式与关键词挑句子，仍然带真实时间戳。"""
    scored = []
    for line in lines:
        text = line["text"].strip()
        if len(text) < 8 or _STOP_LINE.match(text):
            continue
        score = 0
        if _SIGNAL.search(text):
            score += 3
        if re.search(r"[：:]", text):
            score += 1
        if 12 <= len(text) <= 60:
            score += 1
        if score <= 0:
            continue
        scored.append((score, line))
    scored.sort(key=lambda item: (-item[0], item[1]["from"]))
    picked = sorted((line for _, line in scored[:limit]), key=lambda item: item["from"])
    return [{"text": line["text"][:80], "at": int(line["from"])} for line in picked]


def _valid_times(lines: list[dict]) -> set[int]:
    return {int(line["from"]) for line in lines}


def verify(items, allowed_times: set[int], limit: int) -> tuple[list[dict], int]:
    """逐条核对时间戳。返回保留的条目与被丢掉的条数。"""
    kept, dropped = [], 0
    for item in items or []:
        if len(kept) >= limit:
            break
        text = str(item.get("text") or "").strip()
        if not text or len(text) < 4:
            dropped += 1
            continue
        try:
            at = int(float(item.get("at")))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if at not in allowed_times:
            dropped += 1
            continue
        kept.append({"text": text[:120], "at": at})
    return kept, dropped


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
    raise ValueError("模型没有返回可解析的 JSON")


def digest_part(lines: list[dict], *, part: int, part_title: str, llm=None) -> dict:
    """一讲的消化结果。``llm`` 为 None 时走机械摘要。"""
    lines = [line for line in lines if line["text"].strip()]
    if not lines:
        raise ValueError("这一讲没有可用的字幕内容")

    base = {
        "part": part,
        "title": part_title,
        "from": int(lines[0]["from"]),
        "to": int(max(line["to"] for line in lines)),
        "line_count": len(lines),
        "degraded": False,
        "dropped": 0,
    }

    if llm is None or not getattr(llm, "configured", False):
        points = mechanical(lines)
        return {**base, "degraded": True, "keypoints": points, "gotchas": [], "terms": []}

    messages = [
        {"role": "system", "content": DIGEST_PROMPT},
        {"role": "user", "content": (
            f"# 第 {part} 讲：{part_title}\n\n"
            f"# 时间戳（秒数 与 句子的对应）\n{timeline(lines)}"
        )},
    ]
    raw = llm.complete(messages, json_mode=True)
    payload = _parse_json(raw)

    allowed = _valid_times(lines)
    keypoints, dropped_k = verify(payload.get("keypoints"), allowed, MAX_KEYPOINTS)
    gotchas, dropped_g = verify(payload.get("gotchas"), allowed, MAX_GOTCHAS)
    terms = [str(term).strip()[:40] for term in (payload.get("terms") or []) if str(term).strip()][:12]

    return {
        **base,
        "keypoints": keypoints,
        "gotchas": gotchas,
        "terms": terms,
        "dropped": dropped_k + dropped_g,
    }
