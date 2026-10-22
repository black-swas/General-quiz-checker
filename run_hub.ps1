<#
.SYNOPSIS
    Starts the IISER Bhopal Mobile Exam Scanner & AI Grading Hub on PC.
.EXAMPLE
    .\run_hub.ps1
    .\run_hub.ps1 -Port 8080 -Https
#>
param(
    [int]$Port = 8000,
    [switch]$Https,
    [switch]$NoBrowser
)

Set-Location -LiteralPath $PSScriptRoot

$pythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "[INFO] Virtual environment not found at .\venv. Creating venv..." -ForegroundColor Yellow
    python -m venv (Join-Path $PSScriptRoot "venv")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create virtual environment. Ensure Python 3.10+ is installed on PATH."
        exit 1
    }
    Write-Host "[INFO] Installing dependencies from requirements.txt..." -ForegroundColor Yellow
    $pipExe = Join-Path $PSScriptRoot "venv\Scripts\pip.exe"
    & $pipExe install -r (Join-Path $PSScriptRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install dependencies."
        exit 1
    }
    Write-Host "[SUCCESS] Virtual environment initialized successfully!" -ForegroundColor Green
}

$cmdArgs = @("local_hub.py", "--port", $Port)
if ($Https) {
    $cmdArgs += "--https"
}
if (-not $NoBrowser) {
    $cmdArgs += "--open-browser"
}

Write-Host "Starting Exam Scanner & AI Grading Hub..." -ForegroundColor Cyan
& $pythonExe @cmdArgs
