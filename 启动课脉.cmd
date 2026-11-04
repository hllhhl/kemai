@echo off
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
