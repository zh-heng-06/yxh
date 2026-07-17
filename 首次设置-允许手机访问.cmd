@echo off
chcp 65001 >nul
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="掌柜台局域网系统" >nul 2>&1
netsh advfirewall firewall add rule name="掌柜台局域网系统" dir=in action=allow protocol=TCP localport=4180 profile=private
echo.
echo 已允许同一Wi-Fi内的苹果和安卓手机访问掌柜台。
pause
