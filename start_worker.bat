@echo off
:: VM Backup Enterprise - Background Worker Daemon
cd /d "%~dp0"
if not exist "data" mkdir "data"
echo [%date% %time%] Starting background Worker Daemon... >> data\worker.log
".venv\Scripts\python.exe" -u worker_daemon.py >> data\worker.log 2>&1
