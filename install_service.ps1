# Auto-installer for Windows Scheduled Tasks (Split Engine)

$WebTaskName = "VMBackup_Web"
$WorkerTaskName = "VMBackup_Worker"

$WebScriptPath = "C:\VMBackup\VMBackup\start_web.bat"
$WorkerScriptPath = "C:\VMBackup\VMBackup\start_worker.bat"

# Task 1: Web Server
$webAction = New-ScheduledTaskAction -Execute $WebScriptPath
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit 0

Register-ScheduledTask -TaskName $WebTaskName -Action $webAction -Trigger $trigger -Principal $principal -Settings $settings -Description "Runs the VM Backup Web UI" -Force

# Task 2: Worker Daemon
$workerAction = New-ScheduledTaskAction -Execute $WorkerScriptPath
Register-ScheduledTask -TaskName $WorkerTaskName -Action $workerAction -Trigger $trigger -Principal $principal -Settings $settings -Description "Runs the VM Backup Background Worker Daemon" -Force

Write-Host "✅ Distributed Services installed successfully!"
Write-Host "The Control Plane ($WebTaskName) and Data Plane ($WorkerTaskName) will automatically start on boot."
Write-Host "To start them manually right now, run:"
Write-Host "Start-ScheduledTask -TaskName '$WebTaskName'"
Write-Host "Start-ScheduledTask -TaskName '$WorkerTaskName'"

