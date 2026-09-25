# build_android.ps1 — собирает Android-приложение (TWA/APK) из PikaBuReader PWA
# Запуск: powershell -ExecutionPolicy Bypass -File tools\build_android.ps1
# Требования:
#   1. Node.js 18+ (для Bubblewrap)
#   2. Java JDK 17+ (для Android SDK)
#   3. Android SDK (cmdline-tools, platform-tools, platforms;android-34)
#
# Установка зависимостей (один раз):
#   npm install -g @nicknisi/twa-bubblewrap
#   ИЛИ: npm install -g bubblewrap
#   java -version  # убедись JDK 17+
#   sdkmanager --list  # проверить android-34
#
# После сборки: tools\android\app\build\outputs\apk\debug\app-debug.apk

param(
    [string]$HostUrl = "http://localhost:8000",
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path "$here\..").Path
$workDir = "$env:TEMP\pikabu_android_$PID"
$androidDir = "$workDir\android"

Write-Host "=== PikaBuReader Android TWA builder ==="
Write-Host "Work dir: $workDir"

# Проверка Node.js
try {
    $nodeVersion = & node --version 2>&1
    Write-Host "[OK] Node.js $nodeVersion"
} catch {
    Write-Host "[ERROR] Node.js не найден. Установи Node.js 18+: https://nodejs.org/"
    exit 1
}

# Проверка Java
try {
    $javaVersion = & java -version 2>&1 | Select-Object -First 1
    Write-Host "[OK] Java: $javaVersion"
} catch {
    Write-Host "[ERROR] Java JDK не найден. Установи JDK 17+: https://adoptium.net/"
    exit 1
}

# Проверка Bubblewrap
$bw = Get-Command bubblewrap -ErrorAction SilentlyContinue
if (-not $bw) {
    Write-Host "[WARN] Bubblewrap не найден. Устанавливаю..."
    & npm install -g bubblewrap 2>&1 | Out-Null
}

# 1. Инициализация TWA-проекта
Write-Host "[1/4] Инициализация TWA-проекта..."
New-Item -ItemType Directory -Path $androidDir -Force | Out-Null
Set-Location $androidDir

# Создать temporary manifest с реальным host
$manifest = Get-Content "$root\twa-manifest.json" -Raw | ConvertFrom-Json
$manifest.host = $HostUrl
$manifest | ConvertTo-Json -Depth 10 | Set-Content "$androidDir\twa-manifest.json" -Encoding UTF8

# Генерация Android-проекта через Bubblewrap init
& bubblewrap init --manifest "$androidDir\twa-manifest.json" --directory "$androidDir" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] bubblewrap init failed"
    exit 1
}

# 2. Копировать assets (иконки, CSS) — они нужны для офлайн-режима
Write-Host "[2/4] Копирую ассеты..."
$assetsDir = "$androidDir\app\src\main\assets"
New-Item -ItemType Directory -Path $assetsDir -Force | Out-Null
Copy-Item "$root\static\css\pikabu.css" "$assetsDir\"
Copy-Item "$root\static\js\reader.js" "$assetsDir\"
Copy-Item "$root\static\icons" "$assetsDir\" -Recurse

# 3. Сборка APK
Write-Host "[3/4] Собираю APK (это займёт 5–15 минут)..."
Set-Location $androidDir
& bubblewrap build 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] bubblewrap build failed"
    exit 1
}

# 4. Копировать APK в dist
Write-Host "[4/4] Копирую APK..."
New-Item -ItemType Directory -Path "$root\$OutDir" -Force | Out-Null
$apkPath = "$androidDir\app\build\outputs\apk\debug\app-debug.apk"
if (Test-Path $apkPath) {
    Copy-Item $apkPath "$root\$OutDir\pikabureader-android.apk"
    $size = (Get-Item "$root\$OutDir\pikabureader-android.apk").Length / 1MB
    Write-Host ""
    Write-Host "=== Готово ==="
    Write-Host "APK: $root\$OutDir\pikabureader-android.apk"
    Write-Host "Размер: $([math]::Round($size, 1)) МБ"
    Write-Host ""
    Write-Host "Установка на устройство:"
    Write-Host "  adb install $root\$OutDir\pikabureader-android.apk"
    Write-Host ""
    Write-Host "Примечание: для Google Play нужен подписанный релиз-APK."
    Write-Host "Перед установкой на телефон включи 'Установка из неизвестных источников'"
    Write-Host "  (или используй adb: adb install ...)"
} else {
    Write-Host "[ERROR] APK не найден: $apkPath"
}

# Cleanup
Remove-Item -Path $workDir -Recurse -Force -ErrorAction SilentlyContinue
Set-Location $root
