param(
    [string]$OutputPath = "assets\splash.png"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$projectRoot = Split-Path -Parent $PSScriptRoot
$iconPath = Join-Path $projectRoot "assets\icon.png"
$targetPath = Join-Path $projectRoot $OutputPath
$bitmap = [System.Drawing.Bitmap]::new(
    640,
    330,
    [System.Drawing.Imaging.PixelFormat]::Format24bppRgb
)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$icon = $null
$background = $null
$titleBrush = $null
$subtitleBrush = $null
$accentBrush = $null
$titleFont = $null
$subtitleFont = $null

try {
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = (
        [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    )
    $graphics.TextRenderingHint = (
        [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    )

    $background = [System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml("#F8F9FF")
    )
    $titleBrush = [System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml("#1A1B20")
    )
    $subtitleBrush = [System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml("#45464F")
    )
    $accentBrush = [System.Drawing.SolidBrush]::new(
        [System.Drawing.ColorTranslator]::FromHtml("#D6E3FF")
    )
    $graphics.FillRectangle($background, 0, 0, 640, 330)
    $graphics.FillEllipse($accentBrush, 266, 36, 108, 108)

    $icon = [System.Drawing.Image]::FromFile($iconPath)
    $graphics.DrawImage($icon, 278, 48, 84, 84)

    $titleFont = [System.Drawing.Font]::new(
        "Segoe UI Semibold",
        25,
        [System.Drawing.FontStyle]::Regular,
        [System.Drawing.GraphicsUnit]::Pixel
    )
    $subtitleFont = [System.Drawing.Font]::new(
        "Segoe UI",
        14,
        [System.Drawing.FontStyle]::Regular,
        [System.Drawing.GraphicsUnit]::Pixel
    )
    $center = [System.Drawing.StringFormat]::new()
    try {
        $center.Alignment = [System.Drawing.StringAlignment]::Center
        $graphics.DrawString(
            "TeslaParser",
            $titleFont,
            $titleBrush,
            [System.Drawing.RectangleF]::new(0, 164, 640, 42),
            $center
        )
        $graphics.DrawString(
            "Starting application...",
            $subtitleFont,
            $subtitleBrush,
            [System.Drawing.RectangleF]::new(0, 211, 640, 30),
            $center
        )
    }
    finally {
        $center.Dispose()
    }

    $targetDirectory = Split-Path -Parent $targetPath
    New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    $bitmap.Save($targetPath, [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    foreach ($resource in @(
        $icon,
        $background,
        $titleBrush,
        $subtitleBrush,
        $accentBrush,
        $titleFont,
        $subtitleFont,
        $graphics,
        $bitmap
    )) {
        if ($null -ne $resource) {
            $resource.Dispose()
        }
    }
}

Write-Host "Generated $targetPath"
