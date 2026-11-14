# Установка ЮрАгента на Windows 10/11. Запуск: правой кнопкой по файлу → «Выполнить с помощью PowerShell».
# Ставит Ollama, Tesseract (с русским языком), LibreOffice, uv; скачивает модели; создаёт ярлык на рабочем столе.
# Повторный запуск безопасен: уже установленное пропускается.

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function WingetInstall($id) {
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) { throw "Не удалось установить $id" }  # -1978335189 = уже стоит
}
function RefreshPath { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }

if (-not (Have winget)) { throw "Нет winget. Обновите «Установщик приложений» из Microsoft Store и запустите снова." }

Step "1/6 Ollama — локальная нейросеть"
if (-not (Have ollama)) { WingetInstall "Ollama.Ollama"; RefreshPath }

Step "2/6 Tesseract — распознавание сканов"
$Tess = "C:\Program Files\Tesseract-OCR"
if (-not (Test-Path "$Tess\tesseract.exe")) { WingetInstall "UB-Mannheim.TesseractOCR" }
foreach ($lang in @("rus", "osd")) {
    $file = "$Tess\tessdata\$lang.traineddata"
    if (-not (Test-Path $file)) {
        Write-Host "  скачиваю язык $lang"
        Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -Command `"Invoke-WebRequest https://github.com/tesseract-ocr/tessdata_fast/raw/main/$lang.traineddata -OutFile '$file'`""
    }
}

Step "3/6 LibreOffice — сборка PDF для суда"
if (-not (Test-Path "C:\Program Files\LibreOffice\program\soffice.exe")) { WingetInstall "TheDocumentFoundation.LibreOffice" }

Step "4/6 uv и Python-зависимости"
if (-not (Have uv)) { WingetInstall "astral-sh.uv"; RefreshPath }
uv sync

Step "5/6 Модели (≈ 7 ГБ, один раз)"
$Models = @("qwen3.5:9b", "bge-m3")
if (Test-Path "$Repo\config.toml") {
    $cfg = Get-Content "$Repo\config.toml" -Raw
    if ($cfg -match 'chat\s*=\s*"([^"]+)"') { $Models[0] = $Matches[1] }
    if ($cfg -match 'embed\s*=\s*"([^"]+)"') { $Models[1] = $Matches[1] }
} else {
    Copy-Item "$Repo\config.example.toml" "$Repo\config.toml"
}
foreach ($m in $Models) { ollama pull $m }

Step "6/6 Ярлык и проверка"
$Shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\ЮрАгент.lnk")
$Shortcut.TargetPath = "$Repo\start.bat"
$Shortcut.WorkingDirectory = $Repo
$Shortcut.IconLocation = "shell32.dll,1"
$Shortcut.Save()
uv run lexagent doctor

Write-Host "`nГотово. Запускайте «ЮрАгент» с рабочего стола — откроется окно в браузере." -ForegroundColor Green
Write-Host "Документы кладите в папку $env:USERPROFILE\ЮрАгент\Входящие или перетаскивайте в окно."
Read-Host "Нажмите Enter, чтобы закрыть"
