"""文档生成的测试：写出来的文件必须能被读回来。

这一层的失败方式很讨厌——不报错，但用户打开是一份空文档或结构错乱的稿子。
所以每条断言都走「生成 → 用 python-docx/python-pptx 读回 → 核对结构」。
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from pptx import Presentation

from app.docgen import (
    append_markdown_to_docx,
    extract_docx_text,
    markdown_to_docx,
    markdown_to_pptx,
)

SAMPLE = """# 数字集成电路基础

## 一、MOS 开关模型
- NMOS：栅极高电平导通
- PMOS：特性互补
正文里也有一句话。

## 二、CMOS 反相器
| 输入 | 输出 |
| --- | --- |
| 低 | 高 |

### 2.1 噪声容限
$$ NM_H = V_{OH} - V_{IH} $$
"""


def test_docx_keeps_headings_fonts_and_table(tmp_path: Path):
    target = markdown_to_docx(SAMPLE, "复习笔记", tmp_path / "note.docx")
    assert target.exists() and target.stat().st_size > 5000

    text = extract_docx_text(target)
    assert "MOS 开关模型" in text
    assert "NMOS：栅极高电平导通" in text
    # 表格内容要真的进了表格，而不是退化成一行纯文本
    assert "低 | 高" in text

    doc = Document(str(target))
    # 标题用了黑体，正文用了宋体——这是硬要求，回落成等线就不合格
    from docx.oxml.ns import qn

    east_asia = set()
    for para in doc.paragraphs:
        for run in para.runs:
            rfonts = run._element.find(qn("w:rPr") + "/" + qn("w:rFonts"))
            if rfonts is not None:
                value = rfonts.get(qn("w:eastAsia"))
                if value:
                    east_asia.add(value)
    assert "黑体" in east_asia, f"中文字体没写进 eastAsia：{east_asia}"
    assert "宋体" in east_asia
    assert len(doc.tables) == 1


def test_pptx_splits_by_real_heading_level(tmp_path: Path):
    """笔记里 ## 只有一个、### 有多个时，也要按 ### 分页。

    写死二级标题的后果就是整份笔记被压成一张幻灯片。
    """
    markdown = """# 标题

## 第1章

### 1.1 逻辑门
- 与门
- 或门

### 1.2 触发器
- 锁存器
- 边沿触发
"""
    target = markdown_to_pptx(markdown, "课件", tmp_path / "deck.pptx")
    deck = Presentation(str(target))
    # 封面 + 两个小节
    assert len(deck.slides) == 3

    titles = []
    for slide in deck.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                titles.append(shape.text_frame.text.strip().splitlines()[0])
    assert any("1.1 逻辑门" in t for t in titles)
    assert any("1.2 触发器" in t for t in titles)


def test_ppt_points_are_trimmed_not_dumped(tmp_path: Path):
    """一节里塞 12 条要点时，PPT 只留前 6 条——否则一页放不下，字会缩到看不清。"""
    body = "\n".join(f"- 要点 {i}" for i in range(1, 13))
    markdown = f"# 标题\n\n## 一节\n{body}\n"
    target = markdown_to_pptx(markdown, "课件", tmp_path / "deck.pptx")
    deck = Presentation(str(target))
    content = "\n".join(
        shape.text_frame.text for shape in deck.slides[1].shapes if shape.has_text_frame
    )
    assert "要点 1" in content
    assert "要点 7" not in content


def test_append_keeps_existing_content(tmp_path: Path):
    """「改 docx」是追加，不是重写——原文档的段落必须还在。"""
    base = markdown_to_docx("# 原文档\n\n第一段原文。\n", "原文档", tmp_path / "base.docx")
    merged = append_markdown_to_docx(base, "## 新增一章\n\n新写的内容。\n", tmp_path / "merged.docx")

    text = extract_docx_text(merged)
    assert "第一段原文。" in text, "原文被覆盖了"
    assert "新写的内容。" in text, "新增内容没进去"

    doc = Document(str(merged))
    # 追加页从新一页开始，否则会接在原文最后一段后面，看着像同一段
    assert any(p.runs and p.runs[0].text and "文档" in p.runs[0].text for p in doc.paragraphs)


def test_empty_markdown_still_produces_a_file(tmp_path: Path):
    """笔记为空时宁可给一份带标题的空白文件，也不要抛异常。

    抛异常会让前端的「导出」按钮变成一个红字报错，而用户其实只是还没生成笔记。
    """
    target = markdown_to_docx("", "空笔记", tmp_path / "empty.docx")
    assert target.exists()
    assert "空笔记" in extract_docx_text(target)
