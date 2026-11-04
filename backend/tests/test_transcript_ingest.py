"""字幕折块的单元测试（纯函数，不联网、不建库）。"""

import pytest

from app.transcript_ingest import build_blocks, document_name, format_time, frames_to_lines, parse_clock


def lines(*specs):
    """specs: (from, to, text)"""
    return [{"from": f, "to": t, "text": text} for f, t, text in specs]


def test_format_time():
    assert format_time(0) == "00:00"
    assert format_time(65) == "01:05"
    assert format_time(3725) == "62:05"


def test_document_name_carries_source():
    assert "P3" in document_name(3, "3.1.2 栈的顺序存储", "BV1pU4y157uN")
    assert "BV1pU4y157uN" in document_name(3, "", "BV1pU4y157uN")


def test_empty_lines_return_nothing():
    assert build_blocks([], bvid="BVx", cid=1, part=1) == []
    assert build_blocks([{"from": 0, "to": 1, "text": "  "}], bvid="BVx", cid=1, part=1) == []


def test_blocks_carry_video_locator():
    blocks = build_blocks(
        lines((0, 5, "栈是后进先出"), (5, 10, "只允许在栈顶操作")),
        bvid="BVtest", cid=42, part=3, part_title="3.1.1 栈的定义",
    )
    assert len(blocks) == 1
    locator = blocks[0].locator
    assert locator["bvid"] == "BVtest" and locator["cid"] == 42 and locator["part"] == 3
    assert locator["label"] == "第 3 讲 00:00–00:10"
    assert locator["from"] == 0 and locator["to"] == 10
    assert "第 3 讲" in blocks[0].heading


def test_split_by_window():
    # 每条 10 秒，窗口 45 秒 → 每块最多 5 条左右
    payload = lines(*[(i * 10, i * 10 + 10, f"第{i}句话内容") for i in range(20)])
    blocks = build_blocks(payload, bvid="BVx", cid=1, part=1, window_seconds=45)
    assert len(blocks) >= 4
    for block in blocks:
        span = block.locator["to"] - block.locator["from"]
        assert span <= 60, f"块跨度应该受窗口约束，实际 {span}"


def test_split_by_line_count():
    payload = lines(*[(i, i + 1, f"短句{i}") for i in range(30)])
    blocks = build_blocks(payload, bvid="BVx", cid=1, part=1, window_seconds=999, max_lines=8)
    assert len(blocks) >= 4
    assert all(block.text for block in blocks)


def test_chapter_id_is_recorded():
    blocks = build_blocks(lines((0, 5, "内容")), bvid="BVx", cid=1, part=1, chapter_id="ch-9")
    assert blocks[0].locator["chapter_id"] == "ch-9"


def test_text_is_readable_with_separators():
    blocks = build_blocks(
        lines((0, 3, "栈是后进先出"), (3, 6, "队列是先进先出")),
        bvid="BVx", cid=1, part=1,
    )
    text = blocks[0].text
    assert "栈是后进先出" in text and "队列是先进先出" in text
    assert text.count("；") >= 1


def test_parse_clock():
    assert parse_clock("03:24") == 204
    assert parse_clock("1:02:05") == 3725
    assert parse_clock("") == 0
    assert parse_clock("bad") == 0


def test_frames_to_lines_merges_same_screen():
    lines = frames_to_lines([
        {"time": "00:12", "lines": [{"text": "3.1.1 栈的定义"}, {"text": "栈是后进先出"}]},
        {"time": "00:18", "lines": [{"text": "3.1.1 栈的定义"}, {"text": "栈是后进先出"}]},
        {"time": "00:48", "lines": ["3.1.2 栈的顺序存储"]},
    ])
    assert len(lines) == 2, "同一屏连续两帧应合并"
    assert lines[0]["from"] == 12 and lines[0]["to"] == 48
    assert "栈是后进先出" in lines[0]["text"]


def test_frames_to_lines_skips_empty():
    assert frames_to_lines([{"time": "00:01", "lines": []}]) == []
    assert frames_to_lines([]) == []
