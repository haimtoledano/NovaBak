# Auto-installer for Windows Scheduled Task

$TaskName = "VMBackupEnterprise"
$ScriptPath = "C:\Users\haim\Documents\AG\VMBackup\start_service.bat"

# Create action to run the batch file
$action = New-ScheduledTaskAction -Execute $ScriptPath

# Set trigger to run at System Startup
$trigger = New-ScheduledTaskTrigger -AtStartup

# Set permissions to run as SYSTEM with Highest Privileges (Administrator)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Set task settings (don't stop on idle, run indefinitely)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit 0

# Register the task
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Runs the Enterprise VM Backup web server in the background" -Force

Write-Host "✅ Service installed successfully as a Windows Scheduled Task!"
Write-Host "The server will now automatically start every time the Windows Server restarts."
Write-Host "To start it manually right now, you can run: Start-ScheduledTask -TaskName '$TaskName'"
