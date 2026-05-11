$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host '[Setup] Creating virtual environment...'
    py -3 -m venv .venv
}

Write-Host '[Setup] Upgrading pip...'
& $venvPython -m pip install -U pip

$reqA = Join-Path $projectRoot 'zhuanzhuan_pricing\requirements.txt'
$reqB = Join-Path $projectRoot 'requirements.txt'
if (Test-Path $reqA) {
    Write-Host '[Setup] Installing dependencies from zhuanzhuan_pricing\requirements.txt...'
    & $venvPython -m pip install -r $reqA
} elseif (Test-Path $reqB) {
    Write-Host '[Setup] Installing dependencies from requirements.txt...'
    & $venvPython -m pip install -r $reqB
} else {
    Write-Host '[Warn] requirements file not found. Continue without dependency install.'
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'runtime') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'data\browser_profiles') | Out-Null

Write-Host ''
Write-Host '[Agent] Launching interactive runner...'
Write-Host ''
& $venvPython -m zhuanzhuan_pricing.automation.agent_runner
$exitCode = $LASTEXITCODE

Write-Host ''
Write-Host "[Agent] Process exited with code $exitCode."
Read-Host 'Press Enter to exit'
exit $exitCode
