@echo off
title MumEase POS Launcher
echo Starting MumEase POS...
call venv\Scripts\activate

REM شغل السيرفر في نافذة جديدة بعنوان MumEase POS Server
start "MumEase POS Server" cmd /k "uvicorn app.main:app --reload"

REM افتح المتصفح على العنوان
start http://127.0.0.1:8000/