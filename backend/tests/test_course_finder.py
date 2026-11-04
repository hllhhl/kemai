"""课件指纹与课程匹配的单元测试。

全部离线：不请求 B 站，不建数据库。
外部依赖只有 ``BiliVideo`` 这个 dataclass，用构造出来的对象代替真实响应。
"""

from types import SimpleNamespace

import pytest

from app.bilibili import BiliPage, BiliVideo, chapter_id, chapter_prefixes, merge_transcript, search_terms
from app.course_finder import find_courses, map_parts, scan_deck, score_course
from app.slide_fingerprint import (
    build_deck,
    chapter_of,
    compare_decks,
    jaccard,
    normalize,
    shingles,
)

BLOCKS = [
    "第3章 栈与队列\n3.1 栈\n栈是限定仅在表尾进行插入和删除的线性表",
    "3.2 队列\n队列只允许在一端插入、在另一端删除\n先进先出（FIFO）",
    "3.3 循环队列\n队满判据：(rear+1)%MaxSize==front",
]


def make_blocks(texts, start=0):
    return [
        SimpleNamespace(text=text, locator={"page": start + i + 1, "label": f"第 {start + i + 1} 页"}, order=i)
        for i, text in enumerate(texts)
    ]


@pytest.fixture
def deck():
    return build_deck(make_blocks(BLOCKS), "第3章 栈与队列.pptx", "doc-1")


def test_normalize_drops_page_numbers_and_dates():
    plain = normalize("3 / 12\n2026-11-04\n栈是后进先出\n第 5 页\n版权所有")
    assert "栈是后进先出" in plain
    assert "2026" not in plain
    assert "版权所有" not in plain


def test_shingles_and_jaccard_resist_small_edits():
    left = shingles("栈是后进先出的线性表，只允许在栈顶插入和删除")
    right = shingles("栈是后进先出的线性结构，只允许在栈顶插入和删除")
    other = shingles("第九章 图论，Dijkstra 最短路径算法与最小生成树")
    assert jaccard(left, right) > 0.62
    assert jaccard(left, other) < 0.1


def test_chapter_patterns():
    assert chapter_of("3.1.1 栈的定义")["id"] == "3.1.1"
    assert chapter_of("第三章 线性表")["id"] == "三"
    assert chapter_of("Lecture 5: Trees")["id"] == "5"
    assert chapter_of("一句普通的话") is None
    assert chapter_id("3.1.1栈的定义") == "3.1.1"
    # 西电那门课 72 讲全用下划线分隔编号
    assert chapter_id("0_1_前言") == "0.1"
    assert chapter_id("1_2_组合逻辑") == "1.2"
    assert chapter_id("P1 第1集") is None
    assert chapter_prefixes("3.1.1") == ["3", "3.1", "3.1.1"]


def test_build_deck_collects_chapters_and_glossary(deck):
    assert len(deck.pages) == 3
    ids = [item["id"] for item in deck.chapter_seq]
    assert "3" in ids and "3.1" in ids and "3.2" in ids
    assert any("fifo" in term.lower() for term in deck.glossary)
    assert deck.signature and len(deck.signature) == 16


def test_compare_identical_decks(deck):
    other = build_deck(make_blocks(BLOCKS), "copy.pptx", "doc-2")
    result = compare_decks(deck, other)
    assert result["similarity"] == 1.0
    assert len(result["matched"]) == 3
    assert "完全一样" in result["verdict"]


def test_compare_when_cover_changed_and_a_page_dropped(deck):
    variant = build_deck(
        make_blocks(["数据结构 期末复习\n授课教师：张老师", BLOCKS[1], BLOCKS[2]]),
        "variant.pptx",
        "doc-3",
    )
    result = compare_decks(deck, variant)
    assert 0.6 <= result["similarity"] < 1.0
    assert result["verdict"] in ("高度一致", "部分重合")


def test_compare_unrelated_decks(deck):
    other = build_deck(make_blocks(["第九章 图论\nDijkstra 最短路径算法"]), "graph.pptx", "doc-4")
    assert compare_decks(deck, other)["verdict"] == "不同"


