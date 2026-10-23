@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ============================================================
echo  Open-method diagnostic for YouTube2MD
echo ============================================================
echo.
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo         Please run this inside the youtube2md_lab folder.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" diag_open.py
echo.
echo ------------------------------------------------------------
echo  Done. Log file:
echo    %~dp0..\diag_open.log
echo  Please send the content of that file.
echo ------------------------------------------------------------
pause