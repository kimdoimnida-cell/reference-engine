@echo off
chcp 65001 >nul
title 레퍼런스 생성기 서버
cd /d "%~dp0"
echo.
echo   레퍼런스 생성기 서버를 켭니다...
echo   잠시 후 브라우저가 열립니다. (이 창은 켜두세요)
echo.
start "" chrome.exe "http://127.0.0.1:8765"
python server\app.py
pause
