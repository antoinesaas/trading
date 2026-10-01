@echo off
chcp 65001 >nul
title Bot de trading - PAPER TRADING
cd /d "%~dp0"

rem --- 1. Installation (premiere fois seulement) -----------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Premiere installation : creation de l'environnement Python...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo Python est introuvable. Installez-le depuis https://www.python.org/downloads/ puis relancez.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Echec de l'installation des dependances.
        pause
        exit /b 1
    )
)

rem --- 2. Configuration (.env : cle API Claude, secrets) -------------------
if not exist ".env" (
    ".venv\Scripts\python.exe" -m app init-env
    echo.
    echo Collez votre cle Claude sur la ligne ANTHROPIC_API_KEY= du fichier .env, enregistrez, puis relancez.
    notepad ".env"
    pause
    exit /b 1
)

set "TOKEN="
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"DASHBOARD_TOKEN=" ".env"') do set "TOKEN=%%b"
set "PORT=8000"
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"PORT=" ".env"') do set "PORT=%%b"

rem --- 3. Lancement --------------------------------------------------------
echo.
echo   Dashboard : http://127.0.0.1:%PORT%/dashboard/
echo   Le navigateur s'ouvre dans quelques secondes.
echo   Pour arreter le bot : fermez cette fenetre (ou Ctrl+C).
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 6; Start-Process 'http://127.0.0.1:%PORT%/dashboard/#token=%TOKEN%'"
".venv\Scripts\python.exe" -m app serve
pause
