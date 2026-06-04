@echo off
cd /d "%~dp0"
echo Starting Production Schedule...
echo.

REM Start Flask in the background
start "Flask Server" /min python app.py

REM Wait for Flask to come up
timeout /t 2 /nobreak >nul

REM Open local browser
start "" "http://localhost:5000"

echo ============================================================
echo  Local access:    http://localhost:5000
echo  Network access:  http://10.5.0.2:5000
echo                   (only works if admin has opened port 5000)
echo.
echo  To share with colleagues WITHOUT needing admin,
echo  run share.bat in a second window.
echo ============================================================
echo.
pause
