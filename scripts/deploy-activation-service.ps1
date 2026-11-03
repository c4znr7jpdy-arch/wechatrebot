param(
    [switch]$Plan,
    [switch]$Rollback,
    [string]$ServiceUrl = ''
)
$ErrorActionPreference = 'Stop'
$exitCode = 1
try {
    $script = Join-Path $PSScriptRoot 'deploy_activation_service.py'
    $baseArgs = @($script)
    if ($ServiceUrl) { $baseArgs += @('--service-url', $ServiceUrl) }
    if ($Rollback) { $baseArgs += '--rollback' }

    # The plan never connects to the server: it computes the content identity
    # and prints every command that a real run would execute.
    $planArgs = $baseArgs + '--plan'
    & py -3.13 @planArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw 'Plan failed. Nothing was changed on the server.' }

    if (-not $Plan) {
        $word = if ($Rollback) { 'ROLLBACK' } else { 'UPGRADE' }
        $answer = Read-Host "Type $word to run these steps on the production server (anything else cancels)"
        if ($answer -ne $word) {
            Write-Host 'Cancelled. Nothing was changed on the server.' -ForegroundColor Yellow
            $exitCode = 1
        }
        else {
            & py -3.13 @baseArgs
            $exitCode = $LASTEXITCODE
            if ($exitCode -ne 0) { Write-Host 'The operation did not complete. Read the report above.' -ForegroundColor Red }
        }
    }
}
catch { Write-Host $_ -ForegroundColor Red }
finally {
    if (-not $Plan) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $exitCode
