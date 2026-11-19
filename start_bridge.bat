@echo off
chcp 65001 >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0start_bridge.py" %*
) else (
  python "%~dp0start_bridge.py" %*
)
if "%~1"=="" pause
