@echo off
setlocal
chcp 65001 >nul
title Mall Analysis - Quick Pipeline

set "PROJECT_ROOT=%~dp0..\"
set "PIPELINE=%~dp0scripts\run_quick_pipeline.py"

where conda >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Conda was not found in PATH.
    goto :FAILED
)
if not exist "%PIPELINE%" (
    echo [ERROR] Pipeline script was not found: %PIPELINE%
    goto :FAILED
)

echo ============================================================
echo              Mall Analysis - Quick Pipeline
echo ============================================================
echo This mode rebuilds deterministic analyses, CLR-PCA baseline,
echo analysis tables for the map. It does not refit Bayesian LNM.
echo.

cd /d "%PROJECT_ROOT%"
conda run --no-capture-output -n mall-analysis python -u "%PIPELINE%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if "%EXIT_CODE%"=="0" (
    echo.
    echo [DONE] Analysis tables: analysis\outputs\current\tables\core
) else (
    echo.
    echo [FAILED] Exit code: %EXIT_CODE%
)
echo.
pause
exit /b %EXIT_CODE%

:FAILED
echo.
pause
exit /b 1
