@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title ЮрАгент
curl -s -o nul http://127.0.0.1:11434/api/tags || start "" /min ollama serve
echo Запускаю ЮрАгент. Окно откроется в браузере.
echo Не закрывайте это чёрное окно, пока работаете.
uv run lexagent ui
pause
