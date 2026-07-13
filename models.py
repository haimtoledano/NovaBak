from sqlalchemy import Column, Integer, String, Boolean, DateTime, create_engine, ForeignKey, Float
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime
import sqlite3
import os
from config_env import SQLALCHEMY_DATABASE_URL, DATA_DIR

Base = declarative_base()

# Notification event keys available for user subscriptions
NOTIFY_EVENTS = [
    ("backup_success",  "Backup completed successfully"),
    ("backup_failure",  "Backup failed"),
    ("backup_start",    "Backup job started"),
    ("restore_success", "Restore completed successfully"),
    ("restore_failure", "Restore job failed"),
    ("vm_powered_off",  "VM powered off for backup"),
    ("snapshot_cleanup","Snapshot purge triggered"),
]

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    mfa_secret = Column(String, nullable=True)
    is_mfa_enabled = Column(Boolean, default=False)
    role = Column(String, default="admin")  # admin | operator | viewer
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    email = Column(String, default="")  # Personal email for notifications
    notify_subscriptions = Column(String, default="")  # Comma-separated event keys
    failed_login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String)
    key_hash = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    user = relationship("User", back_populates="api_keys")

class ESXiHost(Base):
    __tablename__ = "esxi_hosts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True) # User-friendly display name
    host_ip = Column(String)
    username = Column(String)
    password = Column(String) # For production this should ideally be encrypted
    host_type = Column(String, default="esxi") # "esxi" or "vcenter"
    
    # Establish a relationship with VMs
    vms = relationship("VM", back_populates="esxi_host", cascade="all, delete-orphan")

class StorageTarget(Base):
    __tablename__ = "storage_targets"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True) # User-friendly name
    is_default = Column(Boolean, default=False)
    
    storage_type = Column(String, default="SMB") # SMB, NFS, S3
    
    # SMB Fields
    smb_unc_path = Column(String, default="")
    smb_user = Column(String, default="")
    smb_password = Column(String, default="")
    
    # NFS Fields
    nfs_path = Column(String, default="")
    
    # S3 Fields
    s3_endpoint = Column(String, default="")
    s3_access_key = Column(String, default="")
    s3_secret_key = Column(String, default="")
    s3_bucket = Column(String, default="")
    s3_region = Column(String, default="us-east-1")
    
    # Relationships
    vms = relationship("VM", back_populates="storage_target")

class Config(Base):
    __tablename__ = "config"
    id = Column(Integer, primary_key=True, index=True)
    
    # Global Backup Encryption Key (AES-256 base64 encoded)
    encryption_key = Column(String, nullable=True)
    
    # Email Settings
    smtp_server = Column(String, default="")
    smtp_port = Column(Integer, default=587)
    smtp_user = Column(String, default="")
    smtp_password = Column(String, default="")
    smtp_to_email = Column(String, default="")
    smtp_use_tls = Column(Boolean, default=True)
    smtp_use_ssl = Column(Boolean, default=False)
    # IMAP Settings
    imap_server = Column(String, default="")
    imap_port = Column(Integer, default=993)
    imap_user = Column(String, default="")
    imap_password = Column(String, default="")
    imap_use_ssl = Column(Boolean, default=True)
    
    # Network Security
    allowed_ips = Column(String, nullable=True)
    
    # Webhooks & Reporting
    webhook_url = Column(String, default="")
    daily_report_time = Column(String, default="08:00")
    
    # Performance & Concurrency Tuning
    perf_parallel_threads = Column(Integer, default=0) # 0 = default
    perf_compression_level = Column(Integer, default=0) # 0 = default
    backup_timeout_mins = Column(Integer, default=15) # Default wait for idle/consolidation
    max_global_backups = Column(Integer, default=10)
    max_backups_per_host = Column(Integer, default=2)
    datastore_min_free_pct = Column(Integer, default=15)
    datastore_headroom_gb = Column(Integer, default=10)
    datastore_est_multiplier = Column(Float, default=2.0)
    scheduler_paused = Column(Boolean, default=False)

