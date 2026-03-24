@echo off
:: VM Backup Enterprise - Startup Script
:: This script is designed to be executed by the Windows Task Scheduler on boot.

:: Navigate to the application directory
cd /d "C:\Users\haim\Documents\AG\VMBackup"

:: Ensure the data directory exists for logs
if not exist "data" mkdir "data"

:: Start the background daemon
echo [%date% %time%] Starting background Worker Daemon... >> data\service.log
start /B python worker_daemon.py >> data\worker.log 2>&1

:: Run the FastAPI server and append all output to service.log
echo [%date% %time%] Starting VM Backup Web Service... >> data\service.log
python main.py >> data\service.log 2>&1
