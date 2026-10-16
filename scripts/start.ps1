# Normal start (development). Loads saved settings (.env.local; process environment variables take precedence) and starts the server.
# Never generates a key and never creates an admin. Run scripts\setup.ps1 once before the first start.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  Write-Error 'Virtualenv not found. Run scripts\setup.ps1 first.'
  exit 2
}
& $py -m server.run
exit $LASTEXITCODE
