$ErrorActionPreference = 'Stop'

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    Write-Error 'Virtual environment not found. Run scripts/setup-venv.ps1 first.'
}

.\.venv\Scripts\python -m pytest tests/unit -v

Write-Host 'Local Windows validation complete (unit tests).' -ForegroundColor Green
Write-Host 'Run ansible-galaxy and ansible-test in the Dev Container for full validation.' -ForegroundColor Yellow
