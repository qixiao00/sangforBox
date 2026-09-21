param(
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'SangforBox-Portable')
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run setup.ps1 first.' }

$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$OutputFullPath = [IO.Path]::GetFullPath($OutputDirectory)
$OutputParent = Split-Path -Parent $OutputFullPath
if ($OutputFullPath -eq $ProjectRoot -or $OutputFullPath -eq $OutputParent -or [IO.Path]::GetPathRoot($OutputFullPath) -eq $OutputFullPath) {
    throw 'OutputDirectory must be a dedicated non-root directory.'
}

$BuildRoot = Join-Path $ProjectRoot '.portable-build'
$SeedDirectory = Join-Path $BuildRoot 'seed'
$PyInstallerWork = Join-Path $BuildRoot 'work'
$PyInstallerDist = Join-Path $BuildRoot 'dist'
$SeedDatabase = Join-Path $SeedDirectory 'knowledge.db'

if (Test-Path -LiteralPath $BuildRoot) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
New-Item -ItemType Directory -Path $SeedDirectory -Force | Out-Null

$env:AF_DATABASE_PATH = $SeedDatabase
& $Python -m af_app.import_knowledge --source (Join-Path $ProjectRoot 'knowledge\processed')
if ($LASTEXITCODE -ne 0) { throw 'Failed to build clean knowledge database.' }
Remove-Item Env:AF_DATABASE_PATH

$DeviceCount = & $Python -c "import sqlite3; c=sqlite3.connect(r'$SeedDatabase'); print(c.execute('select count(*) from device_profiles').fetchone()[0]); c.execute('pragma wal_checkpoint(truncate)'); c.close()"
if ($LASTEXITCODE -ne 0 -or [int]$DeviceCount -ne 0) { throw 'Portable seed database contains device profiles or could not be verified.' }

& $Python -m PyInstaller --noconfirm --clean --onefile --name SangforBox `
    --distpath $PyInstallerDist --workpath $PyInstallerWork --specpath $BuildRoot `
    --collect-data mcp `
    --hidden-import win32crypt --hidden-import logging.handlers `
    --add-data "$(Join-Path $ProjectRoot 'af_app\static');af_app/static" `
    --add-data "$SeedDatabase;assets" `
    (Join-Path $ProjectRoot 'af_app\portable.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

if (Test-Path -LiteralPath $OutputFullPath) {
    $ExistingManifest = Join-Path $OutputFullPath 'manifest.json'
    $ExistingItems = @(Get-ChildItem -LiteralPath $OutputFullPath -Force)
    $OwnedOutput = $false
    if (Test-Path -LiteralPath $ExistingManifest) {
        try { $OwnedOutput = ((Get-Content -LiteralPath $ExistingManifest -Raw | ConvertFrom-Json).product -eq 'SangforBox') } catch { $OwnedOutput = $false }
    }
    if ($ExistingItems.Count -gt 0 -and -not $OwnedOutput) {
        throw 'Refusing to replace a nonempty directory that is not a SangforBox portable output.'
    }
    Remove-Item -LiteralPath $OutputFullPath -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputFullPath -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutputFullPath 'data') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutputFullPath 'results') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutputFullPath 'logs') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutputFullPath 'WorkBuddy') -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $PyInstallerDist 'SangforBox.exe') -Destination $OutputFullPath
& (Join-Path $ProjectRoot 'package-workbuddy.ps1')
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'dist\sangforbox-workbuddy-connector.zip') -Destination (Join-Path $OutputFullPath 'WorkBuddy')
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'dist\af-app-control-optimization-skill.zip') -Destination (Join-Path $OutputFullPath 'WorkBuddy')
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'connector\workbuddy\docs\af-initialization-engineer-guide.md') -Destination (Join-Path $OutputFullPath 'WorkBuddy')
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'PORTABLE-README.txt') -Destination $OutputFullPath

$Manifest = [ordered]@{
    product = 'SangforBox'
    package_version = '1.0.4'
    built_at = [DateTime]::UtcNow.ToString('o')
    architecture = $env:PROCESSOR_ARCHITECTURE
    executable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputFullPath 'SangforBox.exe')).Hash.ToLowerInvariant()
    connector_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputFullPath 'WorkBuddy\sangforbox-workbuddy-connector.zip')).Hash.ToLowerInvariant()
    optimization_skill_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputFullPath 'WorkBuddy\af-app-control-optimization-skill.zip')).Hash.ToLowerInvariant()
}
$Manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputFullPath 'manifest.json') -Encoding utf8
$ArchivePath = "$OutputFullPath.zip"
if (Test-Path -LiteralPath $ArchivePath) { Remove-Item -LiteralPath $ArchivePath -Force }
Compress-Archive -Path (Join-Path $OutputFullPath '*') -DestinationPath $ArchivePath -CompressionLevel Optimal
if (-not $BuildRoot.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unsafe build cleanup path.'
}
Remove-Item -LiteralPath $BuildRoot -Recurse -Force
Write-Host "Portable package created: $OutputFullPath"
Write-Host "Transfer archive created: $ArchivePath"
