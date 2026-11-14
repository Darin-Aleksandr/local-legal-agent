# Установщик ЮрАгента для Windows 10/11. Обычно его запускает bootstrap.ps1 (одна команда из README).
# Можно и вручную: правой кнопкой по файлу → «Выполнить с помощью PowerShell».
# Ставит Ollama, Tesseract с русским языком, LibreOffice, uv; скачивает модели; создаёт ярлыки; открывает окно.
# Повторный запуск безопасен: уже установленное пропускается.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = "1"
Set-Location $Repo

$Steps = 7
$script:Step = 0
function Step($text, $wait) {
    $script:Step++
    Write-Host "`n[$script:Step/$Steps] $text" -ForegroundColor Cyan
    if ($wait) { Write-Host "    $wait" -ForegroundColor DarkGray }
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function RefreshPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                (Join-Path $env:LOCALAPPDATA "Programs\Ollama") + ";" + (Join-Path $env:USERPROFILE ".local\bin")
}
function WingetInstall($id) {
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent --disable-interactivity
    # -1978335189 и -1978335135: уже установлено / новее не нужно
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189 -and $LASTEXITCODE -ne -1978335135) {
        throw "не удалось установить $id (код $LASTEXITCODE)"
    }
    RefreshPath
}
function Download($url, $file) { Invoke-WebRequest $url -OutFile $file -UseBasicParsing }
function OllamaUp {
    try { Invoke-WebRequest "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}

try {
    Write-Host "Установка ЮрАгента. Займёт 20–40 минут, почти всё время — скачивание. Компьютер можно не трогать." -ForegroundColor Green
    RefreshPath
    if (-not (Have winget)) {
        Start-Process "ms-windows-store://pdp/?productid=9NBLGGH4NNS1"
        throw "в Windows нет winget. Открылся Microsoft Store: нажмите «Обновить» у «Установщик приложений», потом запустите установку снова"
    }

    Step "Ollama — программа, которая запускает нейросеть на этом компьютере" "≈ 1 ГБ"
    if (-not (Have ollama)) { WingetInstall "Ollama.Ollama" }

    Step "Tesseract — распознавание сканов и фотографий" "≈ 50 МБ"
    $Tess = "C:\Program Files\Tesseract-OCR"
    if (-not (Test-Path "$Tess\tesseract.exe")) { WingetInstall "UB-Mannheim.TesseractOCR" }
    foreach ($lang in @("rus", "osd")) {
        if (-not (Test-Path "$Tess\tessdata\$lang.traineddata")) {
            Download "https://github.com/tesseract-ocr/tessdata_fast/raw/main/$lang.traineddata" "$Tess\tessdata\$lang.traineddata"
        }
    }

    Step "LibreOffice — сборка PDF для суда" "≈ 350 МБ"
    if (-not (Test-Path "C:\Program Files\LibreOffice\program\soffice.exe")) { WingetInstall "TheDocumentFoundation.LibreOffice" }

    Step "Python-часть программы" "≈ 400 МБ"
    if (-not (Have uv)) { WingetInstall "astral-sh.uv" }
    if (-not (Test-Path "$Repo\config.toml")) { Copy-Item "$Repo\config.example.toml" "$Repo\config.toml" }
    uv sync
    if ($LASTEXITCODE -ne 0) { throw "не удалось установить Python-часть (uv sync)" }

    Step "Модели нейросети" "≈ 8 ГБ, это самый долгий шаг. Если прервётся — запустите установку снова, докачается"
    if (-not (OllamaUp)) {
        Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
        for ($i = 0; $i -lt 30 -and -not (OllamaUp); $i++) { Start-Sleep 2 }
    }
    $cfg = Get-Content "$Repo\config.toml" -Raw -Encoding UTF8
    $Models = @("qwen3.5:9b", "bge-m3")
    if ($cfg -match 'chat\s*=\s*"([^"]+)"') { $Models[0] = $Matches[1] }
    if ($cfg -match 'embed\s*=\s*"([^"]+)"') { $Models[1] = $Matches[1] }
    foreach ($m in $Models) {
        ollama pull $m
        if ($LASTEXITCODE -ne 0) { throw "не скачалась модель $m — проверьте интернет и запустите установку снова" }
    }

    Step "Ярлыки на рабочем столе"
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $Shell = New-Object -ComObject WScript.Shell
    $Data = Join-Path $env:USERPROFILE "ЮрАгент"
    New-Item -ItemType Directory -Path "$Data\Входящие" -Force | Out-Null
    $links = @(
        @{ Name = "ЮрАгент"; Target = "$Repo\start.bat"; Icon = "shell32.dll,23" },
        @{ Name = "ЮрАгент — папка с делами"; Target = $Data; Icon = "shell32.dll,3" },
        @{ Name = "ЮрАгент — проверка"; Target = "$Repo\doctor.bat"; Icon = "shell32.dll,22" },
        @{ Name = "ЮрАгент — обновить программу"; Target = "$Repo\update.bat"; Icon = "shell32.dll,238" }
    )
    foreach ($l in $links) {
        $s = $Shell.CreateShortcut("$Desktop\$($l.Name).lnk")
        $s.TargetPath = $l.Target
        $s.WorkingDirectory = $Repo
        $s.IconLocation = $l.Icon
        $s.Save()
    }

    Step "Проверка"
    uv run lexagent doctor
    $ok = $LASTEXITCODE -eq 0

    if ($ok) {
        Write-Host "`nГотово! Сейчас откроется окно ЮрАгента в браузере." -ForegroundColor Green
        Write-Host "В следующий раз запускайте ярлык «ЮрАгент» на рабочем столе."
        Start-Process "$Repo\start.bat" -WorkingDirectory $Repo
    } else {
        Write-Host "`nПочти готово, но проверка нашла проблему (строки с ❌ выше)." -ForegroundColor Yellow
        Write-Host "Сфотографируйте это окно и отправьте тому, кто дал вам ссылку."
    }
} catch {
    Write-Host "`nУстановка остановилась: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Запустите установку ещё раз — уже скачанное повторно не качается."
    Write-Host "Не помогло — сфотографируйте это окно и отправьте тому, кто дал вам ссылку."
}
Read-Host "`nНажмите Enter, чтобы закрыть это окно"
