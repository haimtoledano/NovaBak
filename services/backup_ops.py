"""Shared backup configuration and job operations for UI and API."""

import os

import esxi_handler
import worker
import storage_util
from config_env import DATA_DIR
from models import Config, VM, ESXiHost, BackupLog, RestoreJob


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
        "max_global_backups": getattr(config, "max_global_backups", None) or 10,
        "max_backups_per_host": getattr(config, "max_backups_per_host", None) or 2,
        "datastore_min_free_pct": getattr(config, "datastore_min_free_pct", None) or 15,
        "datastore_headroom_gb": getattr(config, "datastore_headroom_gb", None) or 10,
        "datastore_est_multiplier": getattr(config, "datastore_est_multiplier", None) or 2.0,
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
    }


def update_storage_config(db, data):
    config = get_or_create_config(db)
    if "storage_type" in data and data["storage_type"] is not None:
        config.storage_type = data["storage_type"]
    if "nfs_path" in data and data["nfs_path"] is not None:
        config.nfs_path = data["nfs_path"]
    if "smb_unc_path" in data and data["smb_unc_path"] is not None:
        config.smb_unc_path = data["smb_unc_path"]
    if "smb_user" in data and data["smb_user"] is not None:
        config.smb_user = data["smb_user"]
    if data.get("smb_password"):
        config.smb_password = data["smb_password"]
    if "s3_endpoint" in data and data["s3_endpoint"] is not None:
        config.s3_endpoint = data["s3_endpoint"]
    if "s3_bucket" in data and data["s3_bucket"] is not None:
        config.s3_bucket = data["s3_bucket"]
    if "s3_region" in data and data["s3_region"] is not None:
        config.s3_region = data["s3_region"]
    if data.get("s3_access_key"):
        config.s3_access_key = data["s3_access_key"]
    if data.get("s3_secret_key"):
        config.s3_secret_key = data["s3_secret_key"]
    if "perf_parallel_threads" in data and data["perf_parallel_threads"] is not None:
        config.perf_parallel_threads = data["perf_parallel_threads"]
    if "perf_compression_level" in data and data["perf_compression_level"] is not None:
        config.perf_compression_level = data["perf_compression_level"]
    if "backup_timeout_mins" in data and data["backup_timeout_mins"] is not None:
        config.backup_timeout_mins = data["backup_timeout_mins"]
    db.commit()
    db.refresh(config)
    return config


def update_full_config(db, data):
    config = get_or_create_config(db)
    storage_fields = {
        "storage_type", "nfs_path", "smb_unc_path", "smb_user", "s3_endpoint",
        "s3_bucket", "s3_region", "perf_parallel_threads", "perf_compression_level",
        "backup_timeout_mins", "max_global_backups", "max_backups_per_host",
        "datastore_min_free_pct", "datastore_headroom_gb", "datastore_est_multiplier",
    }
    email_fields = {
        "smtp_server", "smtp_port", "smtp_user", "smtp_to_email", "smtp_use_tls",
        "smtp_use_ssl", "imap_server", "imap_port", "imap_user", "imap_use_ssl",
    }
    secret_fields = {"smb_password", "smtp_password", "imap_password", "s3_access_key", "s3_secret_key"}

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
    }
    if include_secrets:
        data["password"] = host.password
    return data


def add_esxi_host(db, name, host_ip, username, password):
    existing = db.query(ESXiHost).filter(ESXiHost.name == name).first()
    if existing:
        raise ValueError(f"Host '{name}' already exists")
    host = ESXiHost(name=name, host_ip=host_ip, username=username, password=password)
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
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if not host:
        raise ValueError("Invalid ESXi host")
    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
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
                esxi_host_id=host.id,
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
            if vm.esxi_host_id != host.id:
                vm.esxi_host_id = host.id
    db.commit()
    return {"synced_new": synced, "total_on_host": len(vm_list)}


def vm_to_dict(vm):
    return {
        "id": vm.id,
        "vm_name": vm.vm_name,
        "esxi_host_id": vm.esxi_host_id,
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
        "is_job_active", "power_off_for_backup", "schedule_frequency",
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


def trigger_backup(db, vm_id):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise ValueError("VM not found")
    vm.current_action = "PENDING_RUN"
    db.commit()
    return vm


def stop_backup(db, vm_id):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise ValueError("VM not found")
    vm.current_action = "PENDING_STOP"
    db.commit()
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


def start_restore(db, target_esxi_id, source_ova, target_name, datastore):
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


def stop_restore(db, job_id):
    job = db.query(RestoreJob).filter(RestoreJob.id == job_id).first()
    if not job:
        raise ValueError("Restore job not found")
    if job.status != "In Progress":
        raise ValueError("Job not in progress")
    job.is_cancelled = True
    job.current_action = "Stopping..."
    db.commit()
    return job


def delete_restore(db, job_id):
    job = db.query(RestoreJob).filter(RestoreJob.id == job_id).first()
    if not job:
        return False
    db.delete(job)
    db.commit()
    return True
