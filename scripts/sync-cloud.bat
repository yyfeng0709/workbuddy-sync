@echo off
setlocal
chcp 65001 >nul
title WorkBuddy Sync via Cloud Drive (mounted)

rem ==== CLOUD TRACK ====
rem The mounted-cloud sync root (e.g. Z:\...\workbuddy via OpenList/RaiDrive)
rem is read from sync-config-cloud.txt by sync_config.py, so this batch file
rem stays pure ASCII (cmd.exe corrupts UTF-8 batch parsing; never put
rem non-ASCII text in .bat files).
set "WB_SYNC_CONFIG=%~dp0sync-config-cloud.txt"

if not exist "%WB_SYNC_CONFIG%" (
    echo [ERROR] sync-config-cloud.txt not found next to this script.
    echo         Create it with one line:  NAS_ROOT=Z:\your-mount\workbuddy
    goto :end
)

set PYTHON=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe
set DIR=%~dp0

if not exist "%PYTHON%" (
    echo [WARN] Managed Python not found, falling back to system python...
    set PYTHON=python
)

echo ============================================================
echo   WorkBuddy One-Click Sync + Merge  [CLOUD TRACK]
echo   Relay: mounted cloud drive (see sync-config-cloud.txt)
echo   %DATE% %TIME%
echo ============================================================
echo.

echo [STEP 1/3] Bidirectional file sync (identity / memory / tasks / scripts / config / workspace)
echo ------------------------------------------------------------
"%PYTHON%" -u "%DIR%sync-bidirectional.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Step 1 failed. Aborting. Check gateway / cloud drive status.
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
echo   ALL DONE. CLOUD-track Sync + Merge + Report completed.
echo   Task report: %USERPROFILE%\WorkBuddy\TaskReports\tasks-report.html
echo   NOTE: wait for the gateway upload queue to drain before
echo         shutting down this PC (cloud relay is async).
echo ============================================================

:end
echo.
pause
endlocal
