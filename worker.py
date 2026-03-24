import os
import threading
import subprocess
import datetime
import smtplib
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
import esxi_handler
import backup_engine
import storage_util
from models import SessionLocal, Config, VM, BackupLog, RestoreJob
from config_env import DATA_DIR
from logger_util import log_info, log_warn, log_error, log_debug

def cleanup_old_backups(storage, vm_name, retention_count):
    """ Deletes backup folders for the specific VM, keeping only the newest `retention_count` folders. """
    vm_dir = vm_name
    if not storage.exists(vm_dir):
        return
        
    folders = storage.list_dirs(vm_dir)
    # prepend VM name to get relative paths
    full_folders = [f"{vm_name}/{d}" for d in folders]
            
    # Sort folders alphabetically descending (since name is YYYY-MM-DD, this is newest first)
    full_folders.sort(reverse=True)
    
    # Keep the first `retention_count` folders, delete the rest
    if retention_count < 1:
        retention_count = 1 # Safety net
        
    folders_to_delete = full_folders[retention_count:]
    for f in folders_to_delete:
        log_info(f"Retention: Removing old backup directory {f}")
        storage.delete_dir(f)

def _smtp_send(config, to_addrs: list, subject: str, body: str):
    """Low-level helper — sends one email to a list of recipients."""
    if not config or not config.smtp_server or not to_addrs:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.smtp_user if config.smtp_user else "novabak@local"
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)
    try:
        if config.smtp_use_ssl:
            server = smtplib.SMTP_SSL(config.smtp_server, config.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(config.smtp_server, config.smtp_port, timeout=30)
        with server:
            if not config.smtp_use_ssl and config.smtp_use_tls:
                server.starttls()
            if config.smtp_user and config.smtp_password:
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)
        log_info(f"[EMAIL] Sent '{subject}' to {to_addrs}")
    except Exception as e:
        log_error(f"[EMAIL] Failed: {e}")


def send_event_notification(event_key: str, subject: str, body: str):
    """
    Sends a notification to every user who is subscribed to `event_key`
    and has a non-empty email address configured.
    """
    from models import User  # avoid circular import at module level
    db = SessionLocal()
    try:
        config = db.query(Config).first()
        if not config or not config.smtp_server:
            return
        users = db.query(User).all()
        recipients = [
            u.email for u in users
            if u.email and event_key in [s.strip() for s in (u.notify_subscriptions or "").split(",") if s.strip()]
        ]
        if recipients:
            _smtp_send(config, recipients, subject, body)
    finally:
        db.close()


def send_email_report(config, logs_today):
    """Legacy wrapper — sends a summary to the global smtp_to_email address."""
    if not config or not config.smtp_server or not config.smtp_to_email:
        log_info("SMTP not configured. Skipping email.")
        return
    body = "Daily VM Backup Report\n\n"
    for log in logs_today:
        body += f"VM: {log.vm_name}\nStatus: {log.status}\nMessage: {log.message}\nTime: {log.timestamp}\n\n"
    _smtp_send(config, [config.smtp_to_email],
               f"VM Backup Report - {datetime.date.today()}", body)


def authenticate_smb(config):
    """ Authenticates to the SMB share on Windows using net use. Returns (bool, str) """
    if os.name == 'nt' and config.smb_unc_path:
        user_str = config.smb_user if hasattr(config, 'smb_user') else ""
        if user_str:
            log_info(f"Authenticating to SMB share: {config.smb_unc_path} with user {user_str}")
        
        # Disconnect just in case there's a stale connection
        subprocess.run(["net", "use", config.smb_unc_path, "/delete", "/y"], capture_output=True)
        
        # Connect
        cmd = ["net", "use", config.smb_unc_path]
        if hasattr(config, 'smb_password') and config.smb_password and user_str:
            cmd.extend([config.smb_password, f"/user:{user_str}"])
            
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            msg = f"Warning: Failed to authenticate to SMB: {res.stderr}"
            log_warn(msg)
            return False, msg
        else:
            msg = "Successfully authenticated to SMB."
            log_info(msg)
            return True, msg
            
    return True, "Authentication skipped (not Windows or no UNC path)."

