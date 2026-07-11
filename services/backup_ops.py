"""Shared backup configuration and job operations for UI and API."""

import datetime
import os
import shutil
import time

import esxi_handler
import worker
import storage_util
from config_env import DATA_DIR
from models import Config, VM, ESXiHost, BackupLog, RestoreJob

_overview_storage_cache = {"ts": 0, "data": None}
_OVERVIEW_STORAGE_TTL = 300


def get_or_create_config(db):
    config = db.query(Config).first()
    if not config:
        config = Config()
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def config_to_dict(config):
    return {
        "storage_type": config.storage_type,
        "nfs_path": config.nfs_path,
        "smb_unc_path": config.smb_unc_path,
        "smb_user": config.smb_user,
        "s3_endpoint": config.s3_endpoint,
        "s3_bucket": config.s3_bucket,
        "s3_region": config.s3_region,
        "perf_parallel_threads": config.perf_parallel_threads,
        "perf_compression_level": config.perf_compression_level,
        "backup_timeout_mins": config.backup_timeout_mins,
        "max_global_backups": config.max_global_backups,
        "max_backups_per_host": config.max_backups_per_host,
        "datastore_min_free_pct": config.datastore_min_free_pct,
        "datastore_headroom_gb": config.datastore_headroom_gb,
        "datastore_est_multiplier": config.datastore_est_multiplier,
        "smtp_server": config.smtp_server,
        "smtp_port": config.smtp_port,
        "smtp_user": config.smtp_user,
        "smtp_to_email": config.smtp_to_email,
        "smtp_use_tls": config.smtp_use_tls,
        "smtp_use_ssl": config.smtp_use_ssl,
        "imap_server": config.imap_server,
        "imap_port": config.imap_port,
        "imap_user": config.imap_user,
        "imap_use_ssl": config.imap_use_ssl,
        "webhook_url": config.webhook_url,
        "daily_report_time": config.daily_report_time,
    }


def list_storage_targets(db):
    from models import StorageTarget
    return db.query(StorageTarget).all()

def create_storage_target(db, data):
    from models import StorageTarget
    target = StorageTarget(**data)
    db.add(target)
    db.commit()
    db.refresh(target)
    return target

def update_storage_target(db, target_id, data):
    from models import StorageTarget
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise ValueError("Storage target not found")
    for key, value in data.items():
        if value is not None:
            setattr(target, key, value)
    db.commit()
    db.refresh(target)
    return target

def delete_storage_target(db, target_id):
    from models import StorageTarget
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise ValueError("Storage target not found")
    db.delete(target)
    db.commit()
    return True

def get_default_storage_target(db):
    from models import StorageTarget
    target = db.query(StorageTarget).filter(StorageTarget.is_default == True).first()
    if not target:
        target = db.query(StorageTarget).first()
    return target

def update_full_config(db, data):
    config = get_or_create_config(db)
    storage_fields = {
        "perf_parallel_threads", "perf_compression_level",
        "backup_timeout_mins", "encryption_key"
    }
    email_fields = {
        "smtp_server", "smtp_port", "smtp_user", "smtp_to_email", "smtp_use_tls",
        "smtp_use_ssl", "imap_server", "imap_port", "imap_user", "imap_use_ssl",
    }
    secret_fields = {"smtp_password", "imap_password", "encryption_key"}

    for key, value in data.items():
        if value is None:
            continue
        if key in secret_fields:
            if value:
                setattr(config, key, value)
        elif key in storage_fields or key in email_fields or hasattr(config, key):
            setattr(config, key, value)
    db.commit()
    db.refresh(config)
    return config


def test_smtp(db):
    config = db.query(Config).first()
    if not config or not config.smtp_server:
        return False, "SMTP is not configured."
    try:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText("NovaBak SMTP test message.")
        msg["Subject"] = "NovaBak SMTP Test"
        msg["From"] = config.smtp_user or config.smtp_to_email
        msg["To"] = config.smtp_to_email

        if config.smtp_use_ssl:
            server = smtplib.SMTP_SSL(config.smtp_server, config.smtp_port)
        else:
            server = smtplib.SMTP(config.smtp_server, config.smtp_port)
            if config.smtp_use_tls:
                server.starttls()
        if config.smtp_user and config.smtp_password:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg)
        server.quit()
        return True, f"Test email sent to {config.smtp_to_email}"
    except Exception as e:
        return False, str(e)


def test_storage(db):
    config = db.query(Config).first()
    if not config:
        return False, "No configuration found."
    try:
        storage = storage_util.get_storage(config)
        if config.storage_type == "SMB":
            success, msg = worker.authenticate_smb(config)
            if not success:
                return False, msg
        storage.list_dirs("")
        return True, f"Successfully connected to {config.storage_type} storage."
    except Exception as e:
        return False, f"Connection failed: {str(e)}"


