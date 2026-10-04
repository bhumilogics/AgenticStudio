@echo off
setlocal

set "ROOT=d:\Projects\AgenticStudio"
set "FRONTEND_DIR=%ROOT%\frontend"

REM Prefer the Python executable on PATH; if missing, fall back to a common Windows install location.
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
) else (
    for /f "delims=" %%I in ('where python') do set "PYTHON_EXE=%%I"
)

if not exist "%PYTHON_EXE%" (
    echo Python was not found on PATH or in the common Windows install folder.
    echo Install Python first, then run this script again.
    exit /b 1
)

where nssm >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo NSSM was not found on PATH.
    echo Download it from: https://nssm.cc/download
    echo Then place nssm.exe in a folder on PATH or next to this script.
    exit /b 1
)

REM Frontend service
nssm install AgenticStudioFrontend "%PYTHON_EXE%" -m http.server 8080
nssm set AgenticStudioFrontend AppDirectory "%FRONTEND_DIR%"
nssm set AgenticStudioFrontend Description "AgenticStudio frontend on http://localhost:8080"
nssm set AgenticStudioFrontend Start SERVICE_AUTO_START
nssm start AgenticStudioFrontend

REM API service
nssm install AgenticStudioAPI "%PYTHON_EXE%" -m uvicorn interface.api:app --reload
nssm set AgenticStudioAPI AppDirectory "%ROOT%"
nssm set AgenticStudioAPI Description "AgenticStudio FastAPI backend"
nssm set AgenticStudioAPI Start SERVICE_AUTO_START
nssm start AgenticStudioAPI

echo.
echo AgenticStudio services installed.
echo Frontend: http://localhost:8080
sc query AgenticStudioFrontend
sc query AgenticStudioAPI
pause
