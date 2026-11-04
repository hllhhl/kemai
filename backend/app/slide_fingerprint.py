"""课件页指纹与「同一套课件」判定。

为什么不用文件哈希：老师发来的 PDF 和 B 站网课里那套课件，往往不是同一个文件
（换了封面、删了几页、加了批注、换了模板），哈希必然不同。

判据是两个：**页内容相似度（字符 3-gram Jaccard）** 与 **页顺序一致度（LCS）**。
SimHash 只用来做整份课件的快速签名，不参与页匹配——它对短文本的小改动太敏感，
实测改两个字能翻十几位，会把「同一页」判成两页。

指纹只取正文，排除页码、日期、页脚、版权行：全校课件常共用一套母版，
拿母版特征当判据会误判。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re

_FOOTER = (
    re.compile(r"^\d{1,3}\s*[/／-]\s*\d{1,3}$"),
    re.compile(r"^第?\s*\d{1,3}\s*页$"),
    re.compile(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?$"),
    re.compile(r"^(版权所有|all rights reserved|©|\(c\))", re.I),
)
_PUNCT = re.compile(r"[\s\u3000，,。.；;：:！!？?、（）()《》【】\[\]\"'“”‘’—\-]")
_CHAPTER_PATTERNS = (
    re.compile(r"^(\d+(?:\.\d+){1,3})\s*[.、．:：]?\s*(.*)$"),
    re.compile(r"^第\s*([一二三四五六七八九十百\d]+)\s*[章节讲]\s*(.*)$", re.I),
    re.compile(r"^(?:lecture|lesson|unit|chap(?:ter)?)\s*0*(\d+)\s*[:：.-]?\s*(.*)$", re.I),
)


@dataclass(frozen=True)
class PageFingerprint:
    index: int
    label: str
    text: str
    shingles: frozenset[str]
    simhash: int
    chapters: tuple[dict, ...] = ()


@dataclass(frozen=True)
class DeckFingerprint:
    filename: str
    document_id: str
    pages: tuple[PageFingerprint, ...]
    chapter_seq: tuple[dict, ...] = field(default_factory=tuple)
    glossary: tuple[str, ...] = field(default_factory=tuple)

    @property
    def signature(self) -> str:
        raw = "|".join(str(p.simhash) for p in self.pages).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]


def normalize(text: str) -> str:
    """去掉页码/页脚/日期/版权行与所有标点空白，供 Shingle 使用。"""
    kept: list[str] = []
    for line in str(text or "").splitlines():
        value = line.strip()
        if not value:
            continue
        if any(pattern.search(value) for pattern in _FOOTER):
            continue
        kept.append(value)
    return _PUNCT.sub("", "\n".join(kept)).lower()


def shingles(text: str, size: int = 3) -> frozenset[str]:
    plain = normalize(text)
    if not plain:
        return frozenset()
    if len(plain) <= size:
        return frozenset({plain})
    return frozenset(plain[i : i + size] for i in range(len(plain) - size + 1))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    inter = len(left & right)
    union = len(left) + len(right) - inter
    return inter / union if union else 0.0


def simhash(text: str) -> int:
    """64 位 SimHash（字符 5-shingle）。仅用于整份课件的快速签名。"""
    plain = normalize(text)
    if not plain:
        return 0
    size = 5
    chunks = [plain] if len(plain) <= size else [plain[i : i + size] for i in range(len(plain) - size + 1)]
    weights = [0] * 64
    for chunk in chunks:
        digest = hashlib.blake2b(chunk.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            weights[bit] += 1 if (value >> bit) & 1 else -1
    result = 0
    for bit in range(64):
        if weights[bit] >= 0:
            result |= 1 << bit
    return result


def hamming(left: int, right: int) -> int:
    value = left ^ right
    count = 0
    while value:
        count += value & 1
        value >>= 1
    return count


def chapter_of(line: str) -> dict | None:
    value = str(line or "").strip()
    for pattern in _CHAPTER_PATTERNS:
        matched = pattern.match(value)
        if matched:
            return {"id": matched.group(1), "title": (matched.group(2) or "").strip() or value}
    return None


def page_number(block) -> int:
    """从 Block.locator 里取页/幻灯片号；都没有时按 order 归组。"""
    locator = getattr(block, "locator", None) or {}
    for key in ("slide", "page", "paragraph", "index"):
        value = locator.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return int(getattr(block, "order", 0) or 0) + 1


def block_label(block) -> str:
    locator = getattr(block, "locator", None) or {}
    return str(locator.get("label") or "").strip() or f"第 {page_number(block)} 页"


def build_deck(blocks, filename: str, document_id: str = "", glossary_hint: list[str] | None = None) -> DeckFingerprint:
    """把一份文档的 Block 列表折成页级指纹。

    同一页的多个 Block 会拼在一起——页面才是学生能核对的最小单位。
    """
    grouped: dict[int, list] = {}
    for block in blocks or []:
        text = str(getattr(block, "text", "") or "").strip()
        if not text:
            continue
        grouped.setdefault(page_number(block), []).append(block)

    pages: list[PageFingerprint] = []
    chapter_seq: list[dict] = []
    seen_chapters: set[tuple[str, int]] = set()

    for index, (number, items) in enumerate(sorted(grouped.items()), start=1):
        items.sort(key=lambda b: int(getattr(b, "order", 0) or 0))
        text = "\n".join(str(getattr(b, "text", "") or "").strip() for b in items)
        chapters: list[dict] = []
        for line in text.splitlines()[:6]:
            found = chapter_of(line)
            if found:
                chapters.append(found)
                key = (found["id"], index)
                if key not in seen_chapters:
                    seen_chapters.add(key)
                    chapter_seq.append({"id": found["id"], "title": found["title"], "page": number})
        label = block_label(items[0])
        pages.append(
            PageFingerprint(
                index=index,
                label=label,
                text=text,
                shingles=shingles(text),
                simhash=simhash(text),
                chapters=tuple(chapters),
            )
        )

    return DeckFingerprint(
        filename=filename,
        document_id=document_id,
        pages=tuple(pages),
        chapter_seq=tuple(chapter_seq),
        glossary=tuple(extract_glossary(pages, glossary_hint)),
    )


def extract_glossary(pages: tuple[PageFingerprint, ...], hint: list[str] | None = None) -> list[str]:
    """抽术语：英文词 + 「中文（English）」对照 + 短标题。短标题才可能是知识点名。"""
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "into", "using",
        "第", "章", "节", "讲", "概述", "小结", "总结", "目录", "谢谢", "回顾", "内容", "本章",
    }
    counts: dict[str, int] = {}
    for page in pages:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{2,}", page.text):
            key = word.lower()
            if key in stop or len(key) < 3:
                continue
            counts[key] = counts.get(key, 0) + 1
        for chinese, english in re.findall(r"([\u4e00-\u9fa5]{2,10})\s*[（(]\s*([A-Za-z][A-Za-z0-9 +#.-]{2,24})\s*[)）]", page.text):
            key = english.strip().lower()
            counts[key] = counts.get(key, 0) + 4
            counts[chinese] = counts.get(chinese, 0) + 2
        for line in page.text.splitlines():
            value = line.strip()
            if not (2 <= len(value) <= 10):
                continue
            if re.search(r"[，。；：？！、（）()\[\]【】]", value) or re.match(r"^(\d|第|lecture|chapter)", value, re.I):
                continue
            if value in stop:
                continue
            counts[value] = counts.get(value, 0) + 2
    for term in hint or []:
        if term:
            counts[str(term)] = counts.get(str(term), 0) + 6
    ranked = sorted(((term, n) for term, n in counts.items() if n >= 2), key=lambda x: -x[1])
    return [term for term, _ in ranked[:40]]


def lcs_length(left: list, right: list) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            current[j] = previous[j - 1] + 1 if left[i - 1] == right[j - 1] else max(previous[j], current[j - 1])
        previous = current
    return previous[-1]


def compare_decks(left: DeckFingerprint, right: DeckFingerprint, threshold: float = 0.62) -> dict:
    """判定两份课件是否同一套，并给出差异页清单。

    只有两份文件都在本地且 SHA 相同时才能说「文件级完全一样」；
    其余情形最多是「内容级完全一样」——这是本项目对用户的承诺，不夸大。
    """
    if not left.pages or not right.pages:
        return {"similarity": 0.0, "page_cover": 0.0, "order_ratio": 0.0, "verdict": "不同",
                "matched": [], "missing": [p.index for p in left.pages]}

    pairs: list[tuple[float, int, int]] = []
    for i, a in enumerate(left.pages):
        for j, b in enumerate(right.pages):
            score = jaccard(a.shingles, b.shingles)
            if score >= threshold:
                pairs.append((score, i, j))
    pairs.sort(reverse=True)

    used_left: set[int] = set()
    used_right: set[int] = set()
    matched: list[dict] = []
    for score, i, j in pairs:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        matched.append({
            "page_a": left.pages[i].index,
            "page_b": right.pages[j].index,
            "similarity": round(score, 3),
        })
    matched.sort(key=lambda m: m["page_a"])

    denominator = max(1, min(len(left.pages), len(right.pages)))
    page_cover = min(1.0, len(matched) / denominator)
    if len(matched) > 1:
        order_ratio = lcs_length([m["page_a"] for m in matched], [m["page_b"] for m in matched]) / len(matched)
    else:
        order_ratio = 1.0 if matched else 0.0
    similarity = 0.6 * page_cover + 0.4 * order_ratio

    if similarity >= 0.92:
        verdict = "完全一样（内容级）"
    elif similarity >= 0.75:
        verdict = "高度一致"
    elif similarity >= 0.5:
        verdict = "部分重合"
    else:
        verdict = "不同"

    return {
        "similarity": round(similarity, 3),
        "page_cover": round(page_cover, 3),
        "order_ratio": round(order_ratio, 3),
        "verdict": verdict,
        "matched": matched,
        "missing": [p.index for i, p in enumerate(left.pages) if i not in used_left],
    }
