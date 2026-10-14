param([switch]$IncludeOptionalSkills)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $ProjectRoot
$ProgressPreference = 'SilentlyContinue'

$SkillRoot = Join-Path $ProjectRoot 'connector\workbuddy\skills'
$OutputDirectory = Join-Path $ProjectRoot 'dist'
$PublishedSkillNames = @('af-app-control-optimization', 'af-device-initialization')
if ($IncludeOptionalSkills) { $PublishedSkillNames += 'af-daily-security-monitoring' }
$BuildRoot = Join-Path ([IO.Path]::GetTempPath()) ("sangforbox-skills-{0}" -f [guid]::NewGuid().ToString('N').Substring(0, 8))
$StagedOutput = Join-Path $BuildRoot 'dist'
New-Item -ItemType Directory -Path $StagedOutput -Force | Out-Null

try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    foreach ($SkillName in $PublishedSkillNames) {
        $SourceDirectory = Join-Path $SkillRoot $SkillName
        if ($SkillName -eq 'af-daily-security-monitoring') {
            $SourceDirectory = Join-Path $ProjectRoot "connector\workbuddy\optional-skills\$SkillName"
        }
        $SkillFile = Join-Path $SourceDirectory 'SKILL.md'
        if (-not (Test-Path -LiteralPath $SkillFile)) { throw "Skill definition missing: $SkillFile" }
        $Staging = Join-Path $BuildRoot $SkillName
        New-Item -ItemType Directory -Path $Staging | Out-Null
        Copy-Item -LiteralPath $SkillFile -Destination $Staging
        $References = Join-Path $SourceDirectory 'references'
        if (Test-Path -LiteralPath $References) {
            Copy-Item -LiteralPath $References -Destination $Staging -Recurse
        }
        $Archive = Join-Path $StagedOutput "$SkillName-skill.zip"
        Compress-Archive -Path (Join-Path $Staging '*') -DestinationPath $Archive -CompressionLevel Optimal
        $Zip = [IO.Compression.ZipFile]::OpenRead($Archive)
        try { $Entries = @($Zip.Entries | Where-Object { $_.Name } | ForEach-Object { $_.FullName.Replace('\', '/') }) }
        finally { $Zip.Dispose() }
        $ExpectedEntries = @('SKILL.md')
        if ($SkillName -eq 'af-daily-security-monitoring') {
            $ExpectedEntries += 'references/af-api-coverage.md'
        }
        if (@(Compare-Object $ExpectedEntries $Entries).Count -ne 0) {
            throw "Unexpected files in $SkillName skill archive"
        }
    }

    $PreviousOutput = $null
    if (Test-Path -LiteralPath $OutputDirectory) {
        $PreviousOutput = Join-Path $BuildRoot 'previous-dist'
        Move-Item -LiteralPath $OutputDirectory -Destination $PreviousOutput
    }
    try {
        Move-Item -LiteralPath $StagedOutput -Destination $OutputDirectory
    } catch {
        if (Test-Path -LiteralPath $OutputDirectory) {
            Move-Item -LiteralPath $OutputDirectory -Destination (Join-Path $BuildRoot 'failed-dist')
        }
        if ($PreviousOutput -and (Test-Path -LiteralPath $PreviousOutput)) {
            Move-Item -LiteralPath $PreviousOutput -Destination $OutputDirectory
        }
        throw
    }
} finally {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$Produced = @(Get-ChildItem -LiteralPath $OutputDirectory -Filter '*-skill.zip' | Sort-Object Name)
if ($Produced.Count -ne $PublishedSkillNames.Count) { throw 'Unexpected number of published skill archives.' }
Write-Host "Packaged $($Produced.Count) WorkBuddy skills:"
$Produced | ForEach-Object { Write-Host "  $($_.FullName)" }
