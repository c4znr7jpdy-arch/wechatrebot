param([string]$SourceRoot = 'E:\Project', [switch]$Plan, [switch]$PackageOnly, [switch]$ManagerOnly)
$ErrorActionPreference = 'Stop'
$updateExitCode = 1
try {
    $updateArgs = @((Join-Path $PSScriptRoot 'update_local_incremental.py'), '--source-root', $SourceRoot)
    if ($Plan) { $updateArgs += '--plan' }
    if ($PackageOnly) { $updateArgs += '--package-only' }
    if ($ManagerOnly) { $updateArgs += '--manager-only' }
    & py -3.13 @updateArgs
    $updateExitCode = $LASTEXITCODE
}
catch { Write-Host $_ -ForegroundColor Red }
finally { if (-not $Plan) { Read-Host 'Press Enter to close' | Out-Null } }
exit $updateExitCode
