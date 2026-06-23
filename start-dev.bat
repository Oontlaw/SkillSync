@echo off
title SkillSync Dev Mode
echo Starting SkillSync Services (Development Mode with Debugger)...
echo.

:: Start Flask with debug mode
echo [1/3] Starting Flask Server (debug + insecure cookies for localhost)...
start /b cmd /c "set FLASK_ENV=development && .\.venv\Scripts\python.exe app.py"

:: Start Bot
echo [2/3] Starting Discord Bot...
start /b .\.venv\Scripts\python.exe bot.py

echo [3/3] Bot started. No tunnel — localhost only.
echo.
echo ==================================================
echo  SkillSync is running in DEVELOPMENT MODE!
echo  Access: http://localhost:5000
echo  Debugger active — DO NOT expose to the internet
echo ==================================================
pause
