import os
import subprocess
import datetime
import smtplib
from email.message import EmailMessage
from apscheduler.schedulers.background import BackgroundScheduler
import esxi_handler
from models import SessionLocal, Config, VM, BackupLog
from config_env import OVFTOOL_PATH

def cleanup_old_backups(config, vm_name, retention_count):
    """ Deletes backup folders for the specific VM, keeping only the newest `retention_count` folders. """
    vm_dir = os.path.join(config.smb_unc_path, vm_name)
    if not os.path.exists(vm_dir):
        return
        
    folders = []
    for item in os.listdir(vm_dir):
        item_path = os.path.join(vm_dir, item)
        if os.path.isdir(item_path):
            folders.append(item_path)
            
    # Sort folders alphabetically descending (since name is YYYY-MM-DD, this is newest first)
    folders.sort(reverse=True)
    
    # Keep the first `retention_count` folders, delete the rest
    if retention_count < 1:
        retention_count = 1 # Safety net
        
    folders_to_delete = folders[retention_count:]
    for f in folders_to_delete:
        print(f"Retention: Removing old backup directory {f}")
        import shutil
        shutil.rmtree(f, ignore_errors=True)

def send_email_report(config, logs_today):
    if not config.smtp_server or not config.smtp_to_email:
        print("SMTP not configured. Skipping email.")
        return

    msg = EmailMessage()
    msg['Subject'] = f"VM Backup Report - {datetime.date.today()}"
    msg['From'] = config.smtp_user if config.smtp_user else "vmbackup@local"
    msg['To'] = config.smtp_to_email

    content = "Daily VM Backup Report\n\n"
    for log in logs_today:
        content += f"VM: {log.vm_name}\nStatus: {log.status}\nMessage: {log.message}\nTime: {log.timestamp}\n\n"

    msg.set_content(content)

    try:
        with smtplib.SMTP(config.smtp_server, config.smtp_port) as server:
            if config.smtp_user and config.smtp_password:
                server.starttls()
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)
        print("Email report sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}")

def authenticate_smb(config):
    """ Authenticates to the SMB share on Windows using net use. Returns (bool, str) """
    if os.name == 'nt' and config.smb_unc_path:
        user_str = config.smb_user if hasattr(config, 'smb_user') else ""
        if user_str:
            print(f"Authenticating to SMB share: {config.smb_unc_path} with user {user_str}")
        
        # Disconnect just in case there's a stale connection
        subprocess.run(["net", "use", config.smb_unc_path, "/delete", "/y"], capture_output=True)
        
        # Connect
        cmd = ["net", "use", config.smb_unc_path]
        if hasattr(config, 'smb_password') and config.smb_password and user_str:
            cmd.extend([config.smb_password, f"/user:{user_str}"])
            
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            msg = f"Warning: Failed to authenticate to SMB: {res.stderr}"
            print(msg)
            return False, msg
        else:
            msg = "Successfully authenticated to SMB."
            print(msg)
            return True, msg
            
    return True, "Authentication skipped (not Windows or no UNC path)."

def get_backup_dest_folder(config, vm_name):
    """ Constructs the destination SMB folder string for ovftool. """
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    dest_dir = os.path.join(config.smb_unc_path, vm_name, date_str)
    os.makedirs(dest_dir, exist_ok=True) 
    return dest_dir

active_processes = {}

def stop_job(vm_id: int):
    """ Terminate an active OVFTool backup process. """
    if vm_id in active_processes:
        try:
            active_processes[vm_id].terminate()
            return True
        except:
            pass
    return False

