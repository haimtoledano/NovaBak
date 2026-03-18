@echo off
color 0B
echo ==========================================================
echo          VM Backup Enterprise Auto-Installer
echo ==========================================================

:: Check for Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Requesting Administrator Privileges...
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    exit /B
)

:: Run from current directory
cd /d "%~dp0"

echo.
echo [0/4] Stopping existing service if running...
powershell -ExecutionPolicy Bypass -Command "Stop-ScheduledTask -TaskName 'VMBackupEnterprise' -ErrorAction SilentlyContinue"
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo.
echo [1/4] Setting up Python Virtual Environment...
:: Remove old venv if it was copied from another machine (paths break otherwise)
if exist ".venv" rmdir /s /q ".venv"
python -m venv .venv
call .venv\Scripts\activate.bat

echo.
echo [2/4] Installing Required Packages...
pip install -r requirements.txt
if %errorLevel% neq 0 (
    color 0C
    echo [ERROR] Failed to install Python dependencies! Check your internet connection.
    pause
    exit /B
)

echo.
echo [3/4] Generating Startup Service...
echo @echo off > start_service.bat
echo cd /d "%~dp0" >> start_service.bat
echo call .venv\Scripts\activate.bat >> start_service.bat
echo if not exist "data" mkdir "data" >> start_service.bat
echo echo [%%date%% %%time%%] Starting VM Backup Service... ^>^> data\service.log >> start_service.bat
echo python main.py ^>^> data\service.log 2^>^&1 >> start_service.bat

echo.
echo [4/4] Registering Windows Background Service...
powershell -ExecutionPolicy Bypass -Command "$Action = New-ScheduledTaskAction -Execute '%~dp0start_service.bat'; $Trigger = New-ScheduledTaskTrigger -AtStartup; $Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit 0; Register-ScheduledTask -TaskName 'VMBackupEnterprise' -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'VM Backup Enterprise Server' -Force; Start-ScheduledTask -TaskName 'VMBackupEnterprise'"

echo.
echo ==========================================================
echo INSTALLATION COMPLETE! 
echo The system is now running in the background.
echo You can access the UI at: http://localhost:8000
echo ==========================================================
pause
