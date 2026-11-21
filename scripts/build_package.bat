@echo off
REM ── Humanizer Build Script ─────────────────────────────────────────────────
REM Builds standalone executables using PyInstaller.
REM Prerequisites: Python 3.11+, .venv with dependencies installed, pyinstaller>=6.0
REM
REM Usage:
REM   scripts\build_package.bat              # Build both CLI and Web UI
REM   scripts\build_package.bat --clean      # Clean build directory first
REM   scripts\build_package.bat --no-upx     # Disable UPX compression

setlocal enabledelayedexpansion

cd /d "%~dp0.."

set "VENV_PYTHON=.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [ERROR] Virtual environment not found at .venv
    echo        Run: pip install -e humanizer[dev]
    pause & exit /b 1
)

rem Parse arguments
set "CLEAN_OPT="
set "NO_UPX="
for %%a in (%*) do (
    if /i "%%a"=="--clean" set "CLEAN_OPT=--clean"
    if /i "%%a"=="--no-upx" set "NO_UPX=--noupx"
)

echo ╔════════════════════════════════════════╗
echo ║  Humanizer Package Builder             ║
echo ╚════════════════════════════════════════╝
echo.

rem Check if pyinstaller is installed
"%VENV_PYTHON%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    "%VENV_PYTHON%" -m pip install -q "pyinstaller>=6.0"
)

rem Determine output directory
set "OUTPUT_DIR=%~dp0..\dist"

echo Building humanizer.exe (CLI tool)...
echo.
"%VENV_PYTHON%" -m PyInstaller --noconfirm %CLEAN_OPT% %NO_UPX% build.spec
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Check the log above.
    pause & exit /b 1
)

echo.
echo Build complete!
echo ───────────────────────────────────────
echo Output directories:
echo   dist\humanizer\       - CLI executable
echo   dist\humanizer-web\   - Web UI server
echo.
echo To run the web UI:
echo   dist\humanizer-web\humanizer-web.exe
echo.
pause
