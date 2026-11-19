@echo off
chcp 65001 >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0install.py" --uninstall %*
) else (
  python "%~dp0install.py" --uninstall %*
)
pause
