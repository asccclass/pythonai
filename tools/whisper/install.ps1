# install.ps1 — 複製 whisper-cli 工具到本目錄
# 執行方式：在專案根目錄執行 .\tools\whisper\install.ps1
# 或直接在此目錄執行 .\install.ps1

param(
    [string]$WhisperBuildDir = "C:\Users\andyl\Go\whisper.cpp\build\bin\Release",
    [string]$ModelSrc        = "C:\Users\andyl\Go\whisper.cpp\ggml-base.bin"
)

$ErrorActionPreference = "Stop"
$dest = $PSScriptRoot  # tools/whisper/

Write-Host "=== Whisper STT 工具安裝 ===" -ForegroundColor Cyan
Write-Host "來源 EXE 目錄: $WhisperBuildDir"
Write-Host "模型來源:      $ModelSrc"
Write-Host "目標目錄:      $dest"
Write-Host ""

# 需要複製的二進位檔
$binaries = @(
    "whisper-cli.exe",
    "whisper.dll",
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll"
)

foreach ($file in $binaries) {
    $src = Join-Path $WhisperBuildDir $file
    if (-not (Test-Path $src)) {
        Write-Warning "找不到 $src，跳過。"
        continue
    }
    $dst = Join-Path $dest $file
    Copy-Item $src $dst -Force
    $size = [math]::Round((Get-Item $dst).Length / 1KB)
    Write-Host "  複製 $file (${size} KB)" -ForegroundColor Green
}

# 複製模型
if (Test-Path $ModelSrc) {
    $modelDst = Join-Path $dest "ggml-base.bin"
    Write-Host ""
    Write-Host "複製模型（約 142 MB，請稍候…）" -ForegroundColor Yellow
    Copy-Item $ModelSrc $modelDst -Force
    $size = [math]::Round((Get-Item $modelDst).Length / 1MB, 1)
    Write-Host "  複製 ggml-base.bin (${size} MB)" -ForegroundColor Green
} else {
    Write-Warning "模型檔案不存在：$ModelSrc"
}

Write-Host ""
Write-Host "安裝完成！" -ForegroundColor Cyan
Write-Host "測試指令："
Write-Host "  .\whisper-cli.exe --model .\ggml-base.bin --file <your.wav> --language zh"
