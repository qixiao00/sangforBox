$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $ProjectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Run scripts\setup.ps1 first.' }
& .\.venv\Scripts\python.exe -m af_app.import_knowledge --source knowledge\processed
if ($LASTEXITCODE -ne 0) { throw 'Knowledge import failed.' }
