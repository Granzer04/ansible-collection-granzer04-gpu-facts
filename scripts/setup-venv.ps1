$ErrorActionPreference = 'Stop'

py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -r requirements-dev.txt

Write-Host 'Virtual environment is ready at .venv' -ForegroundColor Green
