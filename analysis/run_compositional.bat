@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title Compositional Factor Analysis

set "PROJECT_ROOT=%~dp0..\"
set "SCRIPT=%~dp0scripts\compositional_analysis.py"
set "PACKAGE_SCRIPT=%~dp0scripts\package_results.py"
cd /d "%PROJECT_ROOT%"

where conda >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Conda was not found in PATH.
    goto :FAILED
)
call conda activate mall-analysis >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Conda environment mall-analysis was not found.
    goto :FAILED
)
set "PYTHON_ENV=%CONDA_PREFIX%\python.exe"

rem PyTensor 编译目录必须是纯 ASCII 路径：项目路径含中文会导致读取编译器输出时解码失败
set "PYTENSOR_COMPILEDIR=C:\temp\pytensor_compile"
if not exist "%PYTENSOR_COMPILEDIR%" mkdir "%PYTENSOR_COMPILEDIR%" 2>nul

rem Conda 环境里的 mingw-w64 g++；缺失时 PyTensor 退回纯 Python，速度会慢很多
set "MINGW_BIN=%CONDA_PREFIX%\Library\mingw-w64\bin"
if exist "%MINGW_BIN%\g++.exe" (
    set "PATH=%MINGW_BIN%;%PATH%"
    set "CXX_STATUS=g++ available"
) else (
    set "CXX_STATUS=no g++ - will be slow"
)

set "PYTENSOR_FLAGS=compiledir=%PYTENSOR_COMPILEDIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo ============================================================
echo            Compositional Factor Analysis
echo ============================================================
echo Project : %PROJECT_ROOT%
echo Compiler: %CXX_STATUS%
echo Compile : %PYTENSOR_COMPILEDIR%
echo.

if not exist "%PYTHON_ENV%" (
    echo [ERROR] Python environment not found: %PYTHON_ENV%
    goto :FAILED
)
if not exist "%SCRIPT%" (
    echo [ERROR] Script not found: %SCRIPT%
    goto :FAILED
)

rem 默认参数：ADVI 变分推断，K=1..4。可在命令行追加参数覆盖，例如：
rem   analysis\run_compositional.bat --inference nuts --draws 1000
rem   analysis\run_compositional.bat --skip-lnm
set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=--inference advi --advi-iterations 20000 --draws 800"

echo Running: %ARGS%
echo Progress is printed step by step. Do not close this window.
echo -------------------------- START -----------------------------
echo.

"%PYTHON_ENV%" -X utf8 -u "%SCRIPT%" %ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" (
    "%PYTHON_ENV%" -X utf8 -u "%PACKAGE_SCRIPT%" --output-dir "%PROJECT_ROOT%analysis\outputs\current"
    set "EXIT_CODE=!ERRORLEVEL!"
)

echo.
echo -------------------------- FINISH ----------------------------
if "%EXIT_CODE%"=="0" (
    echo [DONE] Results written to analysis\outputs\current\
    echo   tables\core\mall_profiles.csv        per-mall map profile
    echo   tables\core\compositional_factor_loadings.csv
    echo   intermediate\compositional_factor_scores.csv
    echo   machine\compositional_results.json
) else (
    echo [FAILED] exit code: %EXIT_CODE%
)
echo.
pause
exit /b %EXIT_CODE%

:FAILED
echo.
pause
exit /b 1
