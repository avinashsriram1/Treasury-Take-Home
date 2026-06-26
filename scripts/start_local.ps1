param(
    [int]$Port = 8000,
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (!(Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if (!$line -or $line.StartsWith("#") -or !$line.Contains("=")) { return }
        $parts = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
    }
} else {
    Write-Host "No $EnvFile found. Copy .env.example to .env and set AZURE_FOUNDRY_API_KEY for LLM mode." -ForegroundColor Yellow
}

$env:PORT = [string]$Port
uvicorn app.main:app --reload --host 127.0.0.1 --port $Port
