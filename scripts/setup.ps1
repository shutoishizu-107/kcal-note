# First-time setup (development). Creates the virtualenv if missing, then runs `server.manage init`:
#  - generates and saves the encryption key to .env.local ONLY if no key exists yet (and no encrypted data exists in the DB)
#  - issues a one-time invite link for the first admin (only if no admin exists)
# Safe to re-run: existing keys, settings and admin are never changed. The key is never printed.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  Write-Host 'Creating virtualenv...'
  & py -m venv (Join-Path $root '.venv')
  & $py -m pip install --quiet -r (Join-Path $root 'requirements.txt')
}
& $py -m server.manage init
exit $LASTEXITCODE
