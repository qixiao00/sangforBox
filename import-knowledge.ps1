$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Run setup.ps1 first.' }
& .\.venv\Scripts\python.exe -m af_app.import_knowledge --source knowledge\processed
if ($LASTEXITCODE -ne 0) { throw 'Knowledge import failed.' }
