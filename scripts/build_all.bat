@echo off
chcp 65001 >nul
REM ════════════════════════════════════════════════════════════════════════════
REM Humanizer Complete Package Builder
REM Builds: .exe files + optional Windows installer (.msi/.exe)
REM ════════════════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo ╔════════════════════════════════════════════════╗
echo ║   Humanizer 安装包构建器 v0.1.0              ║
echo ╚════════════════════════════════════════════════╝
echo.

REM ── 检查虚拟环境 ─────────────────────────────────────────────────────────────
set "VENV_PYTHON=.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [ERROR] 虚拟环境不存在！
    echo        请先运行: pip install -e humanizer[dev]
    pause & exit /b 1
)
echo [OK] Python 环境就绪
"%VENV_PYTHON%" --version

REM ── Step 1: 构建可执行文件 ──────────────────────────────────────────────────
echo.
echo ═════════════════════════════════════════════════
echo Step 1/2: 构建 PyInstaller 可执行文件
echo ═════════════════════════════════════════════════
echo.

REM 确保 pyinstaller 已安装
"%VENV_PYTHON%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] 正在安装 PyInstaller...
    "%VENV_PYTHON%" -m pip install -q "pyinstaller>=6.0"
)

REM 清理旧构建
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
mkdir dist

echo [INFO] 开始构建...
"%VENV_PYTHON%" -m PyInstaller --noconfirm build.spec
if errorlevel 1 (
    echo [ERROR] 构建失败！请检查上方错误信息。
    pause & exit /b 1
)

echo [OK] 构建完成！
echo      - dist\humanizer.exe      (CLI 命令行工具)
echo      - dist\humanizer-web.exe  (Web UI 服务)
echo.

REM ── Step 2: 创建安装包（可选）───────────────────────────────────────────────
echo ═════════════════════════════════════════════════
echo Step 2/2: 创建 Windows 安装包
echo ═════════════════════════════════════════════════
echo.

REM 检测 Inno Setup
set "ISCC_FOUND="
set "ISCC_PATH="
for %%p in (
    "C:\Program Files\Inno Setup 6\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) do (
    if exist "%%p" (
        set "ISCC_FOUND=yes"
        set "ISCC_PATH=%%p"
        goto :found_iscc
    )
)
:found_iscc

if not defined ISCC_FOUND (
    echo [WARNING] Inno Setup 未检测到！
    echo.
    echo 要生成 .exe 安装包，请安装 Inno Setup:
    echo   https://jrsoftware.org/isdl.php
    echo.
    echo 当前可用的输出文件:
    echo   • dist\humanizer.exe      - CLI 命令行版本
    echo   • dist\humanizer-web.exe  - Web UI 服务端
    echo.
    echo 或者你可以手动创建一个便携版压缩包:
    echo   7z a Humanizer_v0.1.0.zip dist\*
    goto :finish
)

echo [OK] 检测到 Inno Setup: %ISCC_PATH%
echo [INFO] 正在生成安装包...
echo.

"%ISCC_PATH%" "setup-scripts\HumanizerSetup.iss"
if errorlevel 1 (
    echo [ERROR] 安装包构建失败！
    goto :finish
)

:finish
echo.
echo ╔════════════════════════════════════════════════╗
echo ║   构建完成！                                   ║
echo ╠════════════════════════════════════════════════╣
echo ║                                              ║
echo ║  输出位置: dist\                            ║
echo ║                                              ║
echo ║  可执行文件:                                 ║
echo ║    • humanizer.exe (CLI 命令行)             ║
echo ║    • humanizer-web.exe (Web UI 服务)       ║
echo ║                                              ║
echo ║  安装包:                                     ║
echo ║    • Humanizer_Setup_0.1.0.exe (如已生成)  ║
echo ║                                              ║
echo ║  使用方式:                                   ║
echo ║    双击 humanizer-web.exe 启动 Web UI      ║
echo ║    然后访问 http://localhost:8000           ║
echo ║                                              ║
echo ╚════════════════════════════════════════════════╝
echo.

REM 询问是否打开输出目录
set /p OPEN="是否打开输出文件夹？(Y/N): "
if /i "%OPEN%"=="Y" explorer dist

pause
