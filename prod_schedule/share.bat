@echo off
echo ============================================================
echo  Sharing Production Schedule via ngrok tunnel
echo ============================================================
echo.
echo  FIRST TIME ONLY: You need a free ngrok account.
echo  1. Go to https://ngrok.com and sign up (free)
echo  2. Copy your authtoken from the dashboard
echo  3. Run this once:  ngrok config add-authtoken YOUR_TOKEN
echo.
echo  Starting tunnel on port 5000...
echo  A URL like https://xxxx.ngrok-free.app will appear below.
echo  Share that URL with your colleague.
echo.
ngrok http 5000
pause
