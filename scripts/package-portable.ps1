param(
    [string]$OutputDirectory = ''
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $ProjectRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path -Parent $ProjectRoot) 'SangforBox-Portable'
}

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run scripts\setup.ps1 first.' }

# Keep this script runnable on its own: install the packaging dependency
# declared in requirements-build.txt when the virtual environment lacks it.
$PyInstallerReady = $false
try {
    & $Python -c "import PyInstaller" *> $null
    $PyInstallerReady = ($LASTEXITCODE -eq 0)
} catch {
    $PyInstallerReady = $false
}
if (-not $PyInstallerReady) {
    Write-Host 'Installing build dependency from requirements-build.txt ...'
    try {
        & $Python -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements-build.txt') *> $null
    } catch {
        throw 'Failed to install PyInstaller from requirements-build.txt.'
    }
    & $Python -c "import PyInstaller" *> $null
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller is still unavailable after installation.' }
    Write-Host 'PyInstaller installed.'
}

$OutputFullPath = [IO.Path]::GetFullPath($OutputDirectory)
$OutputParent = Split-Path -Parent $OutputFullPath
if ($OutputFullPath -eq $ProjectRoot -or $OutputFullPath -eq $OutputParent -or [IO.Path]::GetPathRoot($OutputFullPath) -eq $OutputFullPath) {
    throw 'OutputDirectory must be a dedicated non-root directory.'
}

# Each build uses a unique temp workspace: nothing has to be cleared first, and
# repeated or concurrent builds cannot collide. Keep this file ASCII-only so
# Windows PowerShell 5.1 parses it correctly without a UTF-8 BOM.
$BuildRoot = Join-Path ([IO.Path]::GetTempPath()) ("sangforbox-portable-{0}" -f [guid]::NewGuid().ToString('N').Substring(0, 8))
$SeedDirectory = Join-Path $BuildRoot 'seed'
$PyInstallerWork = Join-Path $BuildRoot 'work'
$PyInstallerDist = Join-Path $BuildRoot 'dist'
$SeedDatabase = Join-Path $SeedDirectory 'knowledge.db'

New-Item -ItemType Directory -Path $SeedDirectory -Force | Out-Null

$env:AF_DATABASE_PATH = $SeedDatabase
$env:AF_OUTPUT_DIR = (Join-Path $BuildRoot 'results')
$env:AF_STATE_DIR = (Join-Path $BuildRoot 'state')
& $Python -m af_app.import_knowledge --source (Join-Path $ProjectRoot 'knowledge\processed')
if ($LASTEXITCODE -ne 0) { throw 'Failed to build clean knowledge database.' }
Remove-Item Env:AF_DATABASE_PATH
Remove-Item Env:AF_OUTPUT_DIR
Remove-Item Env:AF_STATE_DIR

if (-not (Test-Path -LiteralPath $SeedDatabase)) { throw 'Seed database was not created at the isolated build path.' }
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

# Move an existing output directory aside instead of deleting it recursively:
# the previous build is preserved and unrelated directories are never removed.
# A directory is recognised as ours by its manifest, by a partial build marker,
# or by the packaged executable, so an interrupted build can be retried.
$ReplacedOutput = $null
if (Test-Path -LiteralPath $OutputFullPath) {
    $ExistingManifest = Join-Path $OutputFullPath 'manifest.json'
    $ExistingItems = @(Get-ChildItem -LiteralPath $OutputFullPath -Force)
    $OwnedOutput = Test-Path -LiteralPath (Join-Path $OutputFullPath '.sangforbox-package')
    if (-not $OwnedOutput -and (Test-Path -LiteralPath (Join-Path $OutputFullPath 'SangforBox.exe'))) { $OwnedOutput = $true }
    if (-not $OwnedOutput -and (Test-Path -LiteralPath $ExistingManifest)) {
        try { $OwnedOutput = ((Get-Content -LiteralPath $ExistingManifest -Raw | ConvertFrom-Json).product -eq 'SangforBox') } catch { $OwnedOutput = $false }
    }
    if ($ExistingItems.Count -gt 0 -and -not $OwnedOutput) {
        throw 'Refusing to replace a nonempty directory that is not a SangforBox portable output.'
    }
    $ReplacedOutput = "{0}.previous-{1}" -f $OutputFullPath, (Get-Date -Format 'yyyyMMdd-HHmmss')
    Move-Item -LiteralPath $OutputFullPath -Destination $ReplacedOutput
    Write-Host "Previous output moved aside: $ReplacedOutput"
}
New-Item -ItemType Directory -Path $OutputFullPath -Force | Out-Null
# Marker written before any content: a build interrupted mid-way stays
# recognisable as ours and can be replaced by the next run.
Set-Content -LiteralPath (Join-Path $OutputFullPath '.sangforbox-package') -Value 'SangforBox' -Encoding ascii
New-Item -ItemType Directory -Path (Join-Path $OutputFullPath 'WorkBuddy') -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $PyInstallerDist 'SangforBox.exe') -Destination $OutputFullPath
& (Join-Path $PSScriptRoot 'package-workbuddy.ps1')
$WorkBuddyDirectory = Join-Path $OutputFullPath 'WorkBuddy'
$SkillArchives = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'dist') -Filter '*-skill.zip' | Sort-Object Name)
if ($SkillArchives.Count -ne 3) { throw 'Expected exactly three skill archives from package-workbuddy.ps1.' }
foreach ($SkillArchive in $SkillArchives) {
    Copy-Item -LiteralPath $SkillArchive.FullName -Destination $WorkBuddyDirectory
}
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'PORTABLE-README.txt') -Destination $OutputFullPath

$SkillHashes = [ordered]@{}
foreach ($SkillArchive in $SkillArchives) {
    $SkillHashes[$SkillArchive.Name] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $WorkBuddyDirectory $SkillArchive.Name)).Hash.ToLowerInvariant()
}

$Manifest = [ordered]@{
    product = 'SangforBox'
    package_version = '1.0.7'
    built_at = [DateTime]::UtcNow.ToString('o')
    architecture = $env:PROCESSOR_ARCHITECTURE
    executable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputFullPath 'SangforBox.exe')).Hash.ToLowerInvariant()
    skill_archive_sha256 = $SkillHashes
}
$Manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputFullPath 'manifest.json') -Encoding utf8

# Compress into the build workspace first, then copy into place, so an existing
# archive never has to be deleted before being overwritten.
$ArchivePath = "$OutputFullPath.zip"
$ArchiveStaging = Join-Path $BuildRoot 'SangforBox-Portable.zip'
Compress-Archive -Path (Join-Path $OutputFullPath '*') -DestinationPath $ArchiveStaging -CompressionLevel Optimal
Copy-Item -LiteralPath $ArchiveStaging -Destination $ArchivePath -Force

if ($ReplacedOutput) {
    # Keep any existing runtime data out of the clean distribution archive.
    # Preserve the previous directory so upgrades never delete user state.
    foreach ($RuntimeName in @('data', 'results', 'logs')) {
        $PreviousRuntime = Join-Path $ReplacedOutput $RuntimeName
        if (Test-Path -LiteralPath $PreviousRuntime -PathType Container) {
            Copy-Item -LiteralPath $PreviousRuntime -Destination $OutputFullPath -Recurse
        }
    }
    Write-Host "Previous output preserved (including runtime data): $ReplacedOutput"
    Write-Host 'Runtime data restored beside the new executable, after creating the clean archive.'
}
Write-Host "Portable package created: $OutputFullPath"
Write-Host "Transfer archive created: $ArchivePath"
Write-Host "Build workspace (safe to delete): $BuildRoot"
