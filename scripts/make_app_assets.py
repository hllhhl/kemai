"""生成课脉的图标、启动脚本与桌面快捷方式。

三件事一次做完：
  1. 画一个 ico（多尺寸），白字「课」压在朱红圆角块上；
  2. 写 `启动课脉.cmd`：起后端 → 起前端预览 → 开浏览器；
  3. 用 Windows 的 IShellLink（纯 ctypes，不需要 pywin32）在桌面建 .lnk。

为什么不用 pywin32：这台机器没装，也不想为一个图标引入新依赖。
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, c_int, c_void_p, c_wchar_p
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICON = ASSETS / "kemai.ico"
LAUNCHER = ROOT / "启动课脉.cmd"

BRAND_BG = (164, 67, 42)      # #a4432a，与界面主色一致
BRAND_FG = (255, 253, 248)


def make_icon() -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=52, fill=BRAND_BG)

    font = None
    for candidate in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(candidate, 150)
            break
        except OSError:
            continue
    text = "课"
    if font is None:
        draw.text((62, 48), text, fill=BRAND_FG)
    else:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.text(((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top), text,
                  fill=BRAND_FG, font=font)

    image.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON


def make_launcher() -> Path:
    """启动脚本：先起后端，等它就绪，再起前端，最后开浏览器。"""
    content = r"""@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 课脉 · 课程知识体系 Agent

if not exist ".venv\Scripts\python.exe" (
  echo [课脉] 还没有安装依赖。
  echo   首次使用请在命令行运行：
  echo     uv sync --frozen --python 3.12
  echo     npm install --prefix frontend
  echo     npm install --prefix model-runtime
  echo   完成后再次双击本文件。
  pause
  exit /b 1
)

if not exist "frontend\dist\index.html" (
  echo [课脉] 正在构建前端界面，第一次会慢一些...
  pushd frontend
  call npm run build
  popd
)

echo [课脉] 启动本地服务...
start "课脉 · 本地服务" /min ".venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

echo [课脉] 等后端就绪...
set /a TRIES=0
:waitloop
set /a TRIES+=1
".venv\Scripts\python.exe" -c "import socket,sys; s=socket.socket(); s.settimeout(0.6); sys.exit(0 if s.connect_ex(('127.0.0.1',8000))==0 else 1)" >nul 2>&1
if %errorlevel%==0 goto ready
if %TRIES% GEQ 40 goto timeout
timeout /t 1 >nul
goto waitloop

:timeout
echo [课脉] 后端没有在 40 秒内启动。窗口已保留，请查看上面的报错。
pause
exit /b 1

:ready
start "" "http://127.0.0.1:4173"
echo [课脉] 界面地址：http://127.0.0.1:4173
echo [课脉] 关闭本窗口即可停止服务。
pushd frontend
call npm run preview -- --port 4173 --strictPort
popd
"""
    LAUNCHER.write_text(content, encoding="utf-8", newline="\r\n")
    return LAUNCHER


# ── 桌面快捷方式（IShellLink，纯 ctypes） ──────────────────
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(text), ctypes.byref(self))


CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
IID_SHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
IID_PERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"


def _method(ptr, index: int, restype, *argtypes):
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])


def make_shortcut(target: Path) -> Path:
    """在桌面建 课脉.lnk，指向启动脚本，并用我们自己的图标。"""
    desktop = Path(os.path.expanduser("~/Desktop"))
    if not desktop.exists():
        desktop = Path(os.path.expanduser("~/OneDrive/Desktop"))
    desktop.mkdir(parents=True, exist_ok=True)
    link_path = desktop / "课脉.lnk"

    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    ptr = c_void_p()
    ole32.CoCreateInstance(
        ctypes.byref(GUID(CLSID_SHELLLINK)), None, 1,
        ctypes.byref(GUID(IID_SHELLLINKW)), ctypes.byref(ptr),
    )

    def call(index, restype, *argtypes, args=()):
        return _method(ptr, index, restype, *argtypes)(ptr, *args)

    call(20, ctypes.HRESULT, c_wchar_p, args=(str(target),))                 # SetPath
    call(9, ctypes.HRESULT, c_wchar_p, args=(str(target.parent),))           # SetWorkingDirectory
    call(7, ctypes.HRESULT, c_wchar_p, args=("课脉 · 课程知识体系 Agent",))    # SetDescription
    call(17, ctypes.HRESULT, c_wchar_p, c_int, args=(str(ICON), 0))          # SetIconLocation

    # QueryInterface(IID_IPersistFile) 拿到持久化接口
    persist = c_void_p()
    iid = GUID(IID_PERSISTFILE)
    _method(ptr, 0, ctypes.HRESULT, c_void_p, c_void_p)(ptr, ctypes.byref(iid), ctypes.byref(persist))
    _method(persist, 6, ctypes.HRESULT, c_wchar_p, c_int)(persist, str(link_path), 1)  # Save

    _method(persist, 2, ctypes.c_ulong)(persist)   # Release
    _method(ptr, 2, ctypes.c_ulong)(ptr)           # Release
    return link_path


def main() -> int:
    icon = make_icon()
    print("图标：", icon)
    launcher = make_launcher()
    print("启动脚本：", launcher)
    try:
        link = make_shortcut(launcher)
        print("桌面快捷方式：", link)
    except Exception as exc:  # noqa: BLE001 - 快捷方式失败不该让整个流程失败
        print("桌面快捷方式创建失败（仍可用启动脚本）：", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