def get_backup_dest_folder(vm_name):
    """ Constructs the relative destination folder string. """
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    return f"{vm_name}/{date_str}"

active_processes = {}
backup_queue_executor = ThreadPoolExecutor(max_workers=10) # Total concurrent workers
restore_queue_executor = ThreadPoolExecutor(max_workers=5) # Concurrent restores
host_locks = {}
host_locks_lock = threading.Lock()
last_trigger_times = {} # vm_id -> timestamp

def get_host_lock(host_id):
    with host_locks_lock:
        if host_id not in host_locks:
            host_locks[host_id] = threading.Lock()
        return host_locks[host_id]

def queue_backup(vm_id: int):
    """ Places a backup job in the queue to run. Allows up to 10 simultaneous jobs total across all hosts. """
    now = datetime.datetime.now().timestamp()
    pid = os.getpid()
    
    # Check cooldown
    if vm_id in last_trigger_times:
        diff = now - last_trigger_times[vm_id]
        if diff < 65:
            log_debug(f"[PID {pid}] Skipping queue for VM {vm_id}: Cooldown active ({int(diff)}s < 65s)")
            return
            
    db = SessionLocal()
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        if vm.current_action != "" and vm.current_action != "Queued...":
            log_debug(f"[PID {pid}] Skipping queue for VM {vm.vm_name}: Already active (Status: {vm.current_action})")
            db.close()
            return
            
        log_info(f"[PID {pid}] Queueing backup for VM: {vm.vm_name}")
        last_trigger_times[vm_id] = now
        vm.current_action = "Queued..."
        vm.progress = 0
        db.commit()
        backup_queue_executor.submit(perform_backup, vm_id)
    else:
        log_error(f"[PID {pid}] queue_backup called for non-existent VM ID: {vm_id}")
        
    db.close()

def stop_job(vm_id: int):
    """ Terminate an active backup process for a VM. """
    pid = os.getpid()
    log_info(f"[PID {pid}] Stop request received for VM ID: {vm_id}")
    if vm_id in active_processes:
        try:
            p = active_processes[vm_id]
            log_info(f"[PID {pid}] Terminating backup process for VM ID: {vm_id}")
            p.terminate()
            return True
        except Exception as e:
            log_error(f"[PID {pid}] Failed to terminate process for VM {vm_id}: {e}")
    else:
        log_warn(f"[PID {pid}] Stop requested for VM {vm_id} but no active process found in this instance.")
    return False

