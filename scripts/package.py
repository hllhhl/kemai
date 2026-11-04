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
    "data", ".cache", ".idea", ".vscode", "htmlcov", ".mypy_cache", "_package",
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


def collect() -> list[tuple[Path, str]]:
    """用 os.walk 剪枝，不进入被排除的目录。

    原来的 rglob("*") 会把 node_modules 和 .venv 全枚举一遍再过滤，
    在装好依赖的机器上要跑好几分钟——等于每次打包都被自己拖死。
    """
    files: list[tuple[Path, str]] = []
    for current, dirs, names in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in EXCLUDE_DIRS]
        for name in names:
            path = Path(current) / name
            if name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIX:
                continue
            files.append((path, str(path.relative_to(ROOT)).replace("\\", "/")))
    return files


def main() -> int:
    # 不往桌面丢东西：产物统一放项目的 _package/ 目录
    out_dir = ROOT / "_package"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"课脉-v{VERSION}-源码.zip"

    files = [(path, str(Path(f"kemai-{VERSION}") / rel)) for path, rel in collect()]

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, arcname in files:
            archive.write(path, arcname)

    size = target.stat().st_size
    print(f"打包完成：{target}")
    print(f"文件数：{len(files)}，大小：{size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
