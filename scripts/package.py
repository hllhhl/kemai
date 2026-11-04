"""打一个源码压缩包（不含依赖目录，解压后按 README 装一次即可）。

排除：虚拟环境、node_modules、数据库、缓存、git 历史。
保留：源码、已构建的前端 dist、演示课件、文档、启动脚本、图标。

用法：uv run --python 3.12 python scripts/package.py
"""

from __future__ import annotations

import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"

EXCLUDE_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    "data", ".cache", ".idea", ".vscode", "htmlcov", ".mypy_cache",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".db-wal", ".db-shm"}
EXCLUDE_FILES = {".env"}


def included(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return False
    if path.name in EXCLUDE_FILES:
        return False
    if path.suffix.lower() in EXCLUDE_SUFFIX:
        return False
    return True


def main() -> int:
    desktop = Path(os.path.expanduser("~/Desktop"))
    if not desktop.exists():
        desktop = Path(os.path.expanduser("~/OneDrive/Desktop"))
    target = desktop / f"课脉-v{VERSION}-源码.zip"

    files: list[tuple[Path, str]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if not included(relative):
            continue
        files.append((path, str(Path(f"kemai-{VERSION}") / relative).replace("\\", "/")))

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, arcname in files:
            archive.write(path, arcname)

    size = target.stat().st_size
    print(f"打包完成：{target}")
    print(f"文件数：{len(files)}，大小：{size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
