# Установка ЮрАгента одной командой. Вставьте в PowerShell:
#
#   irm https://raw.githubusercontent.com/Darin-Aleksandr/local-legal-agent/main/install/bootstrap.ps1 | iex
#
# Что делает: один раз просит права администратора, скачивает программу, запускает установщик.
# Повторный запуск обновляет программу; ваши дела, библиотека и настройки не трогаются.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # без этого скачивание в PowerShell 5 идёт в разы медленнее
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Self = "https://raw.githubusercontent.com/Darin-Aleksandr/local-legal-agent/main/install/bootstrap.ps1"
$Zip = "https://github.com/Darin-Aleksandr/local-legal-agent/archive/refs/heads/main.zip"
$Target = Join-Path $env:LOCALAPPDATA "YurAgent"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Сейчас Windows спросит разрешение на установку — нажмите «Да»." -ForegroundColor Yellow
    $script = Join-Path $env:TEMP "yuragent-bootstrap.ps1"
    $web = New-Object Net.WebClient
    $web.Encoding = [Text.Encoding]::UTF8
    $text = $web.DownloadString($Self)
    [IO.File]::WriteAllText($script, $text, (New-Object Text.UTF8Encoding $true))
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
    return
}

try {
    Write-Host "Скачиваю ЮрАгент…" -ForegroundColor Cyan
    $tmp = Join-Path $env:TEMP "yuragent-$(Get-Random)"
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Invoke-WebRequest $Zip -OutFile "$tmp\main.zip" -UseBasicParsing
    Expand-Archive "$tmp\main.zip" -DestinationPath $tmp -Force
    $source = Get-ChildItem $tmp -Directory | Select-Object -First 1

    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    # обновление: код заменяется, а config.toml и окружение .venv остаются
    Get-ChildItem $Target -Force | Where-Object { $_.Name -notin @("config.toml", ".venv") } |
        Remove-Item -Recurse -Force
    Copy-Item "$($source.FullName)\*" $Target -Recurse -Force
    Get-ChildItem $Target -Recurse -File | Unblock-File
    Remove-Item $tmp -Recurse -Force

    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target "install\windows.ps1")
} catch {
    Write-Host "`nУстановка остановилась: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Сфотографируйте это окно и отправьте тому, кто дал вам ссылку."
    Read-Host "Нажмите Enter, чтобы закрыть"
}
