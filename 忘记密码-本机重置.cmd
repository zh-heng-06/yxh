@echo off
chcp 65001 >nul
set "PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set /p USERNAME=请输入需要重置的用户名（默认owner）：
if "%USERNAME%"=="" set "USERNAME=owner"
cd /d "%~dp0apps\local-server"
"%PYTHON%" server.py --reset-password "%USERNAME%"
pause
