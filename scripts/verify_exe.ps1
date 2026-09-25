$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Get-Command python -ErrorAction Stop
& $Python.Source -X utf8 (Join-Path $PSScriptRoot "verify_exe.py")
if ($LASTEXITCODE -ne 0) { throw "Frozen executable verification failed with exit code $LASTEXITCODE." }
