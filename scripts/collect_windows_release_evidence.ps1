[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ReleaseVersion,
    [Parameter(Mandatory = $true)] [string] $ManagerExe,
    [Parameter(Mandatory = $true)] [string] $InjectorExe,
    [Parameter(Mandatory = $true)] [string] $HookDll,
    [Parameter(Mandatory = $true)] [string] $Output,
    [switch] $RunDefenderScan
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-FileEvidence {
    param(
        [Parameter(Mandatory = $true)] [string] $Role,
        [Parameter(Mandatory = $true)] [string] $Path
    )

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $item = Get-Item -LiteralPath $resolved
    if (-not $item.PSIsContainer -and $item.Length -gt 0) {
        $signature = Get-AuthenticodeSignature -LiteralPath $resolved
        return [ordered]@{
            role = $Role
            filename = $item.Name
            size = $item.Length
            sha256 = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash
            authenticode_status = [string] $signature.Status
            signer_subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
            signer_thumbprint = if ($signature.SignerCertificate) { $signature.SignerCertificate.Thumbprint } else { '' }
            signer_not_after = if ($signature.SignerCertificate) { $signature.SignerCertificate.NotAfter.ToUniversalTime().ToString('o') } else { '' }
            timestamp_subject = if ($signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Subject } else { '' }
            timestamp_thumbprint = if ($signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Thumbprint } else { '' }
        }
    }
    throw "Release evidence target is not a non-empty file: $resolved"
}

$targets = @(
    Get-FileEvidence -Role 'manager' -Path $ManagerExe
    Get-FileEvidence -Role 'injector' -Path $InjectorExe
    Get-FileEvidence -Role 'hook-library' -Path $HookDll
)

$defender = $null
try {
    $status = Get-MpComputerStatus
    $defender = [ordered]@{
        available = $true
        antivirus_enabled = [bool] $status.AntivirusEnabled
        antispyware_enabled = [bool] $status.AntispywareEnabled
        realtime_protection_enabled = [bool] $status.RealTimeProtectionEnabled
        engine_version = [string] $status.AMEngineVersion
        signature_version = [string] $status.AntivirusSignatureVersion
        signature_updated_at = $status.AntivirusSignatureLastUpdated.ToUniversalTime().ToString('o')
        custom_scan_run = [bool] $RunDefenderScan
    }
    if ($RunDefenderScan) {
        foreach ($path in @($ManagerExe, $InjectorExe, $HookDll)) {
            Start-MpScan -ScanType CustomScan -ScanPath (Resolve-Path -LiteralPath $path).Path
        }
    }
} catch {
    $defender = [ordered]@{
        available = $false
        error_type = $_.Exception.GetType().Name
        custom_scan_run = $false
    }
}

$securityProducts = @()
try {
    $securityProducts = @(
        Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntivirusProduct |
            ForEach-Object {
                [ordered]@{
                    display_name = [string] $_.displayName
                    product_state = [int] $_.productState
                    timestamp = [string] $_.timestamp
                }
            }
    )
} catch {
    $securityProducts = @()
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$record = [ordered]@{
    schema_version = 1
    release_version = $ReleaseVersion
    status = if (@($targets | Where-Object authenticode_status -NE 'Valid').Count -eq 0) { 'valid' } else { 'invalid' }
    collected_at = (Get-Date).ToUniversalTime().ToString('o')
    operating_system = [ordered]@{
        caption = [string] $os.Caption
        version = [string] $os.Version
        build_number = [string] $os.BuildNumber
        architecture = [string] $os.OSArchitecture
    }
    files = $targets
    all_signatures_valid = @($targets | Where-Object authenticode_status -NE 'Valid').Count -eq 0
    defender = $defender
    antivirus_products = $securityProducts
    notes = @(
        'This record does not prove that the machine was clean before the test.',
        'A second antivirus product and the complete customer workflow require separate evidence.',
        'Never add private keys, certificate passwords, API keys, cookies, or customer messages.'
    )
}

$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputDirectory = [System.IO.Path]::GetDirectoryName($outputPath)
if ($outputDirectory) {
    [System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
}
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $outputPath -Encoding utf8
$record | ConvertTo-Json -Depth 8
