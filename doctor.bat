@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title ЮрАгент - проверка
curl -s -o nul http://127.0.0.1:11434/api/tags || start "" /min ollama serve
timeout /t 3 >nul
uv run lexagent doctor
echo.
echo Если есть строки с красным крестиком - сфотографируйте окно и отправьте.
pause
