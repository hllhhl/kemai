"""组装可重定位的 Python 运行时，供 electron-builder 一起分发。

为什么不能直接打包 .venv：
Windows 的 venv 在 ``pyvenv.cfg`` 里写死了 ``home = C:\\Users\\...\\uv\\python\\...``，
换一台机器那个路径不存在，整个环境就废了。所以这里改成：

    基础解释器（python-build-standalone，本身可重定位）
      + site-packages 合并进去（不做 venv，不写 pyvenv.cfg）

这样 ``python.exe`` 通过自身相对路径找标准库和依赖，装到任何目录都能跑。
已实测：把整份复制到 ``%TEMP%\\kemai-reloc`` 后 prefix 会变成新路径，依赖照常导入。

用法：python scripts/prepare_runtime.py
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
TARGET = ROOT / "_runtime" / "python"


def base_python() -> Path:
    # pyvenv.cfg 是 key = value 平铺格式，没有 section，不能用 configparser
    text = (VENV / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    home = ""
    for line in text.splitlines():
        if line.strip().lower().startswith("home"):
            home = line.split("=", 1)[1].strip()
            break
    if not home:
        raise SystemExit("读不到 .venv/pyvenv.cfg 里的 home，请先 uv sync")
    path = Path(os.path.expandvars(home))
    if not (path / "python.exe").exists():
        raise SystemExit(f"基础解释器不存在：{path}")
    return path


def main() -> int:
    if not VENV.exists():
        raise SystemExit("没有 .venv，先运行 uv sync --frozen --python 3.12")

    source = base_python()
    print(f"基础解释器：{source}")

    if TARGET.exists():
        shutil.rmtree(TARGET)
    started = time.time()
    shutil.copytree(source, TARGET, symlinks=False)
    print(f"  解释器复制完成 {time.time() - started:.0f}s")

    # venv 的标识文件必须删掉，否则解释器会去找原来的 home
    for name in ("pyvenv.cfg",):
        stale = TARGET / name
        if stale.exists():
            stale.unlink()

    site = VENV / "Lib" / "site-packages"
    if not site.exists():
        raise SystemExit("找不到 .venv/Lib/site-packages")
    started = time.time()
    shutil.copytree(
        site, TARGET / "Lib" / "site-packages",
        symlinks=False, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test", "tests"),
    )
    print(f"  依赖合并完成 {time.time() - started:.0f}s")

    # 指向旧路径的 .pth 会让新机器找错地方，直接清掉
    for pth in (TARGET / "Lib" / "site-packages").glob("*.pth"):
        try:
            text = pth.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ".venv" in text or str(ROOT) in text:
            pth.unlink()
            print(f"  清掉指向旧路径的 {pth.name}")

    size = sum(f.stat().st_size for f in TARGET.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"完成：{TARGET}（{size:.0f} MB）")

    check = subprocess.run(
        [str(TARGET / "python.exe"), "-c",
         "import sys, uvicorn, fastapi, chromadb, onnxruntime; print('prefix', sys.prefix)"],
        capture_output=True, text=True, timeout=180,
    )
    ok = check.returncode == 0 and "prefix" in check.stdout
    print(check.stdout.strip() or check.stderr.strip()[:400])
    print("自检：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
