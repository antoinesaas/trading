@echo off
chcp 65001 >nul
title Test des agents IA
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Lancez d'abord "Lancer le bot.bat" pour installer le bot.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m app check-ai
pause