def perform_backup(vm_id: int):
    """ Backs up a specific VM using the native pyVmomi engine. Runs in parallel with other VMs. """
    pid = os.getpid()
    db = SessionLocal()
    config = db.query(Config).first()
    vm = db.query(VM).filter(VM.id == vm_id).first()

    if not config or not vm or not vm.esxi_host:
        log_error(f"[PID {pid}] perform_backup aborted: Missing config/vm/host for ID {vm_id}")
        db.close()
        return

    host = vm.esxi_host
    log_info(f"[PID {pid}] Starting parallel backup for {vm.vm_name} on host {host.name}")
    
    storage = storage_util.get_storage(config)

    # SMB Authentication (only relevant for SMB storage type)
    if config.storage_type == "SMB":
        authenticate_smb(config)

    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        msg = f"Failed to connect to ESXi host {host.name}"
        log_error(f"[PID {pid}] {msg} for {vm.vm_name}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Failed", message=msg))
        vm.current_action = ""
        vm.progress = 0
        db.commit()
        db.close()
        return

    powered_off_by_us = False  # Track if we shut down the VM so we can restore it

    try:
        timeout_m = config.backup_timeout_mins if hasattr(config, 'backup_timeout_mins') else 15
        dest_rel_dir = get_backup_dest_folder(vm.vm_name)

        # --- POWER OFF (if configured) ---
        if getattr(vm, 'power_off_for_backup', False):
            from pyVmomi import vim as _vim
            content = si.RetrieveContent()
            esxi_vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm.vm_name}")
            current_power = getattr(esxi_vm.runtime, 'powerState', 'poweredOff') if esxi_vm else 'poweredOff'

            if current_power != 'poweredOff':
                vm.current_action = "Shutting down VM..."
                vm.progress = 0
                db.commit()
                log_info(f"[PID {pid}] Power-off-for-backup enabled. Shutting down {vm.vm_name}...")
                ok, msg = esxi_handler.shutdown_vm(si, vm.vm_name, graceful_timeout_mins=5)
                if not ok:
                    raise Exception(f"Shutdown failed: {msg}")
                powered_off_by_us = True
                send_event_notification(
                    "vm_powered_off",
                    f"[NovaBak] VM Powered Off: {vm.vm_name}",
                    f"VM '{vm.vm_name}' was powered off to perform a fast backup on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}."
                )
                log_info(f"[PID {pid}] {vm.vm_name} is now off. Proceeding with fast backup.")
            else:
                log_info(f"[PID {pid}] power_off_for_backup: VM already off, skipping shutdown step.")

        # --- PREFLIGHT ---
        send_event_notification(
            "backup_start",
            f"[NovaBak] Backup Started: {vm.vm_name}",
            f"Backup job for VM '{vm.vm_name}' on host '{host.name}' has started at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}."
        )
        vm.current_action = "Preflight checks..."
        vm.progress = 0
        db.commit()

        ok, msg = backup_engine.preflight_check(si, vm.vm_name, timeout_mins=timeout_m)
        if not ok:
            raise Exception(f"Preflight failed: {msg}")

        # --- BACKUP ---
        vm.current_action = "Backing up VM..."
        vm.progress = 0
        db.commit()

        def progress_cb(pct):
            try:
                vm.progress = pct
                db.commit()
            except Exception:
                pass

        def speed_cb(mbps):
            try:
                vm.speed_mbps = mbps
                vm.current_action = f"Backing up... {mbps:.1f} MB/s"
                db.commit()
            except Exception:
                pass

        success, result_msg = backup_engine.export_vm_native(
            si=si,
            vm_name=vm.vm_name,
            storage=storage,
            dest_rel_dir=dest_rel_dir,
            progress_callback=progress_cb,
            speed_callback=speed_cb,
            max_retries=3
        )

        vm.progress = 100 if success else 0
        vm.current_action = ""
        vm.speed_mbps = 0.0
        vm.last_backup = datetime.datetime.now()
        vm.last_status = "Success" if success else "Failed"

        log_msg = result_msg
        db.add(BackupLog(vm_name=vm.vm_name, status=vm.last_status, message=log_msg))
        
        if success:
            cleanup_old_backups(storage, vm.vm_name, vm.retention_count)
            send_event_notification(
                "backup_success",
                f"[NovaBak] Backup Succeeded: {vm.vm_name}",
                f"VM '{vm.vm_name}' backed up successfully at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\nDetails: {log_msg}"
            )
        else:
            log_error(f"[PID {pid}] {log_msg}")
            send_event_notification(
                "backup_failure",
                f"[NovaBak] Backup FAILED: {vm.vm_name}",
                f"Backup for VM '{vm.vm_name}' FAILED at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\nError: {log_msg}"
            )
            send_email_report(config, [BackupLog(vm_name=vm.vm_name, status="Failed", message=log_msg)])

        db.commit()

    except Exception as e:
        log_error(f"[PID {pid}] Error during backup of {vm.vm_name}: {e}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Failed", message=str(e)))
        vm.progress = 0
        vm.current_action = ""
        vm.last_status = "Failed"
        db.commit()
        send_event_notification(
            "backup_failure",
            f"[NovaBak] Backup FAILED: {vm.vm_name}",
            f"Backup for VM '{vm.vm_name}' encountered an unexpected error.\n\nError: {e}"
        )
        
    finally:
        try:
            vm.current_action = "Cleaning up..."
            db.commit()
            timeout_m = config.backup_timeout_mins if hasattr(config, 'backup_timeout_mins') else 15
            esxi_handler.remove_snapshot(si, vm.vm_name, timeout_mins=timeout_m)
            vm.progress = 0
            vm.current_action = ""
            db.commit()
        except Exception as e:
            log_error(f"[PID {pid}] Cleanup failed for {vm.vm_name}: {e}")

        # --- POWER ON (restore VM state if we shut it down) ---
        if powered_off_by_us:
            try:
                vm.current_action = "⚡ Powering on VM..."
                db.commit()
                log_info(f"[PID {pid}] Restoring power state — powering on {vm.vm_name}...")
                ok, msg = esxi_handler.poweron_vm(si, vm.vm_name, timeout_mins=3)
                if ok:
                    log_info(f"[PID {pid}] {vm.vm_name} powered on successfully after backup.")
                else:
                    log_error(f"[PID {pid}] Failed to power on {vm.vm_name} after backup: {msg}")
                    db.add(BackupLog(vm_name=vm.vm_name, status="Warning", message=f"Backup done but power-on failed: {msg}"))
                vm.current_action = ""
                db.commit()
            except Exception as e:
                log_error(f"[PID {pid}] Power-on step failed for {vm.vm_name}: {e}")
            
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
            queue_backup, 
            'cron', 
            hour=vm.schedule_hour, 
            minute=vm.schedule_minute, 
            args=[vm.id],
            id=job_id,
            misfire_grace_time=30 # Prevent firing if way past due
        )
        log_info(f"Scheduled job {job_id} for {vm.vm_name} at {vm.schedule_hour:02d}:{vm.schedule_minute:02d}")
    
    if not scheduler.running:
        scheduler.start()
    db.close()
    return scheduler

