@echo off
chcp 65001 >nul
title Hyperliquid 고래 모니터
cd /d "%~dp0"
:loop
py -3 monitor.py
echo.
echo 모니터가 종료되었습니다. 10초 후 자동 재시작... (창을 닫으면 중지)
timeout /t 10 >nul
goto loop
