import os
import threading
import subprocess
import datetime
import smtplib
import collections
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
import esxi_handler
import backup_engine
import storage_util
from models import SessionLocal, Config, VM, BackupLog, RestoreJob, AuditLog
from security import SecretManager
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
    Sends a notification via email (to subscribed users) and Webhook (if configured).
    """
    from models import User  # avoid circular import at module level
    db = SessionLocal()
    try:
        config = db.query(Config).first()
        if not config:
            return
            
        # 1. Send Webhook
        if getattr(config, "webhook_url", None):
            try:
                import requests
                payload = {
                    "event": event_key,
                    "subject": subject,
                    "body": body,
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
                }
                requests.post(config.webhook_url, json=payload, timeout=5)
            except Exception as e:
                log_error(f"[WEBHOOK] Failed to send webhook: {e}")

        # 2. Send Emails
        if config.smtp_server:
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
        user_str = getattr(config, 'smb_user', '')
        if getattr(config, 'smb_domain', ''):
            user_str = f"{config.smb_domain}\\{config.smb_user}"
            
        if user_str:
            log_info(f"Authenticating to SMB share: {config.smb_unc_path} with user {user_str}")
            
        # Disconnect just in case there's a stale connection
        import subprocess
        subprocess.run(["net", "use", config.smb_unc_path, "/delete", "/y"], capture_output=True)
        
        # Connect
        cmd = ["net", "use", config.smb_unc_path]
        smb_pass = SecretManager.decrypt(getattr(config, 'smb_password', ''))
        if smb_pass and user_str:
            cmd.extend([smb_pass, f"/user:{user_str}"])
            
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
backup_queue_executor = ThreadPoolExecutor(max_workers=32)
restore_queue_executor = ThreadPoolExecutor(max_workers=5)
last_trigger_times = {}  # vm_id -> timestamp
_cancelled_backups = set()
_cancel_lock = threading.Lock()

WAIT_HOST = "Waiting for host slot..."
WAIT_GLOBAL = "Waiting for worker slot..."

_concurrency_lock = threading.Lock()
_global_active = 0
_host_active = collections.defaultdict(int)
_global_limit = 10
_host_limit = 2
_host_wait_queues = collections.defaultdict(collections.deque)
_global_wait_queue = collections.deque()
_slots_configured = False


class BackupCancelled(Exception):
    """Raised when a backup job is aborted by the user."""


class BackupSkipped(Exception):
    """Raised when preflight skips backup (e.g. insufficient datastore space)."""


def get_concurrency_limits(config):
    global_limit = max(1, min(32, getattr(config, "max_global_backups", None) or 10))
    host_limit = max(1, min(8, getattr(config, "max_backups_per_host", None) or 2))
    return global_limit, host_limit


def configure_concurrency(config):
    global _global_limit, _host_limit, _slots_configured
    with _concurrency_lock:
        _global_limit, _host_limit = get_concurrency_limits(config)
        _slots_configured = True
    log_info(f"[CONCURRENCY] Limits: global={_global_limit}, per_host={_host_limit}")


def _ensure_concurrency_configured():
    if not _slots_configured:
        db = SessionLocal()
        try:
            config = db.query(Config).first()
            configure_concurrency(config or Config())
        finally:
            db.close()


def _try_acquire_slots(host_id):
    global _global_active
    _ensure_concurrency_configured()
    with _concurrency_lock:
        if _global_active >= _global_limit:
            return False, "global"
        if _host_active[host_id] >= _host_limit:
            return False, "host"
        _global_active += 1
        _host_active[host_id] += 1
        return True, None


def _release_slots(host_id):
    with _concurrency_lock:
        global _global_active
        _global_active = max(0, _global_active - 1)
        _host_active[host_id] = max(0, _host_active[host_id] - 1)


def _enqueue_waiting(vm_id, host_id, reason):
    with _concurrency_lock:
        if reason == "host":
            if vm_id not in _host_wait_queues[host_id]:
                _host_wait_queues[host_id].append(vm_id)
        elif vm_id not in _global_wait_queue:
            _global_wait_queue.append(vm_id)


def _remove_from_wait_queues(vm_id):
    global _global_wait_queue
    with _concurrency_lock:
        for host_id in list(_host_wait_queues.keys()):
            _host_wait_queues[host_id] = collections.deque(
                x for x in _host_wait_queues[host_id] if x != vm_id
            )
        _global_wait_queue = collections.deque(x for x in _global_wait_queue if x != vm_id)


def _release_and_drain(host_id):
    _release_slots(host_id)
    to_dispatch = []
    with _concurrency_lock:
        if _host_wait_queues.get(host_id):
            to_dispatch.append(_host_wait_queues[host_id].popleft())
        elif _global_wait_queue:
            to_dispatch.append(_global_wait_queue.popleft())
    for vid in to_dispatch:
        queue_backup(vid)


def _is_queueable_state(action):
    if not action:
        return True
    return action in ("Queued...", WAIT_HOST, WAIT_GLOBAL)


def request_backup_stop(vm_id: int):
    with _cancel_lock:
        _cancelled_backups.add(vm_id)


def clear_backup_cancel(vm_id: int):
    with _cancel_lock:
        _cancelled_backups.discard(vm_id)


def is_backup_cancelled(vm_id: int) -> bool:
    with _cancel_lock:
        return vm_id in _cancelled_backups


def queue_backup(vm_id: int):
    """Queue a backup if concurrency slots are available; otherwise wait."""
    now = datetime.datetime.now().timestamp()
    pid = os.getpid()

    if vm_id in last_trigger_times:
        diff = now - last_trigger_times[vm_id]
        if diff < 65:
            log_debug(f"[PID {pid}] Skipping queue for VM {vm_id}: Cooldown active ({int(diff)}s < 65s)")
            return

    db = SessionLocal()
    vm = db.query(VM).filter(VM.id == vm_id).first()
    config = db.query(Config).first()
    if config and getattr(config, "scheduler_paused", False):
        log_info(f"[PID {pid}] Scheduler paused — skipping backup queue for VM ID {vm_id}")
        db.close()
        return
    if not vm or not vm.esxi_host:
        if vm is None:
            log_error(f"[PID {pid}] queue_backup called for non-existent VM ID: {vm_id}")
        db.close()
        return

    if not _is_queueable_state(vm.current_action):
        log_debug(f"[PID {pid}] Skipping queue for VM {vm.vm_name}: Already active (Status: {vm.current_action})")
        db.close()
        return

    host_id = vm.esxi_host_id
    acquired, reason = _try_acquire_slots(host_id)
    if not acquired:
        status = WAIT_HOST if reason == "host" else WAIT_GLOBAL
        log_info(f"[PID {pid}] VM {vm.vm_name} waiting: {status}")
        vm.current_action = status
        vm.progress = 0
        db.commit()
        _enqueue_waiting(vm_id, host_id, reason)
        db.close()
        return

    log_info(f"[PID {pid}] Queueing backup for VM: {vm.vm_name}")
    last_trigger_times[vm_id] = now
    vm.current_action = "Queued..."
    vm.progress = 0
    db.commit()
    backup_queue_executor.submit(perform_backup, vm_id)
    db.close()


def stop_job(vm_id: int):
    """Request termination of an active backup for a VM."""
    pid = os.getpid()
    log_info(f"[PID {pid}] Stop request received for VM ID: {vm_id}")
    request_backup_stop(vm_id)
    _remove_from_wait_queues(vm_id)
    db = SessionLocal()
    try:
        vm = db.query(VM).filter(VM.id == vm_id).first()
        if vm and vm.current_action in (WAIT_HOST, WAIT_GLOBAL):
            vm.current_action = ""
            vm.progress = 0
            db.commit()
    finally:
        db.close()
    if vm_id in active_processes:
        try:
            p = active_processes[vm_id]
            log_info(f"[PID {pid}] Terminating backup process for VM ID: {vm_id}")
            p.terminate()
            return True
        except Exception as e:
            log_error(f"[PID {pid}] Failed to terminate process for VM {vm_id}: {e}")
    return True

def perform_backup(vm_id: int):
    """ Backs up a specific VM using the native pyVmomi engine. Runs in parallel with other VMs. """
    from logger_util import job_id_var
    job_id_var.set(f"backup-{vm_id}")
    pid = os.getpid()
    db = SessionLocal()
    config = db.query(Config).first()
    vm = db.query(VM).filter(VM.id == vm_id).first()

    if not config or not vm or not vm.esxi_host:
        log_error(f"[PID {pid}] perform_backup aborted: Missing config/vm/host for ID {vm_id}")
        if vm and vm.esxi_host_id:
            _release_and_drain(vm.esxi_host_id)
        db.close()
        return

    host = vm.esxi_host
    host_id = host.id
    log_info(f"[PID {pid}] Starting parallel backup for {vm.vm_name} on host {host.name}")
    clear_backup_cancel(vm_id)

    storage = storage_util.get_storage(config)

    # SMB Authentication (only relevant for SMB storage type)
    if config.storage_type == "SMB":
        authenticate_smb(config)

    try:
        update_job(10, action="Connecting to ESXi...", status="Running")
        host_pass = SecretManager.decrypt(host.password)
        si = esxi_handler.connect_esxi(host.host_ip, host.username, host_pass)
        if not si:
            raise Exception(f"Failed to connect to ESXi host {host.host_ip}")
    except Exception as e:
        msg = f"Failed to connect to ESXi host {host.name}: {e}"
        log_error(f"[PID {pid}] {msg} for {vm.vm_name}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Failed", message=msg))
        vm.current_action = ""
        vm.progress = 0
        vm.last_status = "Failed"
        db.commit()
        db.close()
        _release_and_drain(host_id)
        return

    powered_off_by_us = False  # Track if we shut down the VM so we can restore it
    backup_start_time = datetime.datetime.now()
    dest_path_info = ""
    poweron_result = None  # None = not attempted, True = success, False = failed

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
        vm.current_action = "Preflight checks..."
        vm.progress = 0
        db.commit()

        ok, msg = backup_engine.preflight_check(si, vm.vm_name, timeout_mins=timeout_m, config=config)
        if not ok:
            if msg.startswith("[SKIP]"):
                raise BackupSkipped(msg[6:].strip())
            raise Exception(f"Preflight failed: {msg}")

        send_event_notification(
            "backup_start",
            f"[NovaBak] Backup Started: {vm.vm_name}",
            f"Backup job for VM '{vm.vm_name}' on host '{host.name}' has started at {backup_start_time.strftime('%Y-%m-%d %H:%M')}."
            + (f"\nVM was powered off before backup." if powered_off_by_us else "")
        )

        # --- BACKUP ---
        vm.current_action = "Backing up VM..."
        vm.progress = 0
        db.commit()

        def progress_cb(pct):
            if is_backup_cancelled(vm_id):
                raise BackupCancelled("Backup cancelled by user")
            try:
                vm.progress = pct
                db.commit()
            except Exception:
                pass

        def speed_cb(mbps):
            if is_backup_cancelled(vm_id):
                raise BackupCancelled("Backup cancelled by user")
            try:
                vm.speed_mbps = mbps
                vm.current_action = f"Backing up... {mbps:.1f} MB/s"
                db.commit()
            except Exception:
                pass

        def cancel_check():
            return is_backup_cancelled(vm_id)

        success, result_msg, checksum_json = backup_engine.export_vm_native(
            si=si,
            vm_name=vm.vm_name,
            storage=storage,
            dest_rel_dir=dest_rel_dir,
            progress_callback=progress_cb,
            speed_callback=speed_cb,
            is_cancelled_func=cancel_check,
            max_retries=3
        )

        if not success and "cancelled" in (result_msg or "").lower():
            raise BackupCancelled(result_msg)

        vm.progress = 100 if success else 0
        vm.current_action = ""
        vm.speed_mbps = 0.0
        vm.last_backup = datetime.datetime.now()
        vm.last_status = "Success" if success else "Failed"
        backup_end_time = datetime.datetime.now()
        duration_s = int((backup_end_time - backup_start_time).total_seconds())
        duration_str = f"{duration_s // 60}m {duration_s % 60}s"
        # Build destination path string for email
        try:
            dest_path_info = storage.get_base_path().rstrip('/\\') + '/' + dest_rel_dir
        except Exception:
            dest_path_info = dest_rel_dir

        log_msg = result_msg
        db.add(BackupLog(vm_name=vm.vm_name, status=vm.last_status, message=log_msg, checksum=checksum_json if success else None))
        
        if success:
            cleanup_old_backups(storage, vm.vm_name, vm.retention_count)
            rich_body = (
                f"Backup Report — {vm.vm_name}\n"
                f"{'=' * 50}\n"
                f"Status     : SUCCESS ✓\n"
                f"VM         : {vm.vm_name}\n"
                f"Host       : {host.name}\n"
                f"Started    : {backup_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Finished   : {backup_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Duration   : {duration_str}\n"
                f"Destination: {dest_path_info}\n"
                f"Power State: {'VM was powered OFF for backup and powered ON after.' if powered_off_by_us else 'VM remained powered on (live backup).'}\n"
                f"{'=' * 50}\n"
                f"Details    : {log_msg}\n"
            )
            send_event_notification("backup_success", f"[NovaBak] ✓ Backup Succeeded: {vm.vm_name}", rich_body)
        else:
            log_error(f"[PID {pid}] {log_msg}")
            rich_body = (
                f"Backup Report — {vm.vm_name}\n"
                f"{'=' * 50}\n"
                f"Status     : FAILED ✗\n"
                f"VM         : {vm.vm_name}\n"
                f"Host       : {host.name}\n"
                f"Started    : {backup_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Failed At  : {backup_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Duration   : {duration_str}\n"
                f"Destination: {dest_path_info or 'N/A'}\n"
                f"Power State: {'VM was powered off, may need manual power-on check.' if powered_off_by_us else 'VM remained powered on.'}\n"
                f"{'=' * 50}\n"
                f"Error      : {log_msg}\n"
            )
            send_event_notification("backup_failure", f"[NovaBak] ✗ Backup FAILED: {vm.vm_name}", rich_body)
            send_email_report(config, [BackupLog(vm_name=vm.vm_name, status="Failed", message=log_msg)])

        db.commit()

    except BackupCancelled:
        log_info(f"[PID {pid}] Backup cancelled for {vm.vm_name}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Cancelled", message="Backup cancelled by user"))
        vm.progress = 0
        vm.current_action = ""
        vm.speed_mbps = 0.0
        vm.last_status = "Cancelled"
        db.commit()
    except BackupSkipped as e:
        log_info(f"[PID {pid}] Backup skipped for {vm.vm_name}: {e}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Skipped", message=str(e)))
        vm.progress = 0
        vm.current_action = ""
        vm.speed_mbps = 0.0
        vm.last_status = "Skipped"
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
                    poweron_result = True
                else:
                    log_error(f"[PID {pid}] Failed to power on {vm.vm_name} after backup: {msg}")
                    db.add(BackupLog(vm_name=vm.vm_name, status="Warning", message=f"Backup done but power-on failed: {msg}"))
                    poweron_result = False
                vm.current_action = ""
                db.commit()
            except Exception as e:
                log_error(f"[PID {pid}] Power-on step failed for {vm.vm_name}: {e}")
        
        clear_backup_cancel(vm_id)
        esxi_handler.Disconnect(si)
        db.close()
        _release_and_drain(host_id)




# Shared scheduler instance
scheduler = BackgroundScheduler()

def generate_daily_report():
    """ Generates a summary of backups in the last 24 hours and sends it via email and webhook. """
    db = SessionLocal()
    try:
        config = db.query(Config).first()
        if not config:
            return

        now = datetime.datetime.utcnow()
        yesterday = now - datetime.timedelta(days=1)
        logs = db.query(BackupLog).filter(BackupLog.timestamp >= yesterday).order_by(BackupLog.timestamp.desc()).all()
        
        if not logs:
            log_info("Daily Report: No backups ran in the last 24 hours.")
            return
            
        success_count = sum(1 for log in logs if log.status == "Success")
        failed_count = sum(1 for log in logs if log.status == "Failed")
        
        subject = f"[NovaBak] Daily Backup Report - {success_count} Success, {failed_count} Failed"
        
        # Plain text / Markdown body for Webhooks
        body = f"NovaBak Daily Report ({yesterday.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')})\n\n"
        body += f"Total Jobs: {len(logs)}\nSuccess: {success_count}\nFailed: {failed_count}\n\n"
        for log in logs:
            icon = "✅" if log.status == "Success" else "❌" if log.status == "Failed" else "⚠️"
            body += f"{icon} {log.vm_name} - {log.status} ({log.timestamp.strftime('%H:%M')})\n"
            if log.status == "Failed":
                body += f"   Reason: {log.message}\n"
                
        # Dispatch generic event which sends webhook + email
        send_event_notification("daily_report", subject, body)
        
    except Exception as e:
        log_error(f"Failed to generate daily report: {e}")
    finally:
        db.close()

def start_scheduler():
    """ Initialize APScheduler and load all active jobs from DB. """
    db = SessionLocal()
    config = db.query(Config).first()

    # Clear existing jobs to avoid duplicates on restart/config change
    for job in scheduler.get_jobs():
        job.remove()

    if config and getattr(config, "scheduler_paused", False):
        log_info("Scheduler paused — no cron jobs registered")
        if not scheduler.running:
            scheduler.start()
        configure_concurrency(config or Config())
        db.close()
        return scheduler

    vms = db.query(VM).filter(VM.is_selected == True, VM.is_job_active == True).all()

    for vm in vms:
        job_id = f"backup_{vm.id}"
        freq = getattr(vm, 'schedule_frequency', 'daily') or 'daily'
        days = getattr(vm, 'schedule_days', '0,1,2,3,4,5,6') or '0,1,2,3,4,5,6'

        # Build cron kwargs based on frequency
        cron_kwargs = dict(hour=vm.schedule_hour, minute=vm.schedule_minute)
        if freq == 'daily':
            pass  # No day restriction — fires every day of the week
        elif freq == 'weekly':
            cron_kwargs['day_of_week'] = days  # Run on selected weekdays
        elif freq == 'monthly':
            # First occurrence of selected weekday(s) in each month
            # day='1-7' means days 1-7 of the month; combined with day_of_week gives "first X of month"
            cron_kwargs['day'] = '1-7'
            cron_kwargs['day_of_week'] = days

        scheduler.add_job(
            queue_backup,
            'cron',
            **cron_kwargs,
            args=[vm.id],
            id=job_id,
            misfire_grace_time=60
        )
        day_names = ['Mo','Tu','We','Th','Fr','Sa','Su']
        day_label = ','.join(day_names[int(d)] for d in days.split(',') if d.strip().isdigit()) if freq != 'monthly' else '1st'
        log_info(f"Scheduled job {job_id} for {vm.vm_name} at {vm.schedule_hour:02d}:{vm.schedule_minute:02d} ({freq}: {day_label})")
    
    # Schedule Daily Report Job
    if getattr(config, "webhook_url", None) or getattr(config, "smtp_server", None):
        report_time = getattr(config, "daily_report_time", "08:00")
        try:
            hour, minute = map(int, report_time.split(':'))
            scheduler.add_job(
                generate_daily_report,
                'cron',
                hour=hour,
                minute=minute,
                id='daily_report',
                replace_existing=True
            )
            log_info(f"Scheduled Daily Report for {hour:02d}:{minute:02d}")
        except Exception as e:
            log_error(f"Failed to schedule daily report: {e}")

    if not scheduler.running:
        scheduler.start()
    configure_concurrency(config or Config())
    db.close()
    return scheduler


def get_available_backups(target):
    """ Scans the target storage and returns a list of available backups. """
    from storage_util import get_storage
    
    backups = []
    if not target:
        return backups
        
    storage = get_storage(target)
    if target.storage_type == "SMB":
        authenticate_smb(target)
        
    try:
        vm_dirs = storage.list_dirs("")
        for vm_name in vm_dirs:
            date_folders = storage.list_dirs(vm_name)
            for date_folder in date_folders:
                rel_date_dir = f"{vm_name}/{date_folder}"
                files = storage.list_files(rel_date_dir)
                
                has_descriptor = any(f.endswith(".ovf") or f.endswith(".vmx") for f in files)
                if has_descriptor:
                    # Find main disk size
                    total_size = sum(storage.get_file_size(f"{rel_date_dir}/{f}") for f in files)
                    backups.append({
                        "vm_name": vm_name,
                        "date": date_folder,
                        "path": rel_date_dir,
                        "size": total_size,
                        "target_id": target.id,
                        "target_name": target.name
                    })
    except Exception as e:
        log_error(f"Failed to scan storage target {target.name}: {e}")
        
    return backups


def perform_restore(config, target_ip, target_user, target_password, source_ova_path, target_name, datastore, restore_job_id, is_test_restore=False):
    """ Restores a VM by uploading backup files to ESXi and registering them. """
    from logger_util import job_id_var
    job_id_var.set(f"restore-{restore_job_id}")
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
    target_password = SecretManager.decrypt(target_password)
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
            with SessionLocal() as db:
                job = db.query(RestoreJob).filter(RestoreJob.id == restore_job_id).first()
                is_test = job.is_test_restore if job else False

            if is_test:
                update_job(95, action="Running Test Restore Verification...")
                log_info(f"[TEST RESTORE] Disconnecting vNICs for {target_name}...")
                esxi_handler.disconnect_vnics(si, target_name)
                
                log_info(f"[TEST RESTORE] Powering on {target_name}...")
                esxi_handler.poweron_vm(si, target_name)
                
                log_info(f"[TEST RESTORE] Waiting 60s for boot...")
                time.sleep(60)
                
                log_info(f"[TEST RESTORE] Destroying test VM {target_name}...")
                esxi_handler.unregister_and_delete_vm(si, target_name)
                
                update_job(100, action="Completed (Test Restored & Verified)", status="Success")
                log_info(f"Test Restore successful for {target_name}")
                send_event_notification(
                    "restore_success",
                    f"[NovaBak] Test Restore Verified: {target_name}",
                    f"Test restore verification of '{target_name}' completed successfully (booted and deleted) at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}."
                )
            else:
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
