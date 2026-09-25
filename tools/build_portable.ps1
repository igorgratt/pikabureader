# build_portable.ps1 — создаёт Windows-дистрибутив PikaBuReader без установки
# Запуск: powershell -ExecutionPolicy Bypass -File tools\build_portable.ps1
# Требуется: интернет (для скачивания Python embeddable и зависимостей)
param(
    [string]$Version = "0.15.0",
    [string]$PythonUrl = "https://www.python.org/ftp/python/3.13.0/python-3.13.0-embed-amd64.zip",
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path "$here\..").Path
$workDir = "$env:TEMP\pikabu_build_$PID"
$portableDir = "$workDir\pikabureader-$Version-win64"

Write-Host "=== PikaBuReader $Version portable builder ==="
Write-Host "Work dir: $workDir"

# 1. Скачать Python embeddable
$pythonZip = "$workDir\python.zip"
Write-Host "[1/6] Скачиваю Python embeddable..."
Invoke-WebRequest -Uri $PythonUrl -OutFile $pythonZip -UserAgent "pikabureader-builder"
Expand-Archive -Path $pythonZip -DestinationPath $portableDir -Force
Remove-Item $pythonZip

# 2. Настроить pip в embeddable Python (нужен get-pip.py)
$pythonExe = "$portableDir\python.exe"
$pythonDir = Split-Path -Parent $pythonExe
Write-Host "[2/6] Настраиваю pip..."
$env:PYTHONPATH = ""
$env:PYTHON_HOME = $portableDir
$pipUrl = "https://bootstrap.pypa.io/get-pip.py"
Invoke-WebRequest -Uri $pipUrl -OutFile "$portableDir\get-pip.py" -UserAgent "pikabureader-builder"
& $pythonExe "$portableDir\get-pip.py" --no-warn-script-location 2>&1 | Out-Null
Remove-Item "$portableDir\get-pip.py"

# Убираем zip из пути (мешает pip)
$pythonTxt = "$portableDir\python313._pth"
if (Test-Path $pythonTxt) {
    $content = Get-Content $pythonTxt -Raw
    $content = $content -replace "^import site\r?\n?", ""
    Set-Content -Path $pythonTxt -Value $content
}

# 3. Копировать код приложения
Write-Host "[3/6] Копирую код приложения..."
Copy-Item -Path "$root\app.py" -Destination $portableDir
Copy-Item -Path "$root\db.py" -Destination $portableDir
Copy-Item -Path "$root\pika.py" -Destination $portableDir
Copy-Item -Path "$root\parsers" -Destination $portableDir -Recurse
Copy-Item -Path "$root\templates" -Destination $portableDir -Recurse -Filter "*.html"
Copy-Item -Path "$root\static" -Destination $portableDir -Recurse
Copy-Item -Path "$root\docs" -Destination $portableDir -Recurse
Copy-Item -Path "$root\requirements.txt" -Destination $portableDir

# 4. Установить зависимости
Write-Host "[4/6] Устанавливаю зависимости..."
$pip = "$portableDir\Scripts\pip.exe"
if (-not (Test-Path $pip)) {
    $pip = "$portableDir\Scripts\pip3.exe"
}
& $pip install --no-warn-script-location -q Flask "Pillow>=10.0" chardet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] pip returned $LASTEXITCODE — продолжаю (могут отсутствовать some deps)"
}

# 5. Создать README для портативной версии
$readme = @"
PikaBuReader $Version — Portable (Windows)
==========================================

Запуск: дважды кликни start.bat

Данные: папка data\ рядом с python.exe
(книги, БД, обложки — всё остаётся локально)

Порты: по умолчанию 8000 (изменить: set PORT=9000 && start.bat)

Браузер откроется автоматически.

Пароль: открой http://localhost:8000/admin и задай пароль,
иначе инстанс открыт для всех.

Для остановки — закрой окно консоли.

Версия Python: см. python.exe --version
"@
Set-Content -Path "$portableDir\README.txt" -Value $readme -Encoding UTF8

# 6. Создать start.bat
$startBat = @"
@echo off
title PikaBuReader $Version
set HOST=127.0.0.1
set PORT=8000
set SECRET_KEY=%~dp0data\secret_key
python.exe -X utf8 app.py
pause
"@
Set-Content -Path "$portableDir\start.bat" -Value $startBat -Encoding ASCII

# Создать папку data
New-Item -ItemType Directory -Path "$portableDir\data" -Force | Out-Null
# Создать .gitignore в data
Set-Content -Path "$portableDir\data\.gitignore" -Value "*`n*.db`n*.pkl`nbooks\`ncovers\`nfonts\`nimport_queue\`n" -Encoding ASCII

# 7. Архивировать
Write-Host "[5/6] Архивирую..."
New-Item -ItemType Directory -Path "$root\$OutDir" -Force | Out-Null
$zipPath = "$root\$OutDir\pikabureader-$Version-win64.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }
Compress-Archive -Path "$portableDir\*" -DestinationPath $zipPath -CompressionLevel Optimal

# 8. Cleanup
Write-Host "[6/6] Чищу..."
Remove-Item -Path $workDir -Recurse -Force

$size = (Get-Item $zipPath).Length / 1MB
Write-Host ""
Write-Host "=== Готово ==="
Write-Host "Архив: $zipPath"
Write-Host "Размер: $([math]::Round($size, 1)) МБ"
Write-Host ""
Write-Host "Для запуска:"
Write-Host "  1. Распаковать архив"
Write-Host "  2. Дважды кликнуть start.bat"
Write-Host "  3. Браузер откроется на http://localhost:8000"
