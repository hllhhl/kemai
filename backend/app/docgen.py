"""把笔记变成能交出去的文档：Markdown → docx / pptx，以及在已有 docx 上追加。

为什么单独一层：课脉原来只会「在界面里显示笔记」。用户要交作业、要打印、
要发给老师，界面里的字拿不走。这一层负责把已有产物落成文件。

样式上的两个约定（来自用户反复强调的要求）：
    · 标题黑体、正文宋体
    · 表格黑白，不加底纹，不用彩色强调

刻意不做的事：不追求把 Markdown 全语法还原（脚注、引用块、嵌套列表第 4 层）。
一份学习笔记 90% 的内容是标题 + 段落 + 列表 + 表格，把这四样做干净，
比支持三十种语法但每种都有毛边更有用。
"""

from __future__ import annotations

import re
from pathlib import Path

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
ORDERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
FENCE = re.compile(r"^```")

BODY_FONT = "宋体"
HEAD_FONT = "黑体"
BODY_SIZE = 12          # 小四
HEAD_SIZES = {1: 16, 2: 15, 3: 14, 4: 12.5, 5: 12, 6: 12}


def _set_font(run, name: str, size: float, bold: bool = False) -> None:
    """中文字体必须写 w:eastAsia，只设 font.name 在 Word 里会回落成等线。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)
    rfonts.set(qn("w:ascii"), "Times New Roman")
    rfonts.set(qn("w:hAnsi"), "Times New Roman")


def _inline_runs(paragraph, text: str, size: float) -> None:
    """处理 **粗体** 和 `代码`，其余原样。

    公式（$...$ / $$...$$）故意不做渲染：笔记里的公式来自模型，转成
    OMML 需要额外的转换器，转坏了比留着原文更糟。这里保留 $ 原文，
    让用户在 Word 里用公式编辑器处理。
    """
    for piece in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            _set_font(paragraph.add_run(piece[2:-2]), BODY_FONT, size, bold=True)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            _set_font(paragraph.add_run(piece[1:-1]), "Consolas", size)
        else:
            _set_font(paragraph.add_run(piece), BODY_FONT, size)


def _add_md(doc, markdown: str, *, top_level: int = 1) -> None:
    """把一段 Markdown 追加进 docx。

    ``top_level`` 控制标题降几级：往已有文档里追加时，整块往里缩一层，
    免得新章节的一级标题把原文的章节结构打乱。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if FENCE.match(stripped):
            block: list[str] = []
            i += 1
            while i < len(lines) and not FENCE.match(lines[i].strip()):
                block.append(lines[i])
                i += 1
            i += 1
            for row in block:
                para = doc.add_paragraph()
                para.paragraph_format.space_after = 0
                _set_font(para.add_run(row), "Consolas", BODY_SIZE - 1)
            continue

        # 表格：表头 + 分隔线 + 若干行
        if TABLE_ROW.match(line) and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]):
            rows: list[list[str]] = []
            header = [c.strip() for c in TABLE_ROW.match(line).group(1).split("|")]
            i += 2
            while i < len(lines) and TABLE_ROW.match(lines[i]):
                rows.append([c.strip() for c in TABLE_ROW.match(lines[i]).group(1).split("|")])
                i += 1
            table = doc.add_table(rows=1, cols=len(header))
            table.style = "Table Grid"   # 黑线，无底纹
            head_cells = table.rows[0].cells
            for col, text in enumerate(header[: len(head_cells)]):
                head_cells[col].text = ""
                para = head_cells[col].paragraphs[0]
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _set_font(para.add_run(text), HEAD_FONT, BODY_SIZE - 0.5, bold=True)
            for row in rows:
                cells = table.add_row().cells
                for col, text in enumerate(row[: len(cells)]):
                    cells[col].text = ""
                    _inline_runs(cells[col].paragraphs[0], text, BODY_SIZE - 0.5)
            doc.add_paragraph()
            continue

        heading = HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)), 6)
            shown = min(level + top_level - 1, 6)
            para = doc.add_paragraph()
            para.paragraph_format.space_before = 10 if shown <= 2 else 6
            para.paragraph_format.space_after = 4
            _set_font(para.add_run(heading.group(2).strip()), HEAD_FONT, HEAD_SIZES[shown], bold=True)
            i += 1
            continue

        bullet = BULLET.match(line)
        ordered = ORDERED.match(line)
        if bullet or ordered:
            match = bullet or ordered
            indent = len(match.group(1)) // 2
            para = doc.add_paragraph(style="List Number" if ordered else "List Bullet")
            para.paragraph_format.left_indent = Pt(18 + indent * 18)
            para.paragraph_format.space_after = 2
            _inline_runs(para, match.group(2), BODY_SIZE)
            i += 1
            continue

        para = doc.add_paragraph()
        para.paragraph_format.first_line_indent = Pt(BODY_SIZE * 2)
        para.paragraph_format.line_spacing = 1.5
        para.paragraph_format.space_after = 4
        _inline_runs(para, stripped, BODY_SIZE)
        i += 1


