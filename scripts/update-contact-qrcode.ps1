param(
    [string]$Image = '',
    [string]$GroupName = 'WXRebot',
    [string]$ExpiresAt = '',
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
$exitCode = 1
try {
    if (-not $Image) { $Image = Read-Host 'QR image path (PNG/JPEG, max 512 KiB)' }
    $Image = $Image.Trim().Trim('"')
    if (-not $ExpiresAt) {
        $ExpiresAt = (Read-Host 'Expiry date YYYY-MM-DD, Beijing midnight (empty = 7 days from now)').Trim()
    }
    $script = Join-Path $PSScriptRoot 'update_contact_qrcode.py'
    $scriptArgs = @($script, '--image', $Image, '--group-name', $GroupName)
    if ($ExpiresAt) { $scriptArgs += @('--expires-at', $ExpiresAt) }
    if ($Plan) { $scriptArgs += '--plan' }
    & py -3.13 @scriptArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { Write-Host 'QR code was not updated. See the error above.' -ForegroundColor Red }
}
catch { Write-Host $_ -ForegroundColor Red }
finally {
    if (-not $Plan) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $exitCode
