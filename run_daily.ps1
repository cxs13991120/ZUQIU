$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$PythonPath = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
if (Test-Path $PythonPath) {
    & $PythonPath ".\import_sporttery.py"
    & $PythonPath ".\predict_today.py"
    & $PythonPath ".\generate_betting_plan.py"
    & $PythonPath ".\build_site.py"
} else {
    & ".\predict_today.ps1"
}
