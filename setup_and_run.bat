@echo off
echo === Pixelebbe Bot ===

docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not running. Start Docker Desktop first.
    pause
    exit /b 1
)

echo Building image (only slow on first run)...
docker compose build

echo.
echo === Starting bot at http://localhost:5001 ===
docker compose up
