@echo off
title SkillSync Starter
echo Starting SkillSync Services...

:: Start Flask (production mode — no debugger, Secure cookies)
echo [1/3] Starting Flask Server...
start /b cmd /c ".\.venv\Scripts\python.exe app.py"

:: Start Bot
echo [2/3] Starting Discord Bot...
start /b .\.venv\Scripts\python.exe bot.py

:: Start Public Tunnel
echo [3/3] Starting ngrok Public Tunnel...
start /b %LOCALAPPDATA%\ngrok\ngrok.exe http 5000

echo.
echo ==================================================
echo  SkillSync is now running in the background!
echo  Local: http://localhost:5000
echo  Public: check http://127.0.0.1:4040 for the ngrok URL
echo ==================================================
pause