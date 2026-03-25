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

class ESXiHost(Base):
    __tablename__ = "esxi_hosts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True) # User-friendly display name
    host_ip = Column(String)
    username = Column(String)
    password = Column(String) # For production this should ideally be encrypted
    
    # Establish a relationship with VMs
    vms = relationship("VM", back_populates="esxi_host", cascade="all, delete-orphan")

class Config(Base):
    __tablename__ = "config"
    id = Column(Integer, primary_key=True, index=True)
    # TrueNAS SMB Config
    smb_unc_path = Column(String, default="")
    smb_user = Column(String, default="")
    smb_password = Column(String, default="")
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
    # Performance Tuning
    perf_parallel_threads = Column(Integer, default=0) # 0 = default
    perf_compression_level = Column(Integer, default=0) # 0 = default
    backup_timeout_mins = Column(Integer, default=15) # Default wait for idle/consolidation

    # Storage Settings
    storage_type = Column(String, default="SMB") # SMB, NFS, S3
    nfs_path = Column(String, default="")
    s3_endpoint = Column(String, default="")
    s3_access_key = Column(String, default="")
    s3_secret_key = Column(String, default="")
    s3_bucket = Column(String, default="")
    s3_region = Column(String, default="us-east-1")

class VM(Base):
    """List of VMs fetched from ESXi and marked for backup"""
    __tablename__ = "vms"
    id = Column(Integer, primary_key=True, index=True)
    
    # Foreign Key associating VM with a specific ESXi Host
    esxi_host_id = Column(Integer, ForeignKey("esxi_hosts.id"))
    esxi_host = relationship("ESXiHost", back_populates="vms")
    
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

class BackupLog(Base):
    __tablename__ = "backup_logs"
    id = Column(Integer, primary_key=True, index=True)
    vm_name = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String) # Success / Failed
    message = Column(String)
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

# Database startup logic
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    # 1. Physical Table Creation (metadata)
    Base.metadata.create_all(bind=engine)
    
    # 2. Database Migrations (Manual ALTER TABLEs)
    db_path = os.path.join(DATA_DIR, "backup_system.db")
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # List of migrations to run (column_name, sql_command)
        migrations = [
            ("progress", 'ALTER TABLE vms ADD COLUMN progress INTEGER DEFAULT 0'),
            ("current_action", 'ALTER TABLE vms ADD COLUMN current_action VARCHAR DEFAULT ""'),
            ("cpu_count", 'ALTER TABLE vms ADD COLUMN cpu_count INTEGER DEFAULT 0'),
            ("memory_mb", 'ALTER TABLE vms ADD COLUMN memory_mb INTEGER DEFAULT 0'),
            ("storage_gb", 'ALTER TABLE vms ADD COLUMN storage_gb REAL DEFAULT 0.0'),
            ("perf_parallel_threads", 'ALTER TABLE config ADD COLUMN perf_parallel_threads INTEGER DEFAULT 0'),
            ("perf_compression_level", 'ALTER TABLE config ADD COLUMN perf_compression_level INTEGER DEFAULT 0'),
            ("smtp_use_tls", 'ALTER TABLE config ADD COLUMN smtp_use_tls BOOLEAN DEFAULT 1'),
            ("smtp_use_ssl", 'ALTER TABLE config ADD COLUMN smtp_use_ssl BOOLEAN DEFAULT 0'),
            ("backup_timeout_mins", 'ALTER TABLE config ADD COLUMN backup_timeout_mins INTEGER DEFAULT 15'),
            ("imap_server", 'ALTER TABLE config ADD COLUMN imap_server VARCHAR DEFAULT ""'),
            ("imap_port", 'ALTER TABLE config ADD COLUMN imap_port INTEGER DEFAULT 993'),
            ("imap_user", 'ALTER TABLE config ADD COLUMN imap_user VARCHAR DEFAULT ""'),
            ("imap_password", 'ALTER TABLE config ADD COLUMN imap_password VARCHAR DEFAULT ""'),
            ("imap_use_ssl", 'ALTER TABLE config ADD COLUMN imap_use_ssl BOOLEAN DEFAULT 1'),
            ("power_state", 'ALTER TABLE vms ADD COLUMN power_state VARCHAR DEFAULT "Unknown"'),
            ("storage_type", 'ALTER TABLE config ADD COLUMN storage_type VARCHAR DEFAULT "SMB"'),
            ("nfs_path", 'ALTER TABLE config ADD COLUMN nfs_path VARCHAR DEFAULT ""'),
            ("s3_endpoint", 'ALTER TABLE config ADD COLUMN s3_endpoint VARCHAR DEFAULT ""'),
            ("s3_access_key", 'ALTER TABLE config ADD COLUMN s3_access_key VARCHAR DEFAULT ""'),
            ("s3_secret_key", 'ALTER TABLE config ADD COLUMN s3_secret_key VARCHAR DEFAULT ""'),
            ("s3_bucket", 'ALTER TABLE config ADD COLUMN s3_bucket VARCHAR DEFAULT ""'),
            ("s3_region", 'ALTER TABLE config ADD COLUMN s3_region VARCHAR DEFAULT "us-east-1"'),
            ("is_cancelled", 'ALTER TABLE restore_jobs ADD COLUMN is_cancelled BOOLEAN DEFAULT 0'),
            ("speed_mbps", 'ALTER TABLE vms ADD COLUMN speed_mbps REAL DEFAULT 0.0'),
            ("power_off_for_backup", 'ALTER TABLE vms ADD COLUMN power_off_for_backup BOOLEAN DEFAULT 0'),
            ("role", "ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'admin'"),
            ("email", "ALTER TABLE users ADD COLUMN email VARCHAR DEFAULT ''"),
            ("notify_subscriptions", "ALTER TABLE users ADD COLUMN notify_subscriptions VARCHAR DEFAULT ''"),
            ("schedule_frequency", "ALTER TABLE vms ADD COLUMN schedule_frequency VARCHAR DEFAULT 'daily'"),
            ("schedule_days", "ALTER TABLE vms ADD COLUMN schedule_days VARCHAR DEFAULT '0,1,2,3,4,5,6'"),
        ]
        
        from logger_util import log_info, log_warn
        for col_name, sql in migrations:
            try:
                cursor.execute(sql)
                log_info(f"[MIGRATION] Added column {col_name}")
            except sqlite3.OperationalError:
                # Column likely already exists
                pass
            except Exception as e:
                log_warn(f"[MIGRATION] Failed to add column {col_name}: {e}")
        
        conn.commit()
        conn.close()

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