class VM(Base):
    """List of VMs fetched from ESXi and marked for backup"""
    __tablename__ = "vms"
    id = Column(Integer, primary_key=True, index=True)
    
    # Foreign Key associating VM with a specific ESXi Host
    esxi_host_id = Column(Integer, ForeignKey("esxi_hosts.id"))
    esxi_host = relationship("ESXiHost", back_populates="vms")

    # Foreign Key associating VM with a specific Storage Target (nullable means use default)
    storage_target_id = Column(Integer, ForeignKey("storage_targets.id"), nullable=True)
    storage_target = relationship("StorageTarget", back_populates="vms")
    
    vm_name = Column(String, unique=True)
    is_selected = Column(Boolean, default=False)
    
    # Hardware Config (synced from ESXi)
    cpu_count = Column(Integer, default=0)
    memory_mb = Column(Integer, default=0)
    storage_gb = Column(Float, default=0.0)

    
    # Per-VM Schedule & Retention
    schedule_hour = Column(Integer, default=2) # 2 AM default
    schedule_minute = Column(Integer, default=0)
    retention_count = Column(Integer, default=2) # Number of copies to keep
    is_job_active = Column(Boolean, default=True)
    schedule_frequency = Column(String, default="daily")  # daily | weekly | monthly
    schedule_days = Column(String, default="0,1,2,3,4,5,6")  # APScheduler day_of_week: 0=Mon … 6=Sun
    last_backup = Column(DateTime, nullable=True)
    last_status = Column(String, default="Never")
    progress = Column(Integer, default=0)
    current_action = Column(String, default="")
    power_state = Column(String, default="Unknown") # poweredOn, poweredOff, etc.
    speed_mbps = Column(Float, default=0.0)  # Last known transfer speed
    power_off_for_backup = Column(Boolean, default=False)  # Shutdown VM before backup for faster direct-stream path

    # CBT / Incremental Backup
    backup_type = Column(String, default="full")           # "full" | "incremental"
    full_backup_day = Column(Integer, default=0)           # Day of week for forced full (0=Mon, 6=Sun)
    last_change_id = Column(String, nullable=True)         # Last CBT changeId for incremental chain
    last_full_backup_id = Column(Integer, nullable=True)   # ID of the last full BackupLog entry

class BackupLog(Base):
    __tablename__ = "backup_logs"
    id = Column(Integer, primary_key=True, index=True)
    vm_name = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String) # Success / Failed
    message = Column(String)

    # CBT / Incremental Backup Fields
    change_id = Column(String, nullable=True)
    is_incremental = Column(Boolean, default=False)
    parent_backup_id = Column(Integer, ForeignKey("backup_logs.id"), nullable=True)

    # Verification
    checksum = Column(String, nullable=True)

    # Size tracking (for incremental savings stats)
    backup_size_bytes = Column(Integer, nullable=True)     # Actual bytes written to storage
    disk_total_bytes = Column(Integer, nullable=True)      # Full VM disk size for savings calculation

class RestoreJob(Base):
    __tablename__ = "restore_jobs"
    id = Column(Integer, primary_key=True, index=True)
    target_name = Column(String)
    target_esxi_host = Column(String)
    datastore = Column(String)
    source_path = Column(String)
    status = Column(String, default="In Progress") # In Progress, Success, Failed
    progress = Column(Integer, default=0)
    current_action = Column(String, default="")
    is_cancelled = Column(Boolean, default=False)
    start_time = Column(DateTime, default=datetime.datetime.utcnow)
    end_time = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    is_test_restore = Column(Boolean, default=False)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    username = Column(String, index=True)
    action = Column(String)
    details = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)

# Database startup logic
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

from config_env import BASE_DIR

def init_db():
    from alembic.config import Config as AlembicConfig
    from alembic import command
    
    alembic_cfg = AlembicConfig(os.path.join(BASE_DIR, "alembic.ini"))
    db_path = os.path.join(DATA_DIR, "backup_system.db")
    is_new = not os.path.exists(db_path)
    
    if is_new:
        Base.metadata.create_all(bind=engine)
        command.stamp(alembic_cfg, "head")
    else:
        try:
            command.upgrade(alembic_cfg, "head")
        except Exception as e:
            from logger_util import log_error
            log_error(f"Failed to run alembic migrations: {e}")

    # 3. Default Row Initialization
    db = SessionLocal()
    if not db.query(Config).first():
        default_config = Config()
        db.add(default_config)
        db.commit()
    db.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
