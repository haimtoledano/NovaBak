@echo off
:: VM Backup Enterprise - Web UI Service
cd /d "%~dp0"
if not exist "data" mkdir "data"
echo [%date% %time%] Starting VM Backup Web Service... >> data\service.log
".venv\Scripts\python.exe" -u main.py >> data\service.log 2>&1
