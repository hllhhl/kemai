"""生成一套《数字集成电路基础》演示课件（真 pptx，走真实解析管线）。

不是占位文字：每页都是这门课实际会讲的内容，章节编号连续，
这样课程反查时的「章节序列对齐」才有意义。
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt

SLIDES: list[tuple[str, list[str]]] = [
    ("第 1 章 数字集成电路概览", [
        "1.1 数字集成电路的分类",
        "按集成度分为 SSI、MSI、LSI、VLSI、ULSI",
        "按器件类型分为双极型与 MOS 型，CMOS 是主流",
    ]),
    ("1.2 MOS 晶体管的开关模型", [
        "NMOS 栅极为高电平时导通，源漏之间近似短路",
        "PMOS 栅极为低电平时导通，与 NMOS 互补",
        "截止时源漏之间近似开路，存在亚阈值泄漏",
    ]),
    ("1.3 CMOS 反相器", [
        "NMOS 下拉、PMOS 上拉，两者栅极相连作为输入",
        "输入为低时 PMOS 导通输出高电平，输入为高时 NMOS 导通输出低电平",
        "静态时上下管不同时导通，因此静态功耗极低",
    ]),
    ("1.4 电压传输特性与噪声容限", [
        "VTC 曲线的转折点记为 VM，理想情况 VM 等于 VDD/2",
        "噪声容限 NMH = VOH - VIH，NML = VIL - VOL",
        "噪声容限越大，电路抗干扰能力越强",
    ]),
    ("第 2 章 组合逻辑电路", [
        "2.1 静态 CMOS 门电路",
        "与非门由并联的 NMOS 与串联的 PMOS 构成",
        "或非门由串联的 NMOS 与并联的 PMOS 构成",
        "NMOS 串联会降低驱动能力，因此与非门优于或非门",
    ]),
    ("2.2 传输门与三态门", [
        "传输门由 NMOS 与 PMOS 并联构成，可双向传输",
        "传输门导通电阻随输入电平变化，需注意信号衰减",
        "三态门输出有高、低、高阻三种状态，用于总线共享",
    ]),
    ("2.3 组合逻辑的功耗", [
        "动态功耗 P = α C VDD² f，α 为翻转活动因子",
        "短路功耗来自输入转换瞬间上下管同时导通",
        "降低 VDD 对功耗影响最大，因为是平方关系",
    ]),
    ("第 3 章 时序逻辑电路", [
        "3.1 锁存器与触发器",
        "锁存器是电平敏感，触发器是边沿敏感",
        "主从结构由两个锁存器串联，用相反时钟控制",
        "D 触发器在时钟上升沿采样输入",
    ]),
    ("3.2 建立时间与保持时间", [
        "建立时间 Tsu 是数据必须在时钟沿之前稳定的时间",
        "保持时间 Th 是数据在时钟沿之后必须保持的时间",
        "违例会导致亚稳态，输出在较长时间内不确定",
        "亚稳态无法彻底消除，只能降低发生概率",
    ]),
    ("3.3 时序约束", [
        "最小时钟周期 T >= Tclk-q + Tlogic + Tsetup + Tskew",
        "时钟偏斜 Tskew 会同时影响建立与保持",
        "流水线通过插入寄存器缩短关键路径",
    ]),
]


def build(path: Path) -> Path:
    presentation = Presentation()
    presentation.slide_width = Inches(10)
    presentation.slide_height = Inches(5.63)
    layout = presentation.slide_layouts[5]  # 只有标题的版式

    for title, lines in SLIDES:
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.shapes.title.text_frame.paragraphs[0].font.size = Pt(30)

        box = slide.shapes.add_textbox(Inches(0.7), Inches(1.6), Inches(8.6), Inches(3.4))
        frame = box.text_frame
        frame.word_wrap = True
        for index, line in enumerate(lines):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = line
            paragraph.font.size = Pt(18)

    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(path)
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "decks" / "数字集成电路基础_第1-3章.pptx"
    print(build(target))
