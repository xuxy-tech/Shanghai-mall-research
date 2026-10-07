@echo off
setlocal
chcp 65001 >nul
title Mall Video OCR Batch

set "PROJECT_ROOT=%~dp0..\"
set "PIPELINE_SCRIPT=%~dp0scripts\run_batch.ps1"
set "INPUT_DIR=%~dp0input_videos"

cd /d "%PROJECT_ROOT%"

echo ============================================================
echo                    Mall Video OCR Batch
echo ============================================================
echo Project: %PROJECT_ROOT%
echo.

if not exist "%PIPELINE_SCRIPT%" (
    echo [ERROR] PowerShell batch script was not found:
    echo %PIPELINE_SCRIPT%
    goto :FAILED_SETUP
)

where conda >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Conda was not found in PATH.
    goto :FAILED_SETUP
)
conda run -n mall-analysis python -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Conda environment mall-analysis was not found.
    goto :FAILED_SETUP
)

if not exist "%INPUT_DIR%\*.mp4" (
    echo [ERROR] No MP4 files were found in:
    echo %INPUT_DIR%
    goto :FAILED_SETUP
)

if /i "%~1"=="--check" (
    echo [OK] Launcher, Python environment, and input videos are ready.
    exit /b 0
)

echo Compute: unified mall-analysis Conda environment.
echo.
echo Existing malls with merchant_cards.csv will be skipped.
echo New videos will run one by one. OCR progress appears below.
echo Do not close this window or let the computer sleep.
echo.
echo -------------------------- START -----------------------------
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PIPELINE_SCRIPT%"
set "OCR_EXIT_CODE=%ERRORLEVEL%"

echo.
echo -------------------------- FINISH ----------------------------
if "%OCR_EXIT_CODE%"=="0" (
    echo [DONE] Batch processing completed successfully.
) else (
    echo [FAILED] Batch process exit code: %OCR_EXIT_CODE%
    echo Keep the error messages above for troubleshooting.
)
echo.
pause
exit /b %OCR_EXIT_CODE%

:FAILED_SETUP
echo.
echo Startup checks failed. OCR was not started.
echo.
pause
exit /b 1
