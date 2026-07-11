from typing import Optional, List
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str


class LoginRequest(BaseModel):
    username: str
    password: str
    mfa_code: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    username: Optional[str] = None
    password: Optional[str] = None
    mfa_code: Optional[str] = None


class ApiKeyCreateResponse(BaseModel):
    id: int
    name: str
    key: str
    message: str = "Store this key securely; it will not be shown again."


class ApiKeyInfo(BaseModel):
    id: int
    name: str
    created_at: str
    last_used_at: Optional[str] = None


class StorageConfigUpdate(BaseModel):
    storage_type: Optional[str] = None
    nfs_path: Optional[str] = None
    smb_unc_path: Optional[str] = None
    smb_user: Optional[str] = None
    smb_password: Optional[str] = None
    s3_endpoint: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    perf_parallel_threads: Optional[int] = None
    perf_compression_level: Optional[int] = None
    backup_timeout_mins: Optional[int] = None
    max_global_backups: Optional[int] = None
    max_backups_per_host: Optional[int] = None
    datastore_min_free_pct: Optional[int] = None
    datastore_headroom_gb: Optional[int] = None
    datastore_est_multiplier: Optional[float] = None


class ConfigResponse(BaseModel):
    storage_type: str
    nfs_path: str
    smb_unc_path: str
    smb_user: str
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    perf_parallel_threads: int
    perf_compression_level: int
    backup_timeout_mins: int
    max_global_backups: int
    max_backups_per_host: int
    datastore_min_free_pct: int
    datastore_headroom_gb: int
    datastore_est_multiplier: float
    smtp_server: str
    smtp_port: int
    smtp_user: str
    smtp_to_email: str
    smtp_use_tls: bool
    smtp_use_ssl: bool
    imap_server: str
    imap_port: int
    imap_user: str
    imap_use_ssl: bool


class ConfigUpdate(BaseModel):
    storage_type: Optional[str] = None
    nfs_path: Optional[str] = None
    smb_unc_path: Optional[str] = None
    smb_user: Optional[str] = None
    smb_password: Optional[str] = None
    s3_endpoint: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    perf_parallel_threads: Optional[int] = None
    perf_compression_level: Optional[int] = None
    backup_timeout_mins: Optional[int] = None
    max_global_backups: Optional[int] = None
    max_backups_per_host: Optional[int] = None
    datastore_min_free_pct: Optional[int] = None
    datastore_headroom_gb: Optional[int] = None
    datastore_est_multiplier: Optional[float] = None
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_to_email: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    smtp_use_ssl: Optional[bool] = None
    imap_server: Optional[str] = None
    imap_port: Optional[int] = None
    imap_user: Optional[str] = None
    imap_password: Optional[str] = None
    imap_use_ssl: Optional[bool] = None


class TestResult(BaseModel):
    ok: bool
    message: str


class ESXiHostCreate(BaseModel):
    name: str
    host_ip: str
    username: str
    password: str
    host_type: Optional[str] = "esxi"


class ESXiHostResponse(BaseModel):
    id: int
    name: str
    host_ip: str
    username: str
    host_type: str


class VmUpdate(BaseModel):
    is_selected: Optional[bool] = None
    schedule_hour: Optional[int] = None
    schedule_minute: Optional[int] = None
    retention_count: Optional[int] = None
    is_job_active: Optional[bool] = None
    power_off_for_backup: Optional[bool] = None
    schedule_frequency: Optional[str] = None
    schedule_days: Optional[str] = None


class VmResponse(BaseModel):
    id: int
    vm_name: str
    esxi_host_id: Optional[int]
    is_selected: bool
    cpu_count: int
    memory_mb: int
    storage_gb: float
    schedule_hour: int
    schedule_minute: int
    retention_count: int
    is_job_active: bool
    schedule_frequency: str
    schedule_days: str
    last_backup: Optional[str]
    last_status: str
    progress: int
    current_action: str
    power_state: str
    power_off_for_backup: bool


class SyncResult(BaseModel):
    synced_new: List[str]
    total_on_host: int


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    email: str
    is_mfa_enabled: bool
    created_at: Optional[str] = None


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    role: str = Field(default="operator", pattern="^(admin|operator|viewer)$")


class UserCreateResponse(BaseModel):
    user: UserResponse
    temporary_password: str
    message: str = "Temporary password shown once. User must set up MFA on first login."


class UserRoleUpdate(BaseModel):
    role: str = Field(..., pattern="^(admin|operator|viewer)$")


class PasswordResetResponse(BaseModel):
    username: str
    temporary_password: str
    message: str = "Temporary password shown once."


class ProfileUpdate(BaseModel):
    email: Optional[str] = None
    notify_subscriptions: Optional[str] = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6)



class BackupLogEntry(BaseModel):
    id: int
    vm_name: str
    timestamp: Optional[str]
    status: str
    message: str


class SystemLogsResponse(BaseModel):
    service_log: str
    worker_log: str


class RestoreCreateRequest(BaseModel):
    target_esxi_id: int
    source_ova: str
    target_name: str
    datastore: str


class RestoreResponse(BaseModel):
    id: int
    target_name: str
    target_esxi_host: str
    datastore: str
    source_path: str
    status: str
    progress: int
    current_action: str
    is_cancelled: bool
    start_time: Optional[str]
    end_time: Optional[str]
    error_message: Optional[str]


class OverviewStorage(BaseModel):
    type: str
    path: str
    total_bytes: Optional[int] = None
    total_human: str = "—"
    version_count: int = 0
    vm_count: int = 0
    disk_total_gb: Optional[float] = None
    disk_used_gb: Optional[float] = None
    disk_free_gb: Optional[float] = None
    disk_free_pct: Optional[float] = None
    scan_error: Optional[str] = None


class OverviewLiveJob(BaseModel):
    vm_id: int
    vm_name: str
    host_name: str
    progress: int
    current_action: str
    speed_mbps: float


class OverviewAttentionItem(BaseModel):
    vm_id: int
    vm_name: str
    host_name: str
    reason: str
    severity: str  # error | warning | info
    last_status: str
    last_backup: Optional[str] = None


class OverviewResponse(BaseModel):
    protected_count: int
    scheduled_count: int
    running_count: int
    host_count: int
    inventory_count: int
    status_counts: dict
    log_stats_7d: dict
    success_rate_7d: Optional[float] = None
    storage: OverviewStorage
    worker_online: bool
    worker_last_seen_seconds: Optional[int] = None
    max_global_backups: int
    live_jobs: List[OverviewLiveJob]
    recent_activity: List[BackupLogEntry]
    active_restores: List[RestoreResponse]
    attention: List[OverviewAttentionItem]
    setup_incomplete: bool
