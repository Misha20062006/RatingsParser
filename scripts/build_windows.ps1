param(
    [switch]$KeepBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$fletExecutable = Join-Path $projectRoot ".venv\Scripts\flet.exe"
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"
$driverSource = ".venv\Lib\site-packages\patchright\driver:patchright\driver"
$generatedSpec = Join-Path $projectRoot "TeslaCraftParserGUI.spec"
$buildDirectory = Join-Path $projectRoot "build"

if (-not (Test-Path -LiteralPath $fletExecutable)) {
    throw "Install project dependencies into .venv first: python -m pip install -e `".[dev]`""
}

$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $projectRoot
    $env:PYTHONUTF8 = "1"

    & $fletExecutable pack main.py `
        --icon assets\icon.ico `
        --name TeslaCraftParserGUI `
        --distpath dist `
        --product-name "TeslaCraft Parser" `
        --file-description "TeslaCraft public statistics parser" `
        --product-version 2.0.0 `
        --file-version 2.0.0.0 `
        --company-name Misha20062006 `
        --add-data "assets:assets" $driverSource `
        --hidden-import patchright.async_api `
        --yes
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build the GUI application"
    }

    & $pythonExecutable -m PyInstaller --noconfirm --clean UsersParser.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build the console application"
    }

    Write-Host "Built: dist\TeslaCraftParserGUI.exe"
    Write-Host "Built: dist\TeslaCraftParser.exe"
}
finally {
    if (Test-Path -LiteralPath $generatedSpec) {
        Remove-Item -Force -LiteralPath $generatedSpec
    }
    if (-not $KeepBuild -and (Test-Path -LiteralPath $buildDirectory)) {
        Remove-Item -Recurse -Force -LiteralPath $buildDirectory
    }
    Set-Location -LiteralPath $previousLocation
}
