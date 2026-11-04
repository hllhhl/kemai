@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 课脉 · 发布到 GitHub

where gh >nul 2>&1
if errorlevel 1 (
  echo [课脉] 没找到 GitHub CLI。
  echo   安装命令：winget install --id GitHub.cli
  pause
  exit /b 1
)

echo [课脉] 检查 GitHub 登录状态...
gh auth status >nul 2>&1
if errorlevel 1 (
  echo [课脉] 需要登录。浏览器会打开授权页，请在自己的账号里确认。
  gh auth login -h github.com -p https -w
  if errorlevel 1 ( echo [课脉] 登录没有完成。 & pause & exit /b 1 )
)

for /f "delims=" %%i in ('gh api user --jq .login') do set GHUSER=%%i
if "%GHUSER%"=="" ( echo [课脉] 拿不到 GitHub 账号名。 & pause & exit /b 1 )
echo [课脉] 账号：%GHUSER%

gh repo view %GHUSER%/kemai >nul 2>&1
if errorlevel 1 (
  echo [课脉] 创建仓库并推送...
  gh repo create kemai --public --source=. --remote=origin --push
) else (
  echo [课脉] 仓库已存在，直接推送...
  git remote remove origin >nul 2>&1
  git remote add origin https://github.com/%GHUSER%/kemai.git
  git push -u origin master
)

if errorlevel 1 (
  echo [课脉] 推送失败，请看上面的报错。
  pause
  exit /b 1
)
echo.
echo [课脉] 完成：https://github.com/%GHUSER%/kemai
pause
