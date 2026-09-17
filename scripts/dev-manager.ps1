[CmdletBinding()]
param(
    [switch]$Preview,
    [string]$InstallRoot,
    [string]$Python,
    [switch]$Check,
    [switch]$NoWatch,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $PSScriptRoot 'dev_manager.py'
$runnerArgs = @($entry)
if ($Preview) { $runnerArgs += '--preview' }
if ($InstallRoot) { $runnerArgs += @('--install-root', $InstallRoot) }
if ($Check) { $runnerArgs += '--check' }
if ($NoWatch) { $runnerArgs += '--no-watch' }
if ($NoBrowser) { $runnerArgs += '--no-browser' }

# Reuse an existing environment. This entry never installs dependencies or builds.
if ($Python) {
    $candidates = @((Resolve-Path -LiteralPath $Python).Path)
} else {
    $candidates = @((Join-Path $repoRoot '.venv\Scripts\python.exe'))
    $artifacts = Join-Path $repoRoot 'artifacts'
    if (Test-Path -LiteralPath $artifacts) {
        $candidates += @(Get-ChildItem -LiteralPath $artifacts -Directory |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { Join-Path $_.FullName 'build-venv\Scripts\python.exe' })
    }
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython) { $candidates += $systemPython.Source }
}

$selectedPython = $null
foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    $probe = 'import sys; assert sys.version_info[:2] == (3, 13)'
    if (-not $Preview) { $probe += '; import webview, cryptography, packaging, tzdata' }
    try { & $candidate -c $probe 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { $selectedPython = $candidate; break }
}
if (-not $selectedPython) {
    throw 'No suitable Python 3.13 environment found. Use -Python with an existing manager build environment, or -Preview for UI-only verification.'
}

& $selectedPython @runnerArgs
exit $LASTEXITCODE
