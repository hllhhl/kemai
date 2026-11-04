"""逐讲消化与视觉确认的单元测试（离线，不调模型、不联网）。"""

from types import SimpleNamespace

import pytest

from app.finder_digest import collect_lines, digest_part, mechanical, timeline, verify
from app.finder_visual import check_frames, match_text, page_texts
from app.slide_fingerprint import build_deck


def make_blocks(rows):
    """rows: (from, to, text)"""
    return [
        SimpleNamespace(
            text=text, order=index,
            locator={"from": start, "to": end, "label": f"第 1 讲 00:0{index}"},
            heading="第 1 讲",
        )
        for index, (start, end, text) in enumerate(rows)
    ]


LINES = [
    (0, 8, "这一讲我们讲栈的顺序存储"),
    (8, 20, "栈是限定在表尾进行插入和删除的线性表"),
    (20, 34, "允许插入和删除的一端叫栈顶，另一端叫栈底"),
    (34, 46, "栈的特点是后进先出，也就是 LIFO"),
    (46, 60, "注意判空是 top 等于负一，判满是 top 等于最大下标"),
]


def test_collect_lines_sorts_and_drops_non_transcript():
    blocks = make_blocks(LINES)
    blocks.append(SimpleNamespace(text="没有时间戳的块", order=9, locator={"page": 1}, heading=None))
    lines = collect_lines(blocks)
    assert len(lines) == len(LINES)
    assert [line["from"] for line in lines] == sorted(line["from"] for line in lines)


def test_timeline_puts_seconds_in_front():
    text = timeline(collect_lines(make_blocks(LINES)))
    assert text.startswith("[0] ")
    assert "[46]" in text


def test_mechanical_keeps_real_timestamps():
    points = mechanical(collect_lines(make_blocks(LINES)))
    allowed = {0, 8, 20, 34, 46}
    assert points, "降级路径应该至少挑出一句"
    assert all(point["at"] in allowed for point in points)


def test_verify_drops_fabricated_timestamps():
    allowed = {0, 8, 20}
    kept, dropped = verify(
        [{"text": "栈是后进先出", "at": 8},
         {"text": "第 12 分钟讲到扩容", "at": 720},
         {"text": "", "at": 20}],
        allowed, limit=5,
    )
    assert len(kept) == 1 and kept[0]["at"] == 8
    assert dropped == 2


class FakeLLM:
    configured = True

    def __init__(self, payload):
        self.payload = payload

    def complete(self, _messages, json_mode=False):
        assert json_mode is True
        return self.payload


def test_digest_uses_model_and_verifies_timestamps():
    llm = FakeLLM('{"keypoints":[{"text":"栈是后进先出","at":34},'
                  '{"text":"编造的一条","at":999}],'
                  '"gotchas":[{"text":"判空条件写错","at":46}],'
                  '"terms":["后进先出（LIFO）"]}')
    result = digest_part(collect_lines(make_blocks(LINES)), part=1, part_title="3.1.2 栈的顺序存储", llm=llm)
    assert result["degraded"] is False
    assert [item["at"] for item in result["keypoints"]] == [34]
    assert result["dropped"] == 1
    assert result["gotchas"][0]["at"] == 46
    assert result["terms"] == ["后进先出（LIFO）"]


def test_digest_degrades_without_model():
    result = digest_part(collect_lines(make_blocks(LINES)), part=1, part_title="栈", llm=None)
    assert result["degraded"] is True
    assert result["keypoints"]


def test_digest_rejects_empty_part():
    with pytest.raises(ValueError, match="没有可用"):
        digest_part([], part=1, part_title="空")


def make_deck():
    blocks = [
        SimpleNamespace(text="第3章 栈与队列\n3.1 栈\n栈是限定在表尾进行插入和删除的线性表\n后进先出 LIFO",
                        locator={"page": 1, "label": "第 1 页"}, order=0),
        SimpleNamespace(text="3.2 队列\n队列一端插入一端删除\n先进先出 FIFO",
                        locator={"page": 2, "label": "第 2 页"}, order=1),
    ]
    return build_deck(blocks, "第3章 栈与队列.pptx", "doc-1")


def test_match_text_hits_the_right_page():
    deck = make_deck()
    pages = page_texts(deck)
    hit = match_text("栈是限定在表尾进行插入和删除的线性表，后进先出", pages)
    assert hit is not None and hit["page"] == 1
    assert hit["similarity"] > 0.25


def test_match_text_returns_none_for_unrelated_text():
    pages = page_texts(make_deck())
    assert match_text("今天天气不错，我们去公园散步", pages) is None


def test_visual_check_degrades_without_model():
    frames = [{"time": "00:10", "dataUrl": "data:image/jpeg;base64,AAAA"}]
    result = check_frames(frames, make_deck(), None, part=1)
    assert result["degraded"] is True
    assert "没有配置多模态模型" in result["note"]
    assert result["confidence"] == 0


def test_visual_check_reads_text_then_matches_page():
    llm = FakeLLM('{"is_slide": true, "text": "栈是限定在表尾进行插入和删除的线性表 后进先出"}')
    frames = [
        {"time": "00:10", "dataUrl": "data:image/jpeg;base64,AAAA"},
        {"time": "00:40", "dataUrl": "data:image/jpeg;base64,BBBB"},
    ]
    result = check_frames(frames, make_deck(), llm, part=1)
    assert result["degraded"] is False
    assert result["pages_hit"] == [1]
    assert result["confidence"] > 0.3
    assert "画面确认" in result["verdict"]


def test_visual_check_ignores_non_slide_frames():
    llm = FakeLLM('{"is_slide": false, "text": ""}')
    result = check_frames([{"time": "00:10", "dataUrl": "data:image/jpeg;base64,AAAA"}], make_deck(), llm)
    assert result["pages_hit"] == []
    assert "没能对上" in result["verdict"]
