@echo off
cd /d "d:\Projects\AgenticStudio"

start "AgenticStudio Frontend" cmd /k "cd /d ""d:\Projects\AgenticStudio\frontend"" && python -m http.server 8080"
start "AgenticStudio API" cmd /k "cd /d ""d:\Projects\AgenticStudio"" && python -m uvicorn interface.api:app --reload"

echo Started AgenticStudio frontend and API.
echo Frontend: http://localhost:8080
pause
