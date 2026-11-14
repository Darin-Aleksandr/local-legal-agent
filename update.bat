@echo off
chcp 65001 >nul
title ЮрАгент - обновление
echo Обновляю программу. Ваши дела и библиотека не трогаются.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Darin-Aleksandr/local-legal-agent/main/install/bootstrap.ps1 | iex"
