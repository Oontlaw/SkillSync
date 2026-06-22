@echo off
title SkillSync Stopper
echo Stopping all SkillSync services...

taskkill /F /IM python.exe /T

echo.
echo ==================================================
echo  All Python services have been stopped.
echo ==================================================
pause