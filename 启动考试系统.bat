@echo off
chcp 65001 >nul
title Linux Command Exam System
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  pause
  exit /b 1
)

echo Starting exam server and Cloudflare tunnel...
python start_all.py
echo.
echo Server stopped. Press any key to close.
pause >nul
