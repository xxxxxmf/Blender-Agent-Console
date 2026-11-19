@echo off
chcp 65001 >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0install.py" --install-cli %*
) else (
  python "%~dp0install.py" --install-cli %*
)
pause
