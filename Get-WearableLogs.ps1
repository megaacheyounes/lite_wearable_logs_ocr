$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = $PSScriptRoot
$venvRoot = Join-Path $projectRoot '.venv'
$pythonPath = Join-Path $venvRoot 'Scripts\python.exe'
$lockPath = Join-Path $projectRoot 'requirements.lock.txt'
$markerPath = Join-Path $venvRoot '.requirements.sha256'

function Find-Python312 {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3.12 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
        if ($LASTEXITCODE -eq 0) { return @($pyLauncher.Source, '-3.12') }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        & $python.Source -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
        if ($LASTEXITCODE -eq 0) { return @($python.Source) }
    }

    throw 'Python 3.12 was not found. Install Python 3.12 and rerun this command.'
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonCommand = Find-Python312
    Write-Host 'Creating project-local Python environment...'
    if ($pythonCommand.Count -eq 2) {
        & $pythonCommand[0] $pythonCommand[1] -m venv $venvRoot
    } else {
        & $pythonCommand[0] -m venv $venvRoot
    }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the project-local Python environment.' }
}

$requiredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $lockPath).Hash
$installedHash = if (Test-Path -LiteralPath $markerPath) {
    (Get-Content -Raw -LiteralPath $markerPath).Trim()
} else { '' }

if ($installedHash -ne $requiredHash) {
    Write-Host 'Installing pinned project dependencies...'
    & $pythonPath -m pip install --disable-pip-version-check -r $lockPath
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install the pinned project dependencies.' }
    Set-Content -LiteralPath $markerPath -Value $requiredHash -Encoding ascii
}

$env:WEARABLE_LOGS_ROOT = $projectRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
& $pythonPath -m wearable_logs_ocr.cli @args
exit $LASTEXITCODE