def host_to_dict(host, include_secrets=False):
    data = {
        "id": host.id,
        "name": host.name,
        "host_ip": host.host_ip,
        "username": host.username,
        "host_type": getattr(host, "host_type", "esxi"),
    }
    if include_secrets:
        data["password"] = host.password
    return data


def add_esxi_host(db, name, host_ip, username, password, host_type="esxi"):
    from security import SecretManager
    existing = db.query(ESXiHost).filter(ESXiHost.name == name).first()
    if existing:
        raise ValueError(f"Host '{name}' already exists")
    encrypted_password = SecretManager.encrypt(password)
    host = ESXiHost(name=name, host_ip=host_ip, username=username, password=encrypted_password, host_type=host_type)
    db.add(host)
    db.commit()
    db.refresh(host)
    return host


def delete_esxi_host(db, host_id):
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if not host:
        return False
    db.delete(host)
    db.commit()
    return True


def sync_vms_for_host(db, host_id):
    from security import SecretManager
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if not host:
        raise ValueError("Invalid ESXi host")
    host_ip = host.host_ip
    username = host.username
    password = SecretManager.decrypt(host.password)
    host_id_val = host.id
    db.commit() # Release SQLite lock before long network call
    
    si = esxi_handler.connect_esxi(host_ip, username, password)
    if not si:
        raise ConnectionError("Could not connect to ESXi.")
    vm_list = esxi_handler.get_all_vms(si)
    esxi_handler.Disconnect(si)

    existing_vms = {vm.vm_name: vm for vm in db.query(VM).all()}
    synced = []
    for vm_data in vm_list:
        if vm_data["name"] not in existing_vms:
            new_vm = VM(
                vm_name=vm_data["name"],
                esxi_host_id=host_id_val,
                cpu_count=vm_data.get("cpu_count", 0),
                memory_mb=vm_data.get("memory_mb", 0),
                storage_gb=vm_data.get("storage_gb", 0.0),
                power_state=vm_data.get("power_state", "Unknown"),
            )
            db.add(new_vm)
            synced.append(vm_data["name"])
        else:
            vm = existing_vms[vm_data["name"]]
            vm.cpu_count = vm_data.get("cpu_count", 0)
            vm.memory_mb = vm_data.get("memory_mb", 0)
            vm.storage_gb = vm_data.get("storage_gb", 0.0)
            vm.power_state = vm_data.get("power_state", "Unknown")
            if vm.esxi_host_id != host_id_val:
                vm.esxi_host_id = host_id_val
    db.commit()
    return {"synced_new": synced, "total_on_host": len(vm_list)}


def vm_to_dict(vm):
    return {
        "id": vm.id,
        "vm_name": vm.vm_name,
        "esxi_host_id": vm.esxi_host_id,
        "storage_target_id": vm.storage_target_id,
        "is_selected": vm.is_selected,
        "cpu_count": vm.cpu_count,
        "memory_mb": vm.memory_mb,
        "storage_gb": vm.storage_gb,
        "schedule_hour": vm.schedule_hour,
        "schedule_minute": vm.schedule_minute,
        "retention_count": vm.retention_count,
        "is_job_active": vm.is_job_active,
        "schedule_frequency": vm.schedule_frequency,
        "schedule_days": vm.schedule_days,
        "last_backup": vm.last_backup.isoformat() if vm.last_backup else None,
        "last_status": vm.last_status,
        "progress": vm.progress,
        "current_action": vm.current_action,
        "power_state": vm.power_state,
        "power_off_for_backup": vm.power_off_for_backup,
    }


def update_vm_job(db, vm_id, data):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise ValueError("VM not found")
    for field in (
        "is_selected", "schedule_hour", "schedule_minute", "retention_count",
        "is_job_active", "power_off_for_backup", "schedule_frequency", "storage_target_id"
    ):
        if field in data and data[field] is not None:
            setattr(vm, field, data[field])
    if "schedule_days" in data and data["schedule_days"] is not None:
        valid_days = [d.strip() for d in data["schedule_days"].split(",") if d.strip().isdigit() and 0 <= int(d.strip()) <= 6]
        vm.schedule_days = ",".join(valid_days) if valid_days else "0,1,2,3,4,5,6"
    if vm.schedule_frequency not in ("daily", "weekly", "monthly"):
        vm.schedule_frequency = "daily"
    db.commit()
    db.refresh(vm)
    return vm