def test_scan_deck_returns_identity_and_keywords(deck):
    identity = scan_deck(deck)
    assert identity["pages"] == 3
    assert identity["course"]
    assert identity["keywords"]
    assert all(len(term) <= 40 for term in identity["keywords"])


def test_search_terms_skip_duplicate_course_and_chapter():
    terms = search_terms("栈与队列", ["栈与队列", "栈", "队列"], ["fifo", "lifo"])
    assert terms[0] == "栈与队列"
    assert not any(term == "栈与队列 栈与队列" for term in terms)


def make_video(parts, title="数据结构 第三章 栈与队列 全套教程", bvid="BVtest"):
    pages = tuple(
        BiliPage(page=index, cid=1000 + index, part=part, duration=600)
        for index, part in enumerate(parts, start=1)
    )
    return BiliVideo(bvid=bvid, title=title, owner="某老师", cover="", pages=pages, videos=len(pages))


def test_score_course_prefers_matching_chapter_numbers(deck):
    good = make_video(["3.1.1 栈的定义", "3.1.2 栈的顺序存储", "3.2.1 队列的定义"])
    bad = make_video(["P1 第1集"], title="随手拍的视频")
    good_score = score_course(good, deck, "栈与队列")
    bad_score = score_course(bad, deck, "栈与队列")
    assert good_score["score"] > bad_score["score"] + 0.3
    assert good_score["chapter_cover"] == 1.0
    assert bad_score["need_confirm"] is True


def test_map_parts_uses_chapter_prefix(deck):
    video = make_video(["3.1.1 栈的定义", "3.3.2 循环队列的判定"])
    mapped = map_parts(video, deck)
    assert len(mapped) == 2
    assert 1 in mapped[0]["pages"]
    assert mapped[0]["confidence"] >= 0.8
    assert 3 in mapped[1]["pages"]
    assert mapped[0]["evidence"][0] == "video:BVtest:P1"


def test_map_parts_falls_back_with_low_confidence(deck):
    video = make_video(["P1", "P2"], title="无章节编号")
    mapped = map_parts(video, deck)
    assert all(item["confidence"] <= 0.55 for item in mapped)


class FakeClient:
    def __init__(self, hits, videos):
        self._hits = hits
        self._videos = videos
        self.search_calls = []

    def search(self, keyword, page=1, limit=12):
        self.search_calls.append(keyword)
        return self._hits

    def view(self, bvid):
        return self._videos[bvid]


def test_find_courses_orders_candidates_by_score(deck):
    hits = [
        {"bvid": "BVgood", "title": "数据结构 第三章 全套教程", "author": "A", "play": 100000, "description": ""},
        {"bvid": "BVbad", "title": "随便聊聊", "author": "B", "play": 10, "description": ""},
    ]
    videos = {
        "BVgood": make_video(["3.1.1 栈的定义", "3.1.2 栈的顺序存储", "3.2.1 队列的定义"], bvid="BVgood"),
        "BVbad": make_video(["P1"], title="随便聊聊", bvid="BVbad"),
    }
    result = find_courses(deck, FakeClient(hits, videos), course_name="栈与队列", limit=5, inspect=5)
    assert result["candidates"][0]["bvid"] == "BVgood"
    assert result["candidates"][0]["mapping"]
    assert result["keywords"]


def test_find_courses_reports_empty_search(deck):
    with pytest.raises(ValueError, match="没返回结果"):
        find_courses(deck, FakeClient([], {}), course_name="栈与队列")


def test_merge_transcript_joins_fragments():
    merged = merge_transcript([
        {"from": 0, "to": 1, "text": "栈是"},
        {"from": 1, "to": 2, "text": "后进先出"},
        {"from": 2, "to": 3, "text": "栈是"},
        {"from": 10, "to": 12, "text": "队列是先进先出的线性表，只允许在队尾插入"},
    ])
    assert len(merged) < 4
    assert any("栈是后进先出" in line["text"] for line in merged)
