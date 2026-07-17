$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $bundledPython) { $python = $bundledPython }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
else { Write-Host "没有找到运行环境，请在Codex里告诉我。" -ForegroundColor Red; Read-Host "按回车关闭"; exit 1 }
Set-Location (Join-Path $root "apps\local-server")
& $python "server.py" "--open"
