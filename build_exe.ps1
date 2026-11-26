# Build standalone SocialManager.exe with PyInstaller
param(
    [string]$Name = "SocialManager"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

.\.venv\Scripts\pyinstaller.exe `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $Name `
    --add-data "assets/fonts;assets/fonts" `
    --collect-submodules keyring.backends `
    --collect-submodules win32ctypes `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtWebEngineQuick `
    --exclude-module PySide6.Qt3DCore `
    --exclude-module PySide6.Qt3DRender `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtPdf `
    main.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "`nBuild complete: $PSScriptRoot\dist\$Name.exe"
