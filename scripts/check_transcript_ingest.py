"""端到端验证：字幕块真的进了知页的检索体系。

不联网。用一段构造的字幕走完整链路——建 Document → 折块 → 分块 → 向量索引 →
检索命中 → 检查返回的 locator 里有没有「第 3 讲 03:24–04:10」。

跑法：``uv run --python 3.12 python scripts/check_transcript_ingest.py``
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models import Chunk, Course, Document, new_id  # noqa: E402
from app.store import Services  # noqa: E402
from app.transcript_ingest import build_blocks, document_name  # noqa: E402

LINES = [
    (0, 8, "这一讲我们讲栈的顺序存储"),
    (8, 20, "栈是限定在表尾进行插入和删除的线性表"),
    (20, 34, "允许插入和删除的一端叫栈顶，另一端叫栈底"),
    (34, 46, "栈的特点是后进先出，也就是 LIFO"),
    (46, 60, "顺序栈用一段连续空间存放元素，用 top 指针指示栈顶位置"),
    (60, 76, "入栈时先判满，top 先加一，再把元素放到 top 指向的位置"),
    (76, 92, "出栈时先判空，取出 top 指向的元素，再把 top 减一"),
    (92, 108, "注意判空是 top 等于负一，判满是 top 等于最大下标"),
    (108, 124, "顺序栈的缺点是容量固定，扩容要整体搬移"),
    (124, 140, "所以实际工程里更多用链栈，或者动态数组实现的栈"),
]


def main() -> None:
    settings = Settings.from_env()
    services = Services(settings)
    try:
        with services.sessions() as db:
            course = db.scalar(select(Course).where(Course.name == "数据结构（演示）"))
            if course is None:
                raise SystemExit("先跑 scripts/seed_finder_demo.py 建演示课程")
            course_id = course.id

        blocks = build_blocks(
            [{"from": f, "to": t, "text": text} for f, t, text in LINES],
            bvid="BVdemo", cid=1, part=1, part_title="3.1.2 栈的顺序存储",
        )
        print(f"折成 {len(blocks)} 块")
        for block in blocks:
            print(f"  {block.locator['label']}  {block.text[:34]}…")

        # 反复跑时先清掉旧的演示字幕，保证结果可复现
        with services.sessions() as db:
            stale = [d.id for d in db.scalars(select(Document).where(
                Document.course_id == course_id, Document.kind == "transcript"))]
        for old_id in stale:
            try:
                services.remove_document(old_id)
            except Exception as exc:  # noqa: BLE001
                print("清理旧字幕失败:", old_id, exc)

        document_id = new_id()
        sidecar = services.upload_dir / (document_id + ".transcript.json")
        sidecar.write_text(json.dumps({"lines": LINES}, ensure_ascii=False), encoding="utf-8")

        with services.sessions() as db:
            db.add(Document(
                id=document_id, course_id=course_id,
                filename=document_name(1, "3.1.2 栈的顺序存储", "BVdemo"),
                kind="transcript",
                file_hash=hashlib.sha1(f"BVdemo:1:{document_id}".encode()).hexdigest(),
                path=str(sidecar), status="queued", warnings=[],
                units_total=len(blocks), units_with_text=len(blocks),
            ))
            db.commit()

        stats = services.ingest_transcript(document_id, [{
            "order": b.order, "text": b.text, "locator": b.locator,
            "heading": b.heading, "chapter_id": None,
        } for b in blocks])
        print("入库结果:", stats)

        with services.sessions() as db:
            doc = db.get(Document, document_id)
            chunks = len(list(db.scalars(select(Chunk).where(Chunk.document_id == document_id))))
            print(f"文档状态: {doc.status} · 块 {doc.units_total} · 分块 {chunks}")

        hits = services.retrieval.search(course_id, "栈的顺序存储 判空 判满", kind="material", limit=3, rerank=False)
        print(f"检索命中 {len(hits)} 条：")
        for hit in hits:
            locator = hit.get("locator") or {}
            print(f"  [{hit.get('filename')}] {locator.get('label')} · {str(hit.get('text'))[:40]}…")
    finally:
        try:
            services.retrieval.close()
        finally:
            services.engine.dispose()


if __name__ == "__main__":
    main()
