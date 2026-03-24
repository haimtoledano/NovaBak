@echo off
color 0B
echo ==========================================================
echo           Building VM Backup Release Package
echo ==========================================================
echo.

:: Ensure run from script directory
cd /d "%~dp0"

echo [1/3] Preparing Build Directory...
if exist "VMBackupEnterprise_Release.zip" del /q "VMBackupEnterprise_Release.zip"
if exist "build_temp" rmdir /s /q "build_temp"

mkdir "build_temp"
mkdir "build_temp\templates"

echo [2/3] Copying Core Application Files...
copy main.py build_temp\ >nul
copy worker.py build_temp\ >nul
copy worker_daemon.py build_temp\ >nul
copy models.py build_temp\ >nul
copy auth.py build_temp\ >nul
copy esxi_handler.py build_temp\ >nul
copy backup_engine.py build_temp\ >nul
copy config_env.py build_temp\ >nul
copy logger_util.py build_temp\ >nul
copy storage_util.py build_temp\ >nul
copy start_web.bat build_temp\ >nul
copy start_worker.bat build_temp\ >nul


copy requirements.txt build_temp\ >nul
copy setup.bat build_temp\ >nul
copy templates\* build_temp\templates\ >nul

echo Waiting for file system to settle...
timeout /t 2 /nobreak >nul

echo [3/3] Zipping the Release Package...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'build_temp\*' -DestinationPath 'VMBackupEnterprise_Release.zip' -Force"

echo Cleaning up temp files...
rmdir /s /q "build_temp"

echo.
echo ==========================================================
echo SUCCESS! Your production deployment file is ready:
echo File: VMBackupEnterprise_Release.zip
echo ==========================================================
echo.
echo To upgrade or install on the Win2019 server:
echo 1. Copy 'VMBackupEnterprise_Release.zip' to the server.
echo 2. Extract it into your existing installation folder (overwrite identical files).
echo    - Important: This will NOT overwrite or delete your 'data' folder (your DB/Logs are safe).
echo 3. Right click 'setup.bat' in the server and 'Run as Administrator'.
echo.
pause