def get_available_backups(config):
    """ Scans the target storage and returns a list of available backups. """
    storage = storage_util.get_storage(config)
    if config.storage_type == "SMB":
        authenticate_smb(config)
    
    backups = []
    try:
        # List major VM directories
        vm_dirs = storage.list_dirs("")
        for vm_name in vm_dirs:
            # List date folders within each VM directory
            date_folders = storage.list_dirs(vm_name)
            for date_folder in date_folders:
                rel_date_dir = f"{vm_name}/{date_folder}"
                
                # Look for descriptor files
                files = storage.list_files(rel_date_dir)
                found_vmx = next((f for f in files if f.endswith('.vmx')), None)
                found_ovf = next((f for f in files if f.endswith('.ovf')), None)
                found_ova = next((f for f in files if f.endswith('.ova')), None)
                
                backup_file_rel = None
                if found_vmx: backup_file_rel = f"{rel_date_dir}/{found_vmx}"
                elif found_ovf: backup_file_rel = f"{rel_date_dir}/{found_ovf}"
                elif found_ova: backup_file_rel = f"{rel_date_dir}/{found_ova}"
                
                if backup_file_rel:
                    size_bytes = storage.get_size(rel_date_dir)
                    size_str = f"{size_bytes / (1024**3):.2f} GB" if size_bytes > 1024**3 else f"{size_bytes / (1024**2):.2f} MB"
                    
                    # Store either absolute path (Local) or S3 URI
                    full_path = ""
                    if hasattr(storage, '_full_path'):
                        full_path = storage._full_path(backup_file_rel)
                    else:
                        full_path = f"{storage.get_base_path()}{backup_file_rel}"

                    backups.append({
                        "vm_name": vm_name,
                        "date": date_folder,
                        "path": full_path,
                        "size": size_str
                    })
    except Exception as e:
        log_error(f"Error scanning storage repository: {e}")
        
    # Sort by date descending
    backups.sort(key=lambda x: x["date"], reverse=True)
    return backups

