"""给本地演示灌一份「课件 + 章节目录」，用于验证 PPT 找课链路。

只写数据库，不走上传/OCR 管线——目的是让 `finder/*` 接口有真实的
Block 与 locator 可读，而不是伪造返回值。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import create_database  # noqa: E402
from app.models import Block, Course, Document, new_id  # noqa: E402

SLIDES = [
    "第 3 章 栈与队列\n3.1 栈\n栈是限定仅在表尾进行插入和删除的线性表\n允许插入删除的一端称为栈顶，另一端称为栈底\n栈的特点：后进先出（LIFO）",
    "3.2 队列\n队列只允许在一端插入、在另一端删除\n插入端为队尾，删除端为队头\n队列的特点：先进先出（FIFO）",
    "3.3 栈的应用\n括号匹配：遇到左括号入栈，右括号出栈比对\n表达式求值：操作数栈与运算符栈配合\n递归的实现依赖系统栈保存返回地址",
    "3.4 循环队列\n顺序队列存在假溢出问题\n用取模运算把数组首尾相连构成循环队列\n队满判据：(rear+1)%MaxSize==front\n队空判据：rear==front",
]


def main() -> None:
    settings = Settings.from_env()
    engine, sessions = create_database(settings.data_dir)
    with sessions() as db:
        course = db.scalar(select(Course).where(Course.name == "数据结构（演示）"))
        if course is None:
            course = Course(id=new_id(), name="数据结构（演示）")
            db.add(course)
            db.flush()
        document = db.scalar(select(Document).where(
            Document.course_id == course.id, Document.filename == "第3章 栈与队列.pptx"))
        if document is None:
            document = Document(
                id=new_id(),
                course_id=course.id,
                filename="第3章 栈与队列.pptx",
                kind="pptx",
                file_hash=hashlib.sha1("demo-deck".encode()).hexdigest(),
                path="",
                status="ready",
                warnings=[],
                units_total=len(SLIDES),
                units_with_text=len(SLIDES),
            )
            db.add(document)
            db.flush()
        existing = db.scalar(select(Block.id).where(Block.document_id == document.id))
        if existing is None:
            for order, text in enumerate(SLIDES):
                db.add(Block(
                    id=new_id(), document_id=document.id, chapter_id=None, order=order,
                    text=text, locator={"page": order + 1, "label": f"第 {order + 1} 页"},
                    heading=text.splitlines()[0], block_type="text",
                ))
        db.commit()
        print(f"course_id={course.id}")
        print(f"document_id={document.id}")
    engine.dispose()


if __name__ == "__main__":
    main()
