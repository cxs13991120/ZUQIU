$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$IndexPath = Join-Path $ProjectRoot "web\index.html"
$PythonPath = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"

if (Test-Path $PythonPath) {
    & $PythonPath (Join-Path $ProjectRoot "build_site.py")
} elseif (-not (Test-Path $IndexPath)) {
    & (Join-Path $ProjectRoot "run_daily.ps1")
}

Invoke-Item $IndexPath
