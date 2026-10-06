@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Kaoyan Dashboard...
where python >nul 2>nul && (python server.py & goto :end)
where py >nul 2>nul && (py server.py & goto :end)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" server.py
  goto :end
)
echo [ERROR] Python not found. Install Python 3.8+ and add to PATH.
:end
pause
