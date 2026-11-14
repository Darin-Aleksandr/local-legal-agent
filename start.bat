@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ЮрАгент
echo Запускаю ЮрАгент... Окно откроется в браузере. Не закрывайте это окно, пока работаете.
uv run lexagent ui
pause
