$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$OutputDirectory = Join-Path $PSScriptRoot 'dist'
$OutputFile = Join-Path $OutputDirectory 'sangforbox-workbuddy-connector.zip'
$OptimizationSkillFile = Join-Path $OutputDirectory 'af-app-control-optimization-skill.zip'

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
if (Test-Path -LiteralPath $OutputFile) { Remove-Item -LiteralPath $OutputFile -Force }
Compress-Archive -Path (Join-Path $PSScriptRoot 'connector\workbuddy\*') -DestinationPath $OutputFile -CompressionLevel Optimal
Write-Host "Created $OutputFile"

# WorkBuddy's “导入技能” dialog requires SKILL.md at the ZIP root. The full
# connector intentionally keeps several skills under skills/, so publish this
# separate single-skill archive for that dialog.
if (Test-Path -LiteralPath $OptimizationSkillFile) { Remove-Item -LiteralPath $OptimizationSkillFile -Force }
Compress-Archive -LiteralPath (Join-Path $PSScriptRoot 'connector\workbuddy\skills\af-app-control-optimization\SKILL.md') `
    -DestinationPath $OptimizationSkillFile -CompressionLevel Optimal
Write-Host "Created $OptimizationSkillFile (upload this ZIP in WorkBuddy's skill importer)"
