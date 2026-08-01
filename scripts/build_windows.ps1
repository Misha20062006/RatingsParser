param(
    [switch]$KeepBuild,
    [switch]$GuiOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$fletExecutable = Join-Path $projectRoot ".venv\Scripts\flet.exe"
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"
$splashPath = Join-Path $projectRoot "assets\splash.png"
$splashGenerator = Join-Path $projectRoot "scripts\generate_splash.ps1"
$driverSource = ".venv\Lib\site-packages\patchright\driver:patchright\driver"
$generatedSpec = Join-Path $projectRoot "TeslaCraftParserGUI.spec"
$buildDirectory = Join-Path $projectRoot "build"
$distDirectory = Join-Path $projectRoot "dist"
$pyprojectPath = Join-Path $projectRoot "pyproject.toml"

if (-not (Test-Path -LiteralPath $fletExecutable)) {
    throw "Install project dependencies into .venv first: python -m pip install -e `".[dev]`""
}

$pyproject = Get-Content -Raw -LiteralPath $pyprojectPath
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
    throw "Could not read the project version from pyproject.toml"
}
$productVersion = $Matches[1]
$fileVersion = "$productVersion.0"

$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $projectRoot
    $env:PYTHONUTF8 = "1"

    if (-not (Test-Path -LiteralPath $splashPath)) {
        & $splashGenerator
    }

    & $fletExecutable pack main.py `
        --icon assets\icon.ico `
        --name TeslaCraftParserGUI `
        --distpath dist `
        --product-name "TeslaCraft Parser" `
        --file-description "TeslaCraft public statistics parser" `
        --product-version $productVersion `
        --file-version $fileVersion `
        --company-name Misha20062006 `
        --add-data "assets:assets" $driverSource `
        --hidden-import patchright.async_api `
        --pyinstaller-build-args=--splash=assets/splash.png `
        --pyinstaller-build-args=--splash-center=active `
        --yes
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build the GUI application"
    }

    if (-not $GuiOnly) {
        & $pythonExecutable -m PyInstaller --noconfirm --clean UsersParser.spec
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build the console application"
        }
    }

    $guiExecutable = Join-Path $distDirectory "TeslaCraftParserGUI.exe"
    if (-not (Test-Path -LiteralPath $guiExecutable)) {
        throw "The GUI executable was not created"
    }

    $legacyGuiDirectory = Join-Path $distDirectory "TeslaCraftParserGUI"
    $resolvedDistDirectory = [System.IO.Path]::GetFullPath($distDirectory).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    $resolvedLegacyDirectory = [System.IO.Path]::GetFullPath($legacyGuiDirectory)
    if (
        (Test-Path -LiteralPath $resolvedLegacyDirectory) -and
        $resolvedLegacyDirectory.StartsWith(
            $resolvedDistDirectory,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Remove-Item -Recurse -Force -LiteralPath $resolvedLegacyDirectory
    }
    foreach ($oldShortcutName in @(
        "TeslaCraft Parser.lnk",
        "TeslaCraft Parser (быстрый).lnk"
    )) {
        $oldShortcut = Join-Path $distDirectory $oldShortcutName
        if (Test-Path -LiteralPath $oldShortcut) {
            Remove-Item -Force -LiteralPath $oldShortcut
        }
    }

    Write-Host "Built standalone GUI: dist\TeslaCraftParserGUI.exe"
    if (-not $GuiOnly) {
        Write-Host "Built CLI: dist\TeslaCraftParser.exe"
    }
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
