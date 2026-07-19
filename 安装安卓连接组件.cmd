@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装安卓连接组件.ps1"
if errorlevel 1 (
  echo.
  echo 安装失败，请把上面的错误内容发给开发人员。
  pause
)
