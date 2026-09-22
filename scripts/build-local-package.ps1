param([string]$SourceRoot = 'E:\Project', [switch]$Plan)
$ErrorActionPreference = 'Stop'
$buildExitCode = 1
try {
    $builder = Join-Path $PSScriptRoot 'build_local_package.py'
    $buildArgs = @($builder, '--source-root', $SourceRoot)
    if ($Plan) { $buildArgs += '--plan' }
    else { $buildArgs += '--open-output' }
    & py -3.13 @buildArgs
    $buildExitCode = $LASTEXITCODE
    if ($buildExitCode -ne 0) { Write-Host 'Build failed. See the error and log path above.' -ForegroundColor Red }
}
catch { Write-Host $_ -ForegroundColor Red }
finally {
    if (-not $Plan) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $buildExitCode
