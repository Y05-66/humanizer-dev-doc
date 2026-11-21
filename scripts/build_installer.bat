@echo off
chcp 65001 >nul
title Humanizer 安装包构建器
cd /d "%~dp0.."

echo ============================================================
echo    Humanizer  Windows 安装包构建工具
echo ============================================================
echo.

REM Check Python
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到虚拟环境: %PY%
    echo 请先运行: pip install -e humanizer[dev]
    pause
    exit /b 1
)
echo [OK] Python 已就绪

REM Check/install PyInstaller
"%PY%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [安装] PyInstaller...
    "%PY%" -m pip install -q pyinstaller>=6.0
)

REM Build executables
echo [构建] PyInstaller...
if exist dist rmdir /s /q dist
"%PY%" -m PyInstaller --noconfirm build.spec
if errorlevel 1 (
    echo [错误] 构建失败！
    pause
    exit /b 1
)
echo [OK] 可执行文件已生成

REM Check Inno Setup
set "ISCC="
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo.
    echo [提示] 未检测到 Inno Setup
    echo 下载地址: https://jrsoftware.org/isdl.php
    echo.
    echo 可选方案:
    echo   1. 使用便携版: dist\Humanizer_Portable_v0.1.0.zip
    echo   2. 安装 Inno Setup 后重新运行此脚本
    echo.
    explorer dist
    pause
    exit /b 0
)

echo [OK] Inno Setup 已找到: %ISCC%
echo [构建] Windows 安装包...
"%ISCC%" setup-scripts\HumanizerSetup.iss
if errorlevel 1 (
    echo [错误] 安装包构建失败
    pause
    exit /b 1
)

echo.
echo ============================================================
echo    构建完成！
echo ============================================================
echo.
echo 输出目录: dist\
echo.
echo 可执行文件:
echo   - humanizer.exe        (CLI 命令行工具)
echo   - humanizer-web.exe    (Web UI 服务)
echo.
echo 安装包:
echo   - Humanizer_Setup_0.1.0.exe
echo.
echo 便携包:
echo   - Humanizer_Portable_v0.1.0.zip
echo.
explorer dist
pause
