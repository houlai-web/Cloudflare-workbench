# Build VLESS-Workbench.exe (single file, no console)
# Usage: powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $appDir

$entry = Join-Path $appDir "launcher.pyw"
$webDir = Join-Path $appDir "web"

Write-Host "Cleaning old build/dist ..."
Remove-Item -Recurse -Force (Join-Path $appDir "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $appDir "dist") -ErrorAction SilentlyContinue

# app.ico 以仓库内的固定文件为准（用户提供）；缺失时才用 make_icon.py 生成兜底图标
if (-not (Test-Path (Join-Path $appDir "app.ico"))) {
    Write-Host "app.ico 不存在，使用 make_icon.py 生成兜底图标 ..."
    python make_icon.py
}

Write-Host "Running PyInstaller ..."
python -m PyInstaller `
    --onefile `
    --noconsole `
    --noconfirm `
    --name "VLESS-Workbench" `
    --icon "app.ico" `
    --add-data "${webDir};web" `
    --add-data "${appDir}\app.ico;." `
    --add-data "${appDir}\config.example.json;." `
    --hidden-import waitress `
    --hidden-import flask `
    --collect-submodules waitress `
    $entry

$exe = Join-Path $appDir "dist\VLESS-Workbench.exe"
if (Test-Path $exe) {
    $dest = Join-Path (Split-Path $appDir -Parent) "VLESS-Workbench.exe"
    Copy-Item $exe $dest -Force
    $size = [math]::Round((Get-Item $dest).Length / 1MB, 1)
    Write-Host "Build OK: $dest ($size MB)"
} else {
    Write-Host "Build FAILED: dist\VLESS-Workbench.exe not found" -ForegroundColor Red
    exit 1
}