def is_scheduler_paused(db):
    config = get_or_create_config(db)
    return bool(getattr(config, "scheduler_paused", False))


def set_scheduler_paused(db, paused):
    config = get_or_create_config(db)
    config.scheduler_paused = bool(paused)
    db.commit()
    db.refresh(config)
    return config


def trigger_backup(db, vm_id, current_username="system", ip_address=None):
    if is_scheduler_paused(db):
        raise ValueError("All backups are paused. Click Resume all on the Tasks page to run backups.")
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise ValueError("VM not found")
    vm.current_action = "PENDING_RUN"
    db.commit()
    from logger_util import log_audit
    log_audit(db, current_username, "trigger_backup", f"Triggered backup for VM {vm.vm_name}", ip_address)
    return vm


def stop_backup(db, vm_id, current_username="system", ip_address=None):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise ValueError("VM not found")
    vm.current_action = "PENDING_STOP"
    db.commit()
    from logger_util import log_audit
    log_audit(db, current_username, "stop_backup", f"Stopped backup for VM {vm.vm_name}", ip_address)
    return vm


def stop_all_backups(db):
    vms = db.query(VM).all()
    stopped = []
    for vm in vms:
        action = (vm.current_action or "").strip()
        if action and action not in ("Idle",):
            vm.current_action = "PENDING_STOP"
            stopped.append({"id": vm.id, "vm_name": vm.vm_name})
    db.commit()
    return stopped


def get_datastores(db, host_id):
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if not host:
        raise ValueError("Invalid host")
    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        raise ConnectionError("Could not connect to ESXi host")
    datastores = esxi_handler.get_datastores(si)
    esxi_handler.Disconnect(si)
    return datastores


def list_backups_grouped(db):
    config = db.query(Config).first()
    if not config:
        raise ValueError("No configuration found")
    backups = worker.get_available_backups(config)
    grouped = {}
    for b in backups:
        grouped.setdefault(b["vm_name"], []).append(
            {"date": b["date"], "path": b["path"], "size": b["size"]}
        )
    return [{"vm_name": vm, "versions": versions} for vm, versions in sorted(grouped.items())]


def job_progress(db):
    vms = db.query(VM).all()
    return {
        vm.id: {
            "progress": vm.progress or 0,
            "current_action": vm.current_action or "",
            "speed_mbps": round(getattr(vm, "speed_mbps", 0) or 0, 1),
        }
        for vm in vms
    }


def list_backup_logs(db, limit=100):
    logs = db.query(BackupLog).order_by(BackupLog.timestamp.desc()).limit(limit).all()
    return [
        {
            "id": log.id,
            "vm_name": log.vm_name,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "status": log.status,
            "message": log.message,
            "checksum": getattr(log, 'checksum', None),
        }
        for log in logs
    ]


def tail_log_file(filename, lines=100, search_str=""):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        return f"[{filename} not found or empty]"
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines_list = f.readlines()
            if search_str:
                needle = search_str.lower()
                lines_list = [line for line in lines_list if needle in line.lower()]
            return "".join(lines_list[-lines:])
    except Exception as e:
        return f"Error reading {filename}: {e}"


def get_system_logs(service_lines=100, service_search="", worker_lines=100, worker_search=""):
    return {
        "service_log": tail_log_file("service.log", service_lines, service_search),
        "worker_log": tail_log_file("worker.log", worker_lines, worker_search),
    }


def restore_to_dict(job):
    return {
        "id": job.id,
        "target_name": job.target_name,
        "target_esxi_host": job.target_esxi_host,
        "datastore": job.datastore,
        "source_path": job.source_path,
        "status": job.status,
        "progress": job.progress,
        "current_action": job.current_action,
        "is_cancelled": job.is_cancelled,
        "start_time": job.start_time.isoformat() if job.start_time else None,
        "end_time": job.end_time.isoformat() if job.end_time else None,
        "error_message": job.error_message,
    }


def list_restores(db, limit=50):
    jobs = db.query(RestoreJob).order_by(RestoreJob.start_time.desc()).limit(limit).all()
    return [restore_to_dict(job) for job in jobs]


