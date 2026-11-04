"""把一讲字幕折成知页能入库的 Block。

知页的 Block 需要三样东西：``text``、``locator``（供引用回查）、``heading``。
字幕天然带时间轴，所以 locator 里直接放 ``from/to`` 与可读标签
``第 3 讲 03:24–04:10``，这样生成回答时引用的就是「视频第几分几秒」，
点回原文可以定位到那一讲的那一段。

切块按「时长 45 秒 / 最多 8 行」两个条件里先满足的那个——太碎会让检索命中半句话，
太粗会让一个块跨十几个话题。这两个数是保守值，没有调参依据，先按语感定。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_WINDOW_SECONDS = 45.0
DEFAULT_MAX_LINES = 8


@dataclass(frozen=True)
class TranscriptBlock:
    order: int
    text: str
    locator: dict
    heading: str


def format_time(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    return f"{total // 60:02d}:{total % 60:02d}"


def document_name(part: int, part_title: str, bvid: str) -> str:
    """资料列表里显示的名字。

    带 B 站来源，学生一眼知道这不是课件。标题截到 16 字、不用全角括号——
    列表项宽度有限，长名字加全角标点会换行错位。
    """
    title = (part_title or "").strip() or f"第{part}讲"
    return f"B站 P{part} {title[:16]} {bvid}"


def build_blocks(
    lines: list[dict],
    *,
    bvid: str,
    cid: int,
    part: int,
    part_title: str = "",
    chapter_id: str | None = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[TranscriptBlock]:
    """把 ``[{from,to,text}]`` 折成块。空字幕返回空列表，不抛异常。"""
    cleaned: list[dict] = []
    for item in lines or []:
        text = str(item.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        cleaned.append({
            "from": float(item.get("from") or 0),
            "to": float(item.get("to") or item.get("from") or 0),
            "text": text,
        })
    if not cleaned:
        return []

    heading = f"第 {part} 讲 · {part_title}".strip(" ·") if part_title else f"第 {part} 讲"
    blocks: list[TranscriptBlock] = []
    bucket: list[dict] = []

    def flush() -> None:
        if not bucket:
            return
        start, end = bucket[0]["from"], max(item["to"] for item in bucket)
        label = f"第 {part} 讲 {format_time(start)}–{format_time(end)}"
        locator = {
            "bvid": bvid,
            "cid": cid,
            "part": part,
            "from": round(start, 2),
            "to": round(end, 2),
            "label": label,
        }
        if chapter_id:
            locator["chapter_id"] = chapter_id
        blocks.append(TranscriptBlock(
            order=len(blocks),
            text="".join(item["text"] if item["text"].endswith(("。", "？", "！", "；")) else item["text"] + "；"
                         for item in bucket),
            locator=locator,
            heading=heading,
        ))
        bucket.clear()

    for item in cleaned:
        bucket.append(item)
        span = item["to"] - bucket[0]["from"]
        if span >= window_seconds or len(bucket) >= max_lines:
            flush()
    flush()

    # 末尾只剩一两行时并回上一块，避免出现只有半句话的孤立块
    if len(blocks) >= 2 and len(blocks[-1].text) < 12:
        tail = blocks.pop()
        last = blocks[-1]
        merged_locator = dict(last.locator)
        merged_locator["to"] = tail.locator["to"]
        merged_locator["label"] = f"第 {part} 讲 {format_time(last.locator['from'])}–{format_time(tail.locator['to'])}"
        blocks[-1] = TranscriptBlock(order=last.order, text=last.text + tail.text,
                                     locator=merged_locator, heading=last.heading)

    return blocks


def parse_clock(value: str) -> float:
    """``03:24`` / ``1:02:05`` → 秒。解析不了就当 0，不抛异常。"""
    parts = str(value or "").strip().split(":")
    try:
        numbers = [int(part) for part in parts if part.strip() != ""]
    except ValueError:
        return 0.0
    if not numbers:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def frames_to_lines(recognized: list[dict], *, step: float = 6.0) -> list[dict]:
    """把「每帧 OCR 出来的文字」拼成时间轴。

    画面 OCR 没有字幕那样的精确起止时间，只能给「这一帧出现在 03:24」。
    所以每行的 ``to`` 取下一帧的时间，最后一帧补一个 ``step``；
    这样引用仍然能落到具体时间点，学生跳回去能看到那一屏。

    同一屏连续几帧读到一样的文字要合并——抽帧常常在同一页停好几秒。
    """
    ordered = sorted(
        ({"at": parse_clock(item.get("time")),
          "text": " ".join(t for t in (_line_text(x) for x in (item.get("lines") or [])) if t).strip(),
          "time": str(item.get("time") or "")}
         for item in recognized or []),
        key=lambda item: item["at"],
    )
    merged: list[dict] = []
    for item in ordered:
        text = item["text"]
        if len(text) < 4:
            continue
        if merged and _same_screen(merged[-1]["text"], text):
            continue
        merged.append({"from": item["at"], "to": 0.0, "text": text, "clock": item["time"]})
    for index, item in enumerate(merged):
        item["to"] = merged[index + 1]["from"] if index + 1 < len(merged) else item["from"] + step
    return [{"from": item["from"], "to": item["to"], "text": item["text"]} for item in merged]


def _line_text(item) -> str:
    """OCR 结果可能是字符串，也可能是 {text, confidence, bbox}，统一成文本。"""
    if isinstance(item, dict):
        return str(item.get("text") or item.get("value") or "").strip()
    return str(item or "").strip()


def _same_screen(left: str, right: str) -> bool:
    """两帧读到几乎一样的文字 → 视为同一屏。"""
    import difflib

    return difflib.SequenceMatcher(None, left[:200], right[:200]).ratio() > 0.85
