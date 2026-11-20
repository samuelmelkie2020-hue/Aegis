# Build the Aegis desktop app on Windows.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt

Write-Host "Building..."
python -m PyInstaller --noconfirm --clean packaging/aegis.spec

Write-Host ""
Write-Host "Done: dist\Aegis.exe"