def markdown_to_docx(markdown: str, title: str, out_path: Path) -> Path:
    """一份笔记 → 一份 docx。"""
    from docx import Document
    from docx.shared import Cm

    doc = Document()
    # 页边距按 A4 常用值，免得默认 Letter 打印出来偏
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Cm(2.2)
        section.left_margin = section.right_margin = Cm(2.4)

    if title:
        head = doc.add_paragraph()
        head.alignment = 1  # 居中
        _set_font(head.add_run(title), HEAD_FONT, 18, bold=True)

    _add_md(doc, markdown)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def append_markdown_to_docx(source: Path, markdown: str, out_path: Path) -> Path:
    """在已有 docx 末尾追加一章——这是「改 docx」，不是重写。

    保留原文档的页边距、样式和已有内容，新内容整块降一级标题。
    """
    from docx import Document

    doc = Document(str(source))
    doc.add_page_break()
    _add_md(doc, markdown, top_level=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def _split_sections(markdown: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """按"实际的小节层级"切片：返回 (文档标题, [(小节标题, 小节正文行)])。

    不能写死二级标题——模型产出的笔记层级不固定：有的用 ## 分节，
    有的先来一个 ## 当章、再用 ### 分节。写死的结果是整份笔记压成一张 PPT。

    所以先数一遍标题：挑「出现至少两次的最小层级」当分节线。
        ##×1 + ###×6  → 用 ### 分节（6 页）
        ##×5 + ###×20 → 用 ## 分节（5 页）
    没有重复标题时才退回按空行切段。
    """
    headings: list[tuple[int, int, str]] = []   # (行号, 级别, 文本)
    lines = markdown.splitlines()
    title = ""
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if not match:
            continue
        level, text = len(match.group(1)), match.group(2).strip()
        if level == 1 and not title:
            title = text
            continue
        headings.append((index, level, text))

    counts: dict[int, int] = {}
    for _, level, _ in headings:
        counts[level] = counts.get(level, 0) + 1
    repeatable = [level for level, count in counts.items() if count >= 2]
    if repeatable:
        split_at = min(repeatable)
    elif headings:
        # 只有一个分节标题时也按它切：一门课只有一章是常态，不该退化成按空行切段
        split_at = min(level for _, level, _ in headings)
    else:
        split_at = 0

    if split_at:
        sections: list[tuple[str, list[str]]] = []
        current: tuple[str, list[str]] | None = None
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
            if match and len(match.group(1)) == split_at:
                current = (match.group(2).strip(), [])
                sections.append(current)
                continue
            if current is not None:
                current[1].append(line)
        # 第一个分节标题之前的内容（比如引言）单独成页，否则会被吞掉
        first_index = next((i for i, lv, _ in headings if lv == split_at), None)
        if first_index:
            lead = lines[:first_index]
            body = [l for l in lead if l.strip() and not re.match(r"^#{1,6}\s+", l.strip())]
            if body:
                sections.insert(0, ("概述", body))
        return title, sections

    blocks = [b.strip() for b in re.split(r"\n\s*\n", markdown) if b.strip()]
    return title, [(b.splitlines()[0][:40] or f"第 {i + 1} 部分", b.splitlines()[1:])
                   for i, b in enumerate(blocks)]


def _points(lines: list[str], limit: int = 6) -> list[str]:
    """一节里挑要点：优先列表项，不够再用整句。"""
    bullets: list[str] = []
    prose: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or FENCE.match(stripped) or TABLE_SEP.match(stripped):
            continue
        match = BULLET.match(line) or ORDERED.match(line)
        if match:
            bullets.append(match.group(2).strip())
        elif not HEADING.match(stripped) and not TABLE_ROW.match(line):
            prose.append(stripped)

    picked = bullets or prose
    # 每条压到一行能放下：PPT 上超过 40 字就开始挤
    trimmed = []
    for item in picked:
        text = re.sub(r"\*\*|`", "", item)
        trimmed.append(text if len(text) <= 46 else text[:44] + "…")
    return trimmed[:limit]


def markdown_to_pptx(markdown: str, title: str, out_path: Path) -> Path:
    """一份笔记 → 一份 PPT。一节一页，要点不超过 6 条。"""
    from pptx import Presentation
    from pptx.util import Cm, Pt

    doc_title, sections = _split_sections(markdown)
    deck = Presentation()
    deck.slide_width = Cm(33.87)   # 16:9
    deck.slide_height = Cm(19.05)

    blank = deck.slide_layouts[6]
    cover = deck.slides.add_slide(blank)
    box = cover.shapes.add_textbox(Cm(2.4), Cm(7.0),
                                   Cm(29.0), Cm(3.2))
    frame = box.text_frame
    frame.word_wrap = True
    run = frame.paragraphs[0].add_run()
    run.text = title or doc_title or "复习笔记"
    run.font.size = Pt(36)
    run.font.bold = True
    run.font.name = HEAD_FONT

    for name, body in sections:
        slide = deck.slides.add_slide(blank)
        head = slide.shapes.add_textbox(Cm(2.4), Cm(1.6),
                                        Cm(29.0), Cm(2.0))
        hrun = head.text_frame.paragraphs[0].add_run()
        hrun.text = name
        hrun.font.size = Pt(26)
        hrun.font.bold = True
        hrun.font.name = HEAD_FONT

        points = _points(body)
        if not points:
            continue
        content = slide.shapes.add_textbox(Cm(2.6), Cm(4.2),
                                           Cm(28.6), Cm(13.4))
        cframe = content.text_frame
        cframe.word_wrap = True
        for index, point in enumerate(points):
            para = cframe.paragraphs[0] if index == 0 else cframe.add_paragraph()
            para.level = 0
            para.space_after = Pt(10)
            prow = para.add_run()
            prow.text = "· " + point
            prow.font.size = Pt(18)
            prow.font.name = BODY_FONT

    out_path.parent.mkdir(parents=True, exist_ok=True)
    deck.save(str(out_path))
    return out_path


def extract_docx_text(path: Path) -> str:
    """读 docx 的正文（含表格），用于「改 docx」前的上下文。"""
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_pptx_text(path: Path) -> str:
    """读 ppt 每一页的文字，按页分隔——写笔记时能对上页码。"""
    from pptx import Presentation

    deck = Presentation(str(path))
    pages: list[str] = []
    for index, slide in enumerate(deck.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
        if texts:
            pages.append(f"[第 {index} 页]\n" + "\n".join(texts))
    return "\n\n".join(pages)
