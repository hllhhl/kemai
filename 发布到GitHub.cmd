@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 课脉 · 发布到 GitHub

echo.
echo  课脉 · 发布到 GitHub
echo  ------------------------------------------------
echo  这台机器访问 github.com 网页会超时，但 api.github.com 可用。
echo  所以只需要一个 Personal Access Token（勾 repo 权限），
echo  脚本会走 API 建仓库并把 249 个文件一次性提交上去。
echo.
echo  生成地址（建议在手机上打开）：
echo    https://github.com/settings/tokens  -^>  Generate new token (classic)
echo    勾选 repo，生成后复制，粘贴到下面。
echo.
echo  token 只在本次命令里使用，不会写进任何文件。
echo.

set /p TOKEN=粘贴 token 后回车：
if "%TOKEN%"=="" (
  echo [课脉] 没有输入 token，已取消。
  pause
  exit /b 1
)

set GITHUB_TOKEN=%TOKEN%
".venv\Scripts\python.exe" scripts\publish_via_api.py kemai
set RESULT=%errorlevel%
set GITHUB_TOKEN=

echo.
if %RESULT%==0 (
  echo [课脉] 发布完成。仓库地址见上面输出。
) else (
  echo [课脉] 没有成功，请把上面的报错发给我。
)
pause
