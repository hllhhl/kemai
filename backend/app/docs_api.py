"""导出：把笔记落成 docx / pptx，并在已有 docx 上追加。

和 uploads 方向相反：那边是「你的文件进来」，这里是「我的结果出去」。

三个刻意的取舍：

1. 文件落在 data/exports，不进数据库。产物是派生物——重新生成一次就有，
   存进库里会让备份体积跟着笔记一起涨。
2. 文件名带时间戳，不覆盖上一次导出。用户点了两次导出，拿到的是两份文件，
   而不是"第二份把第一份盖了然后他不确定刚才那份还在不在"。
3. 改 docx 用追加，不用重写。用户上传的那份格式可能是他排版过的，
   重写等于把他的排版扔掉。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .docgen import append_markdown_to_docx, markdown_to_docx, markdown_to_pptx
from .models import Document, Outline

logger = logging.getLogger(__name__)

SAFE = re.compile(r"[^\w\u4e00-\u9fff.-]+")


class ExportInput(BaseModel):
    kind: str = Field(pattern="^(docx|pptx)$")
    outline_id: str = ""
    title: str = Field(default="", max_length=120)


class AppendInput(BaseModel):
    markdown: str = Field(min_length=1, max_length=200_000)
    title: str = Field(default="", max_length=120)


def _exports_dir() -> Path:
    from .config import Settings

    settings = Settings.from_env()
    path = Path(settings.data_dir) / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(text: str, fallback: str) -> str:
    cleaned = SAFE.sub("-", (text or "").strip()).strip("-.")
    return (cleaned or fallback)[:60]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def register_doc_routes(app, services) -> None:
    @app.post("/courses/{course_id}/exports")
    def export_outline(course_id: str, payload: ExportInput):
        """把课程的最新笔记导出成 docx 或 pptx。

        只导出**已生成完成**的笔记：导出一份没写完的笔记，用户拿到的是半份，
        而且他很可能在打印之后才发现。
        """
        with services.sessions() as db:
            if payload.outline_id:
                outline = db.get(Outline, payload.outline_id)
            else:
                outline = db.scalar(
                    select(Outline)
                    .where(Outline.course_id == course_id, Outline.status.in_(("completed", "ready")))
                    .order_by(Outline.created_at.desc())
                )
            if outline is None:
                raise HTTPException(409, "这门课还没有生成完成的笔记，先生成再导出")
            if outline.status not in ("completed", "ready") or not (outline.markdown or "").strip():
                raise HTTPException(409, "笔记还没写完，等它完成再导出")
            markdown = outline.markdown

        title = payload.title.strip() or "复习笔记"
        name = f"{_safe_name(title, '复习笔记')}-{_stamp()}.{payload.kind}"
        target = _exports_dir() / name
        try:
            if payload.kind == "docx":
                markdown_to_docx(markdown, title, target)
            else:
                markdown_to_pptx(markdown, title, target)
        except Exception as exc:  # noqa: BLE001 - 生成失败要给出人能看懂的原因
            logger.exception("导出失败")
            raise HTTPException(500, f"生成失败：{type(exc).__name__}: {exc}") from exc

        return {
            "filename": name,
            "url": f"/exports/{name}",
            "bytes": target.stat().st_size,
            "kind": payload.kind,
        }

    @app.post("/documents/{document_id}/exports")
    def append_to_document(document_id: str, payload: AppendInput):
        """在一份已上传的 docx 末尾追加一章——这是「改 docx」。

        只支持 docx：往 pptx 里追加一页需要决定版式，拍脑袋决定的版式
        多半比用户原来的难看，不如不做。
        """
        with services.sessions() as db:
            document = db.get(Document, document_id)
            if document is None:
                raise HTTPException(404, "资料不存在")
            source = Path(document.path or "")
            filename = document.filename or ""
        if source.suffix.lower() != ".docx":
            raise HTTPException(422, "只有 .docx 支持追加内容")
        if not source.exists():
            raise HTTPException(409, "原始文件不在磁盘上，可能已被移动或删除")

        title = payload.title.strip() or (Path(filename).stem + "（续）")
        name = f"{_safe_name(title, '追加内容')}-{_stamp()}.docx"
        target = _exports_dir() / name
        try:
            append_markdown_to_docx(source, payload.markdown, target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("追加失败")
            raise HTTPException(500, f"写入失败：{type(exc).__name__}: {exc}") from exc

        return {"filename": name, "url": f"/exports/{name}", "bytes": target.stat().st_size, "kind": "docx"}

    @app.get("/exports/{name}")
    def download_export(name: str):
        # 只允许访问导出目录里真实存在的文件，挡住 ../ 之类
        path = (_exports_dir() / name).resolve()
        root = _exports_dir().resolve()
        if root not in path.parents or not path.exists():
            raise HTTPException(404, "文件不存在")
        return FileResponse(path, filename=name)
