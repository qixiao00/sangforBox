$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend')

$BundledPnpm = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd'
$Pnpm = if (Get-Command pnpm -ErrorAction SilentlyContinue) { 'pnpm' } elseif (Test-Path -LiteralPath $BundledPnpm) { $BundledPnpm } else { $null }
if (-not $Pnpm) { throw 'pnpm is required to build the Next.js frontend.' }

& $Pnpm install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
& $Pnpm run build:app
if ($LASTEXITCODE -ne 0) { throw 'Next.js build failed.' }
