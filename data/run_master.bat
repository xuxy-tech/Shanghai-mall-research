@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

echo ============================================================
echo Mall Merchant Master - Rebuild
echo ============================================================
echo.
echo Input:  ocr\outputs\*\merchant_cards.csv
echo Config: data\config\pipeline_config.json
echo Master: data\master_stores.csv
echo Audit:  data\outputs\current
echo.
echo This rebuilds derived tables only. OCR source files are not changed.
echo.

where conda >nul 2>nul
if errorlevel 1 goto no_python
conda run -n mall-analysis python -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python
set "PIPELINE_PYTHON=conda run --no-capture-output -n mall-analysis python"

:run_pipeline
echo -------------------------- START -----------------------------
%PIPELINE_PYTHON% "%CD%\data\scripts\build_master.py"
set "PIPELINE_EXIT=%ERRORLEVEL%"
echo -------------------------- FINISH ----------------------------
echo.
if not "%PIPELINE_EXIT%"=="0" goto failed
echo [SUCCESS] Master table and audit outputs were regenerated.
echo Master: %CD%\data\master_stores.csv
echo Audit:  %CD%\data\outputs\current
goto end

:no_python
echo [FAILED] Conda environment mall-analysis was not found.
set "PIPELINE_EXIT=1"
goto end

:failed
echo [FAILED] Pipeline exit code: %PIPELINE_EXIT%
echo Keep the error messages above for troubleshooting.

:end
echo.
pause
exit /b %PIPELINE_EXIT%
