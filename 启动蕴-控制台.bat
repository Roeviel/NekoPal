@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 蕴 · 猫娘伴友（控制台模式）

echo ============================================================
echo   蕴 · 猫娘伴友  —— 控制台启动
echo   （日常用「启动蕴.vbs」，这个用来排查问题）
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [!] 还没装运行环境，请先双击「安装依赖.bat」
  echo.
  pause
  exit /b 1
)

if not exist "config.json" (
  echo [!] 还没有 config.json，请先双击「安装依赖.bat」
  echo.
  pause
  exit /b 1
)

echo 正在启动... 这个窗口会显示日志，关掉它应用也会退出。
echo.
".venv\Scripts\python.exe" -m neko.app
echo.
echo 应用已退出。
pause
