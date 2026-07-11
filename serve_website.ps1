$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WebDir = Join-Path $ProjectRoot "web"
$PythonPath = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"

if (-not (Test-Path $PythonPath)) {
    throw "Python was not found at $PythonPath"
}

Set-Location $WebDir
& $PythonPath -m http.server 8765 --bind 127.0.0.1