def perform_backup(vm_id: int):
    """ Backs up a specific VM. """
    db = SessionLocal()
    config = db.query(Config).first()
    vm = db.query(VM).filter(VM.id == vm_id).first()

    if not config or not vm or not vm.esxi_host:
        print(f"Skipping backup for VM ID {vm_id}: Config, VM, or ESXiHost missing.")
        db.close()
        return

    host = vm.esxi_host
    print(f"Starting backup for {vm.vm_name} at {datetime.datetime.now()} from {host.name}")
    
    # Authenticate to SMB
    authenticate_smb(config)

    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        msg = f"Failed to connect to ESXi host {host.name} for {vm.vm_name}."
        db.add(BackupLog(vm_name=vm.vm_name, status="Failed", message=msg))
        db.commit()
        db.close()
        return

    try:
        dest_dir = get_backup_dest_folder(config, vm.vm_name)
        ova_dest = os.path.join(dest_dir, f"{vm.vm_name}.ova")
        
        # Check if today's backup already exists
        # if os.path.exists(ova_dest):
        #     print(f"Backup for {vm.vm_name} already exists for today. Skipping.")
        #     return

        vm.current_action = "Creating Snapshot..."
        vm.progress = 0
        db.commit()
        
        # Create temporary crash-consistent snapshot for live backup
        snap_success = esxi_handler.create_snapshot(si, vm.vm_name)
        if not snap_success:
            raise Exception("Failed to create temporary VM snapshot.")
            
        vm.current_action = "Exporting OVA..."
        db.commit()
        
        source_uri = f"vi://{host.username}:{host.password}@{host.host_ip}/{vm.vm_name}"
        cmd = [
            OVFTOOL_PATH,
            "--noSSLVerify",
            "--acceptAllEulas",
            "--overwrite",
            source_uri,
            ova_dest
        ]
        
        import re
        # Unbuffered binary output to catch \r in real-time
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        active_processes[vm_id] = process
        last_prog = 0
        ovftool_output = []
        
        current_line = []
        while True:
            char_b = process.stdout.read(1)
            if not char_b and process.poll() is not None:
                break
            if not char_b:
                continue
                
            char = char_b.decode('utf-8', errors='replace')
            if char in ('\r', '\n'):
                line = "".join(current_line).strip()
                current_line = []
                if line:
                    if len(ovftool_output) < 30:
                        ovftool_output.append(line)
                    else:
                        ovftool_output.pop(0)
                        ovftool_output.append(line)
                        
                    # Catch OVFtool output variations: "Disk transfer: 12%", "Progress: 12"
                    match = re.search(r"(?:progress|transfer|writing|disk)[^\d]*(\d+)\s*(?:%|\b)", line, re.IGNORECASE)
                    if match:
                        prog = int(match.group(1))
                        if prog > last_prog and prog <= 100:
                            vm.progress = prog
                            db.commit()
                            last_prog = prog
            else:
                current_line.append(char)
                    
        process.wait()
        success = (process.returncode == 0)
        vm.progress = 100 if success else 0
        vm.current_action = ""
        
        # Update VM status
        vm.last_backup = datetime.datetime.now()
        vm.last_status = "Success" if success else "Failed"
        
        if success:
            log_msg = f"Backup completed for {vm.vm_name}"
        else:
            # Join all gathered lines to make sure the actual error message doesn't get sliced out
            full_out = " | ".join(ovftool_output)
            log_msg = f"OVFTool Error: {full_out}"
            
        db.add(BackupLog(vm_name=vm.vm_name, status=vm.last_status, message=log_msg))
        
        # Enforce retention policy
        cleanup_old_backups(config, vm.vm_name, vm.retention_count)
        
        if not success:
            send_email_report(config, [BackupLog(vm_name=vm.vm_name, status="Failed", message=log_msg)])

    except Exception as e:
        print(f"Error during backup of {vm.vm_name}: {e}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Failed", message=str(e)))
        vm.progress = 0
        vm.current_action = ""
        
    finally:
        active_processes.pop(vm_id, None)
        try:
            vm.current_action = "Consolidating Snapshot..."
            db.commit()
            
            esxi_handler.remove_snapshot(si, vm.vm_name)
            
            vm.progress = 0
            vm.current_action = ""
            db.commit()
        except:
            pass
        esxi_handler.Disconnect(si)
        db.close()

# Shared scheduler instance
scheduler = BackgroundScheduler()

def start_scheduler():
    """ Initialize APScheduler and load all active jobs from DB. """
    db = SessionLocal()
    vms = db.query(VM).filter(VM.is_selected == True, VM.is_job_active == True).all()

    # Clear existing jobs to avoid duplicates on restart/config change
    for job in scheduler.get_jobs():
        job.remove()
    
    for vm in vms:
        job_id = f"backup_{vm.id}"
        scheduler.add_job(
            perform_backup, 
            'cron', 
            hour=vm.schedule_hour, 
            minute=vm.schedule_minute, 
            args=[vm.id],
            id=job_id
        )
        print(f"Scheduled job {job_id} for {vm.vm_name} at {vm.schedule_hour:02d}:{vm.schedule_minute:02d}")
    
    if not scheduler.running:
        scheduler.start()
    db.close()
    return scheduler

def get_available_backups(config):
    """ Scans the SMB share and returns a list of available backups. """
    authenticate_smb(config)
    
    backups = []
    if not config.smb_unc_path or not os.path.exists(config.smb_unc_path):
        return backups
        
    try:
        for vm_name in os.listdir(config.smb_unc_path):
            vm_dir = os.path.join(config.smb_unc_path, vm_name)
            if os.path.isdir(vm_dir):
                for date_folder in os.listdir(vm_dir):
                    date_dir = os.path.join(vm_dir, date_folder)
                    if os.path.isdir(date_dir):
                        # Check if an OVA exists inside
                        ova_file = os.path.join(date_dir, f"{vm_name}.ova")
                        if os.path.exists(ova_file):
                            size_bytes = os.path.getsize(ova_file)
                            size_str = f"{size_bytes / (1024**3):.2f} GB" if size_bytes > 1024**3 else f"{size_bytes / (1024**2):.2f} MB"
                            
                            backups.append({
                                "vm_name": vm_name,
                                "date": date_folder,
                                "path": ova_file,
                                "size": size_str
                            })
    except Exception as e:
        print(f"Error scanning backups directory: {e}")
    # Sort by date descending
    backups.sort(key=lambda x: x["date"], reverse=True)
    return backups

def perform_restore(config, target_ip, target_user, target_password, source_ova, target_name, datastore):
    """ Executes ovftool to inject the OVA back to a target ESXi host. """
    print(f"Starting Restore for OVA: {source_ova} -> {target_name} on {datastore} ({target_ip})")
    try:
        if not os.path.exists(source_ova):
            print(f"Restore Failed: Source file {source_ova} not found.")
            return

        target_uri = f"vi://{target_user}:{target_password}@{target_ip}/"

        cmd = [
            OVFTOOL_PATH,
            "--noSSLVerify",
            "--acceptAllEulas",
            f"--name={target_name}",
            f"--datastore={datastore}",
            source_ova,
            target_uri
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Restore successful for {target_name}")
        else:
            print(f"Restore Failed for {target_name}: {result.stderr[:500]}")
            
    except Exception as e:
        print(f"Restore Exception: {e}")
