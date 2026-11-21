@echo off
chcp 65001 >nul
REM Use venv python directly -- bypasses activate and avoids PATH conflicts
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] .venv not found
    pause
    exit /b 1
)
set PYTHONPATH=%~dp0src
"%PY%" "%~dp0run.py" %*
pause
