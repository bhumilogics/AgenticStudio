@echo off
setlocal

where nssm >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo NSSM was not found on PATH.
    exit /b 1
)

nssm stop AgenticStudioFrontend 2>nul
nssm remove AgenticStudioFrontend confirm 2>nul

nssm stop AgenticStudioAPI 2>nul
nssm remove AgenticStudioAPI confirm 2>nul

echo AgenticStudio services removed.
pause
