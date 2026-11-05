@echo off
setlocal
chcp 65001 >nul
title WorkBuddy One-Click Sync + Merge

set PYTHON=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe
set DIR=%~dp0

if not exist "%PYTHON%" (
    echo [WARN] Managed Python not found, falling back to system python...
    set PYTHON=python
)

echo ============================================================
echo   WorkBuddy One-Click Sync + Merge
echo   %DATE% %TIME%
echo ============================================================
echo.

echo [STEP 1/3] Bidirectional file sync (identity / memory / tasks / scripts / config / workspace)
echo ------------------------------------------------------------
"%PYTHON%" -u "%DIR%sync-bidirectional.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Step 1 failed. Aborting. Check NAS path / network.
    goto :end
)
echo.

echo [STEP 2/3] Merge session index into workbuddy.db (fixes /resume list)
echo ------------------------------------------------------------
"%PYTHON%" -u "%DIR%sessions-merge.py"
if errorlevel 1 (
    echo.
    echo [WARN] Step 2 failed. File sync done, but session index not merged.
    echo        /resume may not show the other PC's sessions.
    goto :end
)
echo.

echo [STEP 3/3] Regenerate global task report (HTML)
echo ------------------------------------------------------------
"%PYTHON%" -u "%DIR%tasks-report.py"
if errorlevel 1 (
    echo.
    echo [WARN] Step 3 failed, but sync + merge succeeded.
    goto :end
)
echo.

echo ============================================================
echo   ALL DONE. Sync + Merge + Report completed.
echo   Task report: %USERPROFILE%\WorkBuddy\TaskReports\tasks-report.html
echo   TIP: Reopen WorkBuddy to see the merged sessions/tasks.
echo ============================================================

:end
echo.
pause
endlocal
