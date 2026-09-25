$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

foreach ($Directory in @("dist", "build")) {
    $Path = Join-Path $ProjectRoot $Directory
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

$PyInstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $PyInstaller) {
    $Python = Get-Command python -ErrorAction Stop
    & $Python.Source -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Failed to install build-only PyInstaller." }
    $PyInstaller = Get-Command pyinstaller -ErrorAction Stop
}

& $PyInstaller.Source --noconfirm --clean (Join-Path $ProjectRoot "ScriptPlayground.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
$Executable = Join-Path $ProjectRoot "dist\ScriptPlayground\ScriptPlayground.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Build succeeded but executable is missing: $Executable"
}
$Item = Get-Item -LiteralPath $Executable
Write-Output ("Built: {0}" -f $Item.FullName)
Write-Output ("Size:  {0:N2} MiB ({1:N0} bytes)" -f ($Item.Length / 1MB), $Item.Length)
