$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $venv)) {
    Write-Host "Virtual environment not found. Create it with: python -m venv .venv"
    exit 1
}
& $venv $here\main.py @args