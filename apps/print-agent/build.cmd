@echo off
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
"%CSC%" /nologo /target:exe /optimize+ /out:"%~dp0LabelPrinter.exe" /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "%~dp0LabelPrinter.cs"
if errorlevel 1 exit /b 1
echo Label printer build complete.