def start_restore(db, target_esxi_id, source_ova, target_name, datastore, current_username="system", ip_address=None):
    config = db.query(Config).first()
    target_host = db.query(ESXiHost).filter(ESXiHost.id == target_esxi_id).first()
    if not config or not target_host:
        raise ValueError("Invalid configuration or ESXi host")
    if config.storage_type == "SMB":
        worker.authenticate_smb(config)
    restore_job = RestoreJob(
        target_name=target_name,
        target_esxi_host=target_host.name,
        datastore=datastore,
        source_path=source_ova,
        status="In Progress",
        progress=0,
        current_action="Initializing...",
    )
    db.add(restore_job)
    db.commit()
    db.refresh(restore_job)
    from logger_util import log_audit
    log_audit(db, current_username, "start_restore", f"Started restore of {source_ova} to {target_name} on {target_host.name}", ip_address)
    worker.restore_queue_executor.submit(
        worker.perform_restore,
        config,
        target_host.host_ip,
        target_host.username,
        target_host.password,
        source_ova,
        target_name,
        datastore,
        restore_job.id,
    )
    return restore_job


def stop_restore(db, job_id, current_username="system", ip_address=None):
    job = db.query(RestoreJob).filter(RestoreJob.id == job_id).first()
    if not job:
        raise ValueError("Restore job not found")
    if job.status != "In Progress":
        raise ValueError("Job not in progress")
    job.is_cancelled = True
    job.current_action = "Stopping..."
    db.commit()
    from logger_util import log_audit
    log_audit(db, current_username, "stop_restore", f"Stopped restore job {job_id}", ip_address)
    return job


def delete_restore(db, job_id, current_username="system", ip_address=None):
    job = db.query(RestoreJob).filter(RestoreJob.id == job_id).first()
    if not job:
        return False
    db.delete(job)
    db.commit()
    from logger_util import log_audit
    log_audit(db, current_username, "delete_restore", f"Deleted restore job {job_id}", ip_address)
    return True


def _parse_backup_size_bytes(size_str):
    try:
        parts = (size_str or "").split()
        if len(parts) != 2:
            return 0
        val = float(parts[0])
        unit = parts[1].upper()
        if unit == "GB":
            return int(val * (1024 ** 3))
        if unit == "MB":
            return int(val * (1024 ** 2))
        if unit == "TB":
            return int(val * (1024 ** 4))
        return int(val)
    except (TypeError, ValueError):
        return 0


def _format_bytes(num_bytes):
    if num_bytes is None or num_bytes <= 0:
        return "—"
    if num_bytes >= 1024 ** 4:
        return f"{num_bytes / (1024 ** 4):.2f} TB"
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / (1024 ** 3):.2f} GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def _vm_is_running(vm):
    action = (vm.current_action or "").strip()
    if action in ("PENDING_RUN", "PENDING_STOP"):
        return True
    if action:
        return True
    progress = vm.progress or 0
    return 0 < progress < 100


def _storage_path_label(target):
    if not target:
        return "SMB", ""
    stype = target.storage_type or "SMB"
    if stype == "S3":
        bucket = target.s3_bucket or "—"
        region = target.s3_region or ""
        path = f"s3://{bucket}" + (f" ({region})" if region else "")
    elif stype == "NFS":
        path = target.nfs_path or "—"
    else:
        path = target.smb_unc_path or "—"
    return stype, path


def _disk_usage_for_path(path):
    if not path or path == "—":
        return None
    try:
        if os.path.exists(path):
            usage = shutil.disk_usage(path)
            total = usage.total
            free = usage.free
            used = usage.used
            return {
                "disk_total_gb": round(total / (1024 ** 3), 1),
                "disk_used_gb": round(used / (1024 ** 3), 1),
                "disk_free_gb": round(free / (1024 ** 3), 1),
                "disk_free_pct": round(100 * free / total, 1) if total else None,
            }
    except OSError:
        pass
    return None


def _cached_storage_scan(target):
    global _overview_storage_cache
    now = time.time()
    if _overview_storage_cache["data"] and (now - _overview_storage_cache["ts"]) < _OVERVIEW_STORAGE_TTL:
        return _overview_storage_cache["data"]

    stype, path = _storage_path_label(target)
    result = {
        "type": stype,
        "path": path,
        "total_bytes": 0,
        "total_human": "—",
        "version_count": 0,
        "vm_count": 0,
        "scan_error": None,
    }
    disk = _disk_usage_for_path(path) if stype != "S3" else None
    if disk:
        result.update(disk)

    try:
        if target and target.storage_type == "SMB":
            worker.authenticate_smb(target)
        backups = worker.get_available_backups(target)
        total_bytes = sum(_parse_backup_size_bytes(b.get("size")) for b in backups)
        vm_names = {b["vm_name"] for b in backups}
        result["total_bytes"] = total_bytes
        result["total_human"] = _format_bytes(total_bytes)
        result["version_count"] = len(backups)
        result["vm_count"] = len(vm_names)
    except Exception as e:
        result["scan_error"] = str(e)

    _overview_storage_cache = {"ts": now, "data": result}
    return result