def perform_restore(config, target_ip, target_user, target_password, source_ova_path, target_name, datastore, restore_job_id):
    """ Restores a VM by uploading backup files to ESXi and registering them. """
    log_info(f"Starting Native Restore: {source_ova_path} -> {target_name} on {datastore} ({target_ip})")
    
    from models import SessionLocal
    def update_job(pct, action=None, status=None, error=None):
        with SessionLocal() as db:
            job = db.query(RestoreJob).filter(RestoreJob.id == restore_job_id).first()
            if job:
                if pct is not None: job.progress = pct
                if action: job.current_action = action
                if status: job.status = status
                if error: 
                    job.error_message = error
                    job.status = "Failed"
                if status in ["Success", "Failed"]:
                    job.end_time = datetime.datetime.utcnow()
                db.commit()

    log_info(f"[RESTORE] Connecting to target ESXi {target_ip}...")
    si = esxi_handler.connect_esxi(target_ip, target_user, target_password)
    if not si:
        log_warn(f"[RESTORE] Could not connect to target ESXi {target_ip}")
        update_job(0, error=f"Could not connect to target ESXi {target_ip}")
        return
    log_info(f"[RESTORE] Connected to ESXi successfully.")

    try:
        log_info(f"[RESTORE] Getting storage provider...")
        from storage_util import get_storage
        from models import Config
        with SessionLocal() as fresh_db:
            fresh_config = fresh_db.query(Config).first()
            storage_type = getattr(fresh_config, 'storage_type', 'SMB')
            log_info(f"[RESTORE] Storage type is {storage_type}. Initializing provider...")
            storage = get_storage(fresh_config)
        
        log_info(f"[RESTORE] Updating job status (Resolving paths)...")
        update_job(2, action="Resolving source paths...")
        
        log_info(f"[RESTORE] Normalizing source path: {source_ova_path}")
        s_path = source_ova_path.replace("\\", "/")
        b_path = storage.get_base_path().replace("\\", "/")
        
        log_info(f"[RESTORE] Base path: {b_path}")
        if s_path.startswith(b_path):
            rel_file_path = s_path[len(b_path):].strip("/\\")
            source_rel_dir = os.path.dirname(rel_file_path)
        else:
            source_rel_dir = os.path.dirname(source_ova_path).strip("/\\")

        source_rel_dir = source_rel_dir.replace("\\", "/").strip("/")

        def is_cancelled():
            with SessionLocal() as db:
                job = db.query(RestoreJob).filter(RestoreJob.id == restore_job_id).first()
                return job.is_cancelled if job else False

        log_info(f"[RESTORE] Path resolved: Rel Dir = '{source_rel_dir}'")

        log_info(f"[RESTORE] Calling backup_engine.import_vm_native...")
        success, msg = backup_engine.import_vm_native(
            si=si,
            storage=storage,
            source_rel_dir=source_rel_dir,
            target_ds=datastore,
            target_name=target_name,
            progress_callback=lambda p: update_job(p, action=f"Restoring files ({p}%)..."),
            is_cancelled_func=is_cancelled
        )

        if success:
            update_job(100, action="Completed", status="Success")
            log_info(f"Native Restore successful for {target_name}")
            send_event_notification(
                "restore_success",
                f"[NovaBak] Restore Succeeded: {target_name}",
                f"VM restore of '{target_name}' completed successfully at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}."
            )
        else:
            update_job(None, error=msg)
            log_error(f"Native Restore Failed: {msg}")
            send_event_notification(
                "restore_failure",
                f"[NovaBak] Restore FAILED: {target_name}",
                f"VM restore of '{target_name}' FAILED.\n\nError: {msg}"
            )

            
    except Exception as e:
        update_job(None, error=str(e))
        log_error(f"Restore Exception: {e}")
    finally:
        if si:
            esxi_handler.Disconnect(si)
