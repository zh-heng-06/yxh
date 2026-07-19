$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$target = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "apps\local-server\tools\android-platform-tools"))
$allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "apps\local-server\tools"))
if (-not $target.StartsWith($allowedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Target directory validation failed."
}

$download = Join-Path $env:TEMP "zhanggui-platform-tools.zip"
Write-Host "Downloading Google Android Platform Tools..."
Invoke-WebRequest -UseBasicParsing -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" -OutFile $download

if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
New-Item -ItemType Directory -Path $target -Force | Out-Null
Expand-Archive -LiteralPath $download -DestinationPath $target -Force
Remove-Item -LiteralPath $download -Force

$adb = Join-Path $target "platform-tools\adb.exe"
if (-not (Test-Path -LiteralPath $adb)) {
    throw "adb.exe was not found after extraction. Please check the network and retry."
}
Write-Host "Android Platform Tools installed: $adb"
Write-Host "Please restart ZhangGuiTai."
Read-Host "Press Enter to finish"