def _worker_health():
    heartbeat = os.path.join(DATA_DIR, "worker.heartbeat")
    if os.path.exists(heartbeat):
        age = int(time.time() - os.path.getmtime(heartbeat))
        # Worker polls every 5s; allow generous margin for slow disks / load
        return age < 45, age

    worker_log = os.path.join(DATA_DIR, "worker.log")
    if os.path.exists(worker_log):
        age = int(time.time() - os.path.getmtime(worker_log))
        return age < 120, age
    return False, None


def get_overview(db):
    config = get_or_create_config(db)
    vms = db.query(VM).all()
    esxi_hosts = db.query(ESXiHost).all()
    selected = [v for v in vms if v.is_selected]

    status_counts = {"Success": 0, "Failed": 0, "Cancelled": 0, "Skipped": 0, "Never": 0, "Other": 0}
    for vm in selected:
        st = vm.last_status or "Never"
        if st in status_counts:
            status_counts[st] += 1
        else:
            status_counts["Other"] += 1

    cutoff_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    logs_7d = db.query(BackupLog).filter(BackupLog.timestamp >= cutoff_7d).all()
    log_stats_7d = {"Success": 0, "Failed": 0, "Cancelled": 0, "Skipped": 0, "Warning": 0, "Other": 0}
    for log in logs_7d:
        st = log.status or "Other"
        if st in log_stats_7d:
            log_stats_7d[st] += 1
        else:
            log_stats_7d["Other"] += 1

    finished_7d = log_stats_7d["Success"] + log_stats_7d["Failed"] + log_stats_7d["Cancelled"] + log_stats_7d["Skipped"]
    success_rate_7d = round(100 * log_stats_7d["Success"] / finished_7d, 1) if finished_7d else None

    stale_cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=48)
    attention = []
    for vm in selected:
        host_name = vm.esxi_host.name if vm.esxi_host else "—"
        last_backup_iso = vm.last_backup.isoformat() if vm.last_backup else None
        if vm.last_status == "Failed":
            attention.append({
                "vm_id": vm.id,
                "vm_name": vm.vm_name,
                "host_name": host_name,
                "reason": "Last backup failed",
                "severity": "error",
                "last_status": vm.last_status,
                "last_backup": last_backup_iso,
            })
        elif vm.is_job_active and (not vm.last_backup or vm.last_backup < stale_cutoff):
            attention.append({
                "vm_id": vm.id,
                "vm_name": vm.vm_name,
                "host_name": host_name,
                "reason": "No recent backup (48h+)",
                "severity": "warning",
                "last_status": vm.last_status or "Never",
                "last_backup": last_backup_iso,
            })
        elif vm.last_status == "Never":
            attention.append({
                "vm_id": vm.id,
                "vm_name": vm.vm_name,
                "host_name": host_name,
                "reason": "Never backed up",
                "severity": "info",
                "last_status": "Never",
                "last_backup": None,
            })

    severity_order = {"error": 0, "warning": 1, "info": 2}
    attention.sort(key=lambda x: (severity_order.get(x["severity"], 9), x["vm_name"]))

    live_jobs = []
    for vm in selected:
        if not _vm_is_running(vm):
            continue
        live_jobs.append({
            "vm_id": vm.id,
            "vm_name": vm.vm_name,
            "host_name": vm.esxi_host.name if vm.esxi_host else "—",
            "progress": vm.progress or 0,
            "current_action": vm.current_action or "",
            "speed_mbps": round(getattr(vm, "speed_mbps", 0) or 0, 1),
        })

    recent_activity = list_backup_logs(db, limit=15)
    restores = list_restores(db, limit=10)
    active_restores = [r for r in restores if r.get("status") == "In Progress"]

    worker_online, worker_age = _worker_health()
    default_target = get_default_storage_target(db)
    storage = _cached_storage_scan(default_target)
    setup_incomplete = len(esxi_hosts) == 0 or len(selected) == 0

    return {
        "protected_count": len(selected),
        "scheduled_count": sum(1 for v in selected if v.is_job_active),
        "running_count": len(live_jobs),
        "host_count": len(esxi_hosts),
        "inventory_count": len(vms),
        "status_counts": status_counts,
        "log_stats_7d": log_stats_7d,
        "success_rate_7d": success_rate_7d,
        "storage": storage,
        "worker_online": worker_online,
        "worker_last_seen_seconds": worker_age,
        "max_global_backups": config.max_global_backups or 10,
        "live_jobs": live_jobs,
        "recent_activity": recent_activity,
        "active_restores": active_restores[:5],
        "attention": attention[:12],
        "setup_incomplete": setup_incomplete,
    }
