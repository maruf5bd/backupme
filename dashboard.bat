@echo off
title BackupMe Dashboard
chcp 65001 >nul 2>nul
cd /d "%~dp0"
set "PATH=%PATH%;%APPDATA%\Python\Python313\Scripts"

rem If the dashboard is already running, just open the browser.
netstat -ano 2>nul | findstr /c:":8081" | findstr /c:"LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo BackupMe dashboard already running at http://127.0.0.1:8081
    start "" http://127.0.0.1:8081
    exit /b 0
)

echo Starting BackupMe dashboard at http://127.0.0.1:8081 ...
echo Close this window to stop the dashboard.
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8081"
backupme web --host 127.0.0.1 --port 8081
