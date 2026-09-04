$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$publisher = Join-Path $PSScriptRoot "publish_full_update.py"
$publishExitCode = 1

Push-Location -LiteralPath $projectRoot
try {
    & py -3.13 $publisher @args
    $publishExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $publishExitCode
