[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$Before
)

# Re-render the soft-skin design deliverable.
#
# Renders the real manager UI through the built-in ?preview fixtures, so no robot
# is started and no installed product is touched. Writes PNGs into screens/.
#
#   .\柔和界面设计稿\导出设计稿.ps1
#   .\柔和界面设计稿\导出设计稿.ps1 -Before      # capture the shipped look instead
#
# Only a Python 3.13 environment with the playwright package is required; the
# script never installs dependencies and never builds a package.

$ErrorActionPreference = 'Stop'
$designRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $designRoot

$python = $null
$candidates = @(
    (Join-Path $repoRoot '.venv\Scripts\python.exe'),
    'C:\Program Files\Python313\python.exe'
)
$systemPython = Get-Command python -ErrorAction SilentlyContinue
if ($systemPython) { $candidates += $systemPython.Source }

foreach ($candidate in $candidates) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try { & $candidate -c 'import playwright, sys; assert sys.version_info[:2] == (3, 13)' 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) {
    throw 'No Python 3.13 environment with the playwright package was found. Install it with: python -m pip install playwright; python -m playwright install chromium'
}

# Assemble the skin first, so the screenshots always match layers/ rather than a
# stale generated stylesheet. Skipped for the -Before comparison pass.
$builder = Join-Path $designRoot 'build_skin.py'
if ((Test-Path -LiteralPath $builder) -and -not $Before) {
    Write-Host 'Assembling the soft skin from its layers...'
    & $python $builder
    if ($LASTEXITCODE -ne 0) { throw 'Skin assembly failed.' }
}

$arguments = @((Join-Path $designRoot 'capture_soft_skin.py'))
if ($Before) { $arguments += @('--skin', 'before') }
if ($OutputDirectory) {
    $resolved = [System.IO.Path]::GetFullPath($OutputDirectory)
    $arguments += @('--out', $resolved)
}

& $python @arguments
exit $LASTEXITCODE
