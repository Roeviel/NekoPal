@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 蕴 · 猫娘伴友 —— 安装

echo ============================================================
echo   蕴 · 猫娘伴友  —— 首次安装
echo ============================================================
echo.

rem ---------- 找一个可用的 Python ----------
set "BASEPY="
where python >nul 2>nul && set "BASEPY=python"
if not defined BASEPY (
  where py >nul 2>nul && set "BASEPY=py -3"
)
if not defined BASEPY (
  echo [X] 没找到 Python。
  echo     请先到 https://www.python.org/downloads/ 安装 Python 3.10 以上，
  echo     安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)
echo [1/6] 使用 Python：
%BASEPY% --version
echo.

rem ---------- 虚拟环境 ----------
if exist ".venv\Scripts\python.exe" (
  echo [2/6] 虚拟环境已存在，跳过创建
) else (
  echo [2/6] 创建虚拟环境 .venv ...
  %BASEPY% -m venv .venv
  if errorlevel 1 (
    echo [X] 创建虚拟环境失败。
    pause
    exit /b 1
  )
)
echo.

rem ---------- 依赖 ----------
rem 直连 pypi.org 在国内经常超时，所以默认走 USTC 镜像；
rem 缓存也放到项目目录里，避免个别机器上用户缓存目录不可写。
set "PIP_CACHE_DIR=%~dp0.pip-cache"
set "MIRROR=https://mirrors.ustc.edu.cn/pypi/simple"

echo [3/6] 安装依赖（需要联网，约 1-3 分钟）...
".venv\Scripts\python.exe" -m pip install --upgrade pip --no-cache-dir -i %MIRROR% --timeout 30 --disable-pip-version-check -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt --no-cache-dir -i %MIRROR% --timeout 30 --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo [X] 依赖安装失败。
  echo     如果提示连接超时，可以手动换镜像重试，例如：
  echo       .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
  echo.
  pause
  exit /b 1
)
echo.

rem ---------- 配置 ----------
echo [4/6] 准备配置文件...
if not exist "config.json" (
  copy /y "config.example.json" "config.json" >nul
  echo      已从模板生成 config.json
) else (
  echo      config.json 已存在，保留你原来的设置
)
echo.

rem ---------- 自检 ----------
echo [5/6] 环境自检...
".venv\Scripts\python.exe" tools\selfcheck.py --quick
echo.

echo ============================================================
echo   安装完成
echo ============================================================
echo.
echo   还差最后一步：填 DeepSeek API Key
echo     打开 config.json，把 llm.api_key 改成你的 Key
echo     （到 https://platform.deepseek.com 申请）
echo.
echo   填完之后，双击「启动蕴.vbs」就能用了。
echo.
choice /c YN /m "现在打开 config.json 填写吗"
if errorlevel 2 goto :done
start "" notepad "config.json"
:done
echo.
pause


echo [6/6] 在桌面创建快捷方式...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\make_shortcut.ps1"