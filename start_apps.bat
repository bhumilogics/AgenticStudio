@echo off
cd /d "%~dp0"

start "AgenticStudio Frontend" cmd /k "cd /d ""%~dp0frontend"" && python -m http.server 8080"
start "AgenticStudio API" cmd /k "cd /d ""%~dp0"" && python -m uvicorn interface.api:app --host 0.0.0.0 --port 8000 --reload"

echo Started AgenticStudio frontend and API.
echo Frontend: http://localhost:8080
pause
