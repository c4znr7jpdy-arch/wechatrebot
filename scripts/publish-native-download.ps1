$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $projectRoot
try {
    & py -3.13 (Join-Path $PSScriptRoot 'publish_native_download.py') @args
    $publishExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $publishExitCode
