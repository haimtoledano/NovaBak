from sqlalchemy import Column, Integer, String, Boolean, DateTime, create_engine, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime
from config_env import SQLALCHEMY_DATABASE_URL

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    mfa_secret = Column(String, nullable=True) # TOTP Secret
    is_mfa_enabled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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

class VM(Base):
    """List of VMs fetched from ESXi and marked for backup"""
    __tablename__ = "vms"
    id = Column(Integer, primary_key=True, index=True)
    
    # Foreign Key associating VM with a specific ESXi Host
    esxi_host_id = Column(Integer, ForeignKey("esxi_hosts.id"))
    esxi_host = relationship("ESXiHost", back_populates="vms")
    
    vm_name = Column(String, unique=True)
    is_selected = Column(Boolean, default=False)
    
    # Per-VM Schedule & Retention
    schedule_hour = Column(Integer, default=2) # 2 AM default
    schedule_minute = Column(Integer, default=0)
    retention_count = Column(Integer, default=2) # Number of copies to keep
    is_job_active = Column(Boolean, default=True)
    last_backup = Column(DateTime, nullable=True)
    last_status = Column(String, default="Never")
    progress = Column(Integer, default=0)
    current_action = Column(String, default="")

class BackupLog(Base):
    __tablename__ = "backup_logs"
    id = Column(Integer, primary_key=True, index=True)
    vm_name = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String) # Success / Failed
    message = Column(String)

# Database startup logic
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
    
    # Create default config row if not exists
    db = SessionLocal()
    if not db.query(Config).first():
        default_config = Config()
        db.add(default_config)
        db.commit()
    db.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
