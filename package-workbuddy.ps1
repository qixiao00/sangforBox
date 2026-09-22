$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Compress-Archive -Force removes the destination before writing, which fails
# under restrictive safe-delete policies. Archives are therefore built in a temp
# directory and copied into place instead.
$ProgressPreference = 'SilentlyContinue'

$SourceDirectory = Join-Path $PSScriptRoot 'connector\workbuddy'
$OutputDirectory = Join-Path $PSScriptRoot 'dist'
$ConnectorDirectory = Join-Path $OutputDirectory 'sangforbox-workbuddy-connector'
$ConnectorArchive = Join-Path $OutputDirectory 'sangforbox-workbuddy-connector.zip'

if (-not (Test-Path -LiteralPath (Join-Path $SourceDirectory 'SKILL.md'))) {
    throw "Connector index not found: $SourceDirectory\SKILL.md"
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceDirectory 'mcp.json'))) {
    throw "Connector MCP configuration not found: $SourceDirectory\mcp.json"
}

# dist/ is a build output, always rebuilt from connector/workbuddy so the two
# trees cannot drift apart. Compress-Archive overwrites existing archives, and
# Copy-Item -Force overwrites existing files, so no destructive delete is needed
# here; that keeps the script usable under restrictive safe-delete policies.
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $ConnectorDirectory -Force | Out-Null
Copy-Item -Path (Join-Path $SourceDirectory '*') -Destination $ConnectorDirectory -Recurse -Force

$BuildRoot = Join-Path ([IO.Path]::GetTempPath()) ("sangforbox-package-{0}" -f [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
try {
    $ConnectorArchiveStaging = Join-Path $BuildRoot 'connector.zip'
    Compress-Archive -Path (Join-Path $ConnectorDirectory '*') -DestinationPath $ConnectorArchiveStaging -CompressionLevel Optimal
    Copy-Item -LiteralPath $ConnectorArchiveStaging -Destination $ConnectorArchive -Force
    Write-Host "Created $ConnectorArchive"

    # WorkBuddy's "import skill" dialog requires SKILL.md at the ZIP root. Every
    # scenario skill under skills/ is a separate installable unit, so publish one
    # archive per skill. Only skills\<name>\SKILL.md is a skill definition; the
    # connector-level SKILL.md is a shared index and must NOT be packaged here.
    $SkillRoot = Join-Path $SourceDirectory 'skills'
    $SkillIndex = @(Get-ChildItem -LiteralPath $SkillRoot -Directory | Sort-Object Name)
    if ($SkillIndex.Count -eq 0) { throw "No skills found under $SkillRoot" }

    foreach ($Skill in $SkillIndex) {
        $SkillFile = Join-Path $Skill.FullName 'SKILL.md'
        if (-not (Test-Path -LiteralPath $SkillFile)) { throw "Skill definition missing: $SkillFile" }
        $Archive = Join-Path $OutputDirectory ("{0}-skill.zip" -f $Skill.Name)
        $Staging = Join-Path $BuildRoot $Skill.Name
        New-Item -ItemType Directory -Path $Staging -Force | Out-Null
        Copy-Item -LiteralPath $SkillFile -Destination $Staging -Force
        # Same staging pattern as the connector archive: Compress-Archive without
        # -Force fails when the destination exists, so build in temp and copy over.
        $SkillArchiveStaging = Join-Path $BuildRoot ("{0}.zip" -f $Skill.Name)
        Compress-Archive -Path (Join-Path $Staging '*') -DestinationPath $SkillArchiveStaging -CompressionLevel Optimal
        Copy-Item -LiteralPath $SkillArchiveStaging -Destination $Archive -Force
        Write-Host "Created $Archive"
    }
} finally {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$Produced = @(Get-ChildItem -LiteralPath $OutputDirectory -Filter '*-skill.zip')
Write-Host ("Packaged {0} skill archives for {1} skills." -f $Produced.Count, $SkillIndex.Count)
if ($Produced.Count -ne $SkillIndex.Count) { throw 'Skill archive count does not match the skill directory count.' }

# The connector copy and the source must stay identical: compare relative paths
# so a future drift fails the build instead of shipping a stale connector.
$SourceFiles = @(Get-ChildItem -LiteralPath $SourceDirectory -Recurse -File |
    ForEach-Object { $_.FullName.Substring($SourceDirectory.Length + 1) } | Sort-Object)
$CopyFiles = @(Get-ChildItem -LiteralPath $ConnectorDirectory -Recurse -File |
    ForEach-Object { $_.FullName.Substring($ConnectorDirectory.Length + 1) } | Sort-Object)
$Drift = Compare-Object -ReferenceObject $SourceFiles -DifferenceObject $CopyFiles
if ($Drift) {
    throw ("dist connector copy does not match connector\workbuddy: " + (($Drift | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }) -join '; '))
}
Write-Host "Verified dist connector copy matches connector\workbuddy ($($SourceFiles.Count) files)."
