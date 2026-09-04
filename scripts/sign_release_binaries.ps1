[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ManagerExe,
    [Parameter(Mandatory = $true)] [string] $InjectorExe,
    [Parameter(Mandatory = $true)] [string] $HookDll,
    [Parameter(Mandatory = $true)] [string] $CertificateThumbprint,
    [Parameter(Mandatory = $true)] [uri] $TimestampUrl,
    [string] $SignTool
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $SignTool) {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) {
        $SignTool = $command.Source
    } else {
        $kitsRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
        $candidate = Get-ChildItem -LiteralPath $kitsRoot -Filter signtool.exe -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object FullName -Match '\\x64\\signtool\.exe$' |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { $SignTool = $candidate.FullName }
    }
}
if (-not $SignTool -or -not (Test-Path -LiteralPath $SignTool -PathType Leaf)) {
    throw 'signtool.exe was not found. Install the Windows SDK or pass -SignTool.'
}

$certificate = @(
    Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object Thumbprint -EQ $CertificateThumbprint
)
if ($certificate.Count -ne 1 -or -not $certificate[0].HasPrivateKey) {
    throw 'Exactly one usable code-signing certificate with a private key must match the thumbprint.'
}
if ($certificate[0].NotAfter -le (Get-Date)) {
    throw 'The selected code-signing certificate is expired.'
}

$targets = @($ManagerExe, $InjectorExe, $HookDll) | ForEach-Object {
    (Resolve-Path -LiteralPath $_).Path
}
foreach ($target in $targets) {
    & $SignTool sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl.AbsoluteUri /td SHA256 $target
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed for $target with exit code $LASTEXITCODE"
    }
    & $SignTool verify /pa /all /v $target
    if ($LASTEXITCODE -ne 0) {
        throw "signature verification failed for $target with exit code $LASTEXITCODE"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $target
    if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate) {
        throw "Authenticode or RFC 3161 timestamp validation failed for $target"
    }
}

[ordered]@{
    ok = $true
    signer_subject = $certificate[0].Subject
    signer_thumbprint = $certificate[0].Thumbprint
    timestamp_url = $TimestampUrl.AbsoluteUri
    signed_files = @($targets | ForEach-Object { Split-Path -Leaf $_ })
} | ConvertTo-Json -Depth 4
