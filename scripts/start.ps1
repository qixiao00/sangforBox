param([int]$Port = 8765, [string]$BindAddress = '127.0.0.1')
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $ProjectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Run scripts\setup.ps1 first.' }
& .\.venv\Scripts\python.exe -m uvicorn af_app.app:create_app --factory --host $BindAddress --port $Port
