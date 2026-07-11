from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import List, Optional

import auth
from api.deps import get_db, get_api_user, require_api_role, bearer_scheme, _user_from_token, cookie_sec
from api.schemas import (
    LoginRequest, TokenResponse, ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyInfo,
    ConfigResponse, ConfigUpdate, TestResult,
    ESXiHostCreate, ESXiHostUpdate, ESXiHostResponse, VMUpdateRequest, VMResponse, SyncResult,
    UserResponse, UserCreateRequest, UserCreateResponse, UserRoleUpdate,
    PasswordResetResponse, ProfileUpdate, PasswordChangeRequest, BackupLogEntry, SystemLogsResponse,
    RestoreCreateRequest, RestoreResponse, OverviewResponse,
    StorageTargetCreate, StorageTargetUpdate, StorageTargetResponse
)
from models import User, ApiKey
from services import backup_ops
from services import user_ops

router = APIRouter(tags=["api-v1"])


def _authenticate_login(db: Session, username: str, password: str, mfa_code: Optional[str] = None) -> User:
    import datetime
    from logger_util import log_audit
    
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    if user.locked_until and user.locked_until > datetime.datetime.utcnow():
        raise HTTPException(status_code=423, detail="Account is locked due to too many failed attempts. Try again later.")
        
    if not auth.verify_password(password, user.hashed_password):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= 5:
            user.locked_until = datetime.datetime.utcnow() + datetime.timedelta(minutes=15)
            log_audit(db, username, "account_locked", "Account locked due to 5 failed login attempts")
        db.commit()
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    if user.is_mfa_enabled:
        if not mfa_code or not auth.verify_totp(user.mfa_secret, mfa_code):
            raise HTTPException(status_code=401, detail="MFA code required or invalid")
            
    # Reset failed attempts on success
    if user.failed_login_attempts > 0 or user.locked_until is not None:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()
        
    return user


from limiter import limiter
from fastapi import Request

# ─── Auth / Tokens ────────────────────────────────────────────────────────────

@router.post("/auth/token", response_model=TokenResponse)
@router.post("/auth/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def create_token(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    """Create a short-lived JWT bearer token (7 days). Use for API key creation or direct API calls."""
    user = _authenticate_login(db, body.username, body.password, body.mfa_code)
    return TokenResponse(access_token=auth.create_access_token(user.username))



@router.post("/auth/api-keys", response_model=ApiKeyCreateResponse)
def create_api_key(
    body: ApiKeyCreateRequest,
    db: Session = Depends(get_db),
    bearer: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    cookie_token: str = Depends(cookie_sec),
):
    """
    Create a long-lived API key (`nbak_...`).

    Authenticate with session cookie, Bearer token, or username/password in body.
    """
    user = None
    token = bearer.credentials if bearer else cookie_token
    if token:
        try:
            user = _user_from_token(db, token)
        except HTTPException:
            user = None
    if user is None:
        if not body.username or not body.password:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated. Log in to the UI or provide credentials.",
            )
        user = _authenticate_login(db, body.username, body.password, body.mfa_code)

    if (user.role or "admin") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create API keys")

    raw_key, api_key = auth.create_api_key(db, user.id, body.name)
    return ApiKeyCreateResponse(id=api_key.id, name=api_key.name, key=raw_key)


@router.get("/auth/api-keys", response_model=List[ApiKeyInfo])
def list_api_keys(
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    keys = db.query(ApiKey).filter(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()).all()
    return [
        ApiKeyInfo(
            id=k.id,
            name=k.name,
            created_at=k.created_at.isoformat() if k.created_at else "",
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
        )
        for k in keys
    ]


@router.delete("/auth/api-keys/{key_id}")
def revoke_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    key = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user.id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    db.delete(key)
    db.commit()
    return {"ok": True}


@router.get("/auth/me", response_model=UserResponse)
def get_me(user: User = Depends(get_api_user)):
    return UserResponse(**user_ops.user_to_dict(user))


# ─── Config ─────────────────────────────────────────────────────────────────

@router.get("/config", response_model=ConfigResponse)
def get_config(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    config = backup_ops.get_or_create_config(db)
    return ConfigResponse(**backup_ops.config_to_dict(config))


@router.put("/config", response_model=ConfigResponse)
def update_config(
    body: ConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    config = backup_ops.update_full_config(db, body.model_dump(exclude_unset=True))
    return ConfigResponse(**backup_ops.config_to_dict(config))


# ─── Storage Targets ──────────────────────────────────────────────────────────

@router.get("/storage-targets", response_model=List[StorageTargetResponse])
def list_storage_targets(db: Session = Depends(get_db), user: User = Depends(require_api_role("admin", "operator"))):
    return backup_ops.list_storage_targets(db)

@router.post("/storage-targets", response_model=StorageTargetResponse)
def create_storage_target(
    body: StorageTargetCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin"))
):
    return backup_ops.create_storage_target(db, body.model_dump())

@router.put("/storage-targets/{target_id}", response_model=StorageTargetResponse)
def update_storage_target(
    target_id: int,
    body: StorageTargetUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin"))
):
    try:
        return backup_ops.update_storage_target(db, target_id, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/storage-targets/{target_id}")
def delete_storage_target(
    target_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin"))
):
    try:
        backup_ops.delete_storage_target(db, target_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/config/storage/test", response_model=TestResult)
def test_storage(db: Session = Depends(get_db), user: User = Depends(require_api_role("admin", "operator"))):
    ok, message = backup_ops.test_storage(db)
    return TestResult(ok=ok, message=message)


@router.post("/config/email/test", response_model=TestResult)
def test_email(db: Session = Depends(get_db), user: User = Depends(require_api_role("admin"))):
    ok, message = backup_ops.test_smtp(db)
    return TestResult(ok=ok, message=message)


# ─── Users ────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=List[UserResponse])
def list_users(db: Session = Depends(get_db), user: User = Depends(require_api_role("admin"))):
    return [UserResponse(**user_ops.user_to_dict(u)) for u in user_ops.list_users(db)]


@router.post("/users", response_model=UserCreateResponse, status_code=201)
def create_user(
    body: UserCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    try:
        new_user, temp_pw = user_ops.create_user(db, body.username, body.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return UserCreateResponse(user=UserResponse(**user_ops.user_to_dict(new_user)), temporary_password=temp_pw)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    try:
        user_ops.delete_user(db, user_id, user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.patch("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: int,
    body: UserRoleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    try:
        target = user_ops.update_role(db, user_id, body.role, user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return UserResponse(**user_ops.user_to_dict(target))


@router.post("/users/{user_id}/reset-password", response_model=PasswordResetResponse)
def reset_user_password(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    try:
        target, temp_pw = user_ops.reset_password(db, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return PasswordResetResponse(username=target.username, temporary_password=temp_pw)


@router.post("/users/{user_id}/reset-mfa")
def reset_user_mfa(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    try:
        target = user_ops.reset_mfa(db, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "username": target.username, "message": "MFA reset; user will be prompted on next login"}


@router.patch("/profile", response_model=UserResponse)
def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    updated = user_ops.update_profile(
        db,
        user.username,
        email=body.email,
        notify_subscriptions=body.notify_subscriptions,
    )
    return UserResponse(**user_ops.user_to_dict(updated))


@router.post("/profile/password")
def change_password(
    request: Request,
    body: PasswordChangeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    try:
        user_ops.change_password(db, user.username, body.current_password, body.new_password, request.client.host)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": "Password updated successfully"}


# ─── ESXi Hosts ───────────────────────────────────────────────────────────────

@router.get("/hosts", response_model=List[ESXiHostResponse])
def list_hosts(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    from models import ESXiHost
    hosts = db.query(ESXiHost).all()
    return [ESXiHostResponse(**backup_ops.host_to_dict(h)) for h in hosts]


@router.post("/hosts", response_model=ESXiHostResponse, status_code=201)
def create_host(
    body: ESXiHostCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    try:
        host = backup_ops.add_esxi_host(db, body.name, body.host_ip, body.username, body.password, host_type=body.host_type)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return ESXiHostResponse(**backup_ops.host_to_dict(host))


@router.post("/hosts/test", response_model=TestResult)
def test_esxi_host(body: dict, user: User = Depends(require_api_role("admin", "operator"))):
    host_ip = body.get("host_ip")
    username = body.get("username")
    password = body.get("password")
    ok, message = backup_ops.test_esxi_connection(host_ip, username, password)
    return TestResult(ok=ok, message=message)

@router.put("/hosts/{host_id}", response_model=ESXiHostResponse)
def update_esxi_host(
    host_id: int, 
    body: ESXiHostUpdate, 
    db: Session = Depends(get_db), 
    user: User = Depends(require_api_role("admin", "operator"))
):
    try:
        return backup_ops.update_esxi_host(db, host_id, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/hosts/{host_id}")
def remove_host(
    host_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin")),
):
    if not backup_ops.delete_esxi_host(db, host_id):
        raise HTTPException(status_code=404, detail="Host not found")
    return {"ok": True}


@router.get("/hosts/{host_id}/datastores")
def host_datastores(
    host_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    try:
        return backup_ops.get_datastores(db, host_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/hosts/{host_id}/sync-vms", response_model=SyncResult)
def sync_vms(
    host_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    try:
        result = backup_ops.sync_vms_for_host(db, host_id)
        return SyncResult(**result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ─── VMs ──────────────────────────────────────────────────────────────────────

@router.get("/vms", response_model=List[VMResponse])
def list_vms(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    from models import VM
    vms = db.query(VM).order_by(VM.vm_name).all()
    return [VMResponse(**backup_ops.vm_to_dict(v)) for v in vms]


@router.patch("/vms/{vm_id}", response_model=VMResponse)
def patch_vm(
    vm_id: int,
    body: VMUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    try:
        vm = backup_ops.update_vm_job(db, vm_id, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return VMResponse(**backup_ops.vm_to_dict(vm))


@router.post("/vms/{vm_id}/run")
def run_vm_backup(
    request: Request,
    vm_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    try:
        backup_ops.trigger_backup(db, vm_id, user.username, request.client.host)
    except ValueError as e:
        code = 409 if "paused" in str(e).lower() else 404
        raise HTTPException(status_code=code, detail=str(e))
    return {"ok": True, "message": "Backup queued"}


@router.post("/vms/{vm_id}/stop")
def stop_vm_backup(
    request: Request,
    vm_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    try:
        backup_ops.stop_backup(db, vm_id, user.username, request.client.host)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "message": "Stop requested"}


@router.post("/jobs/stop-all")
def stop_all_backups(
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    stopped = backup_ops.stop_all_backups(db)
    return {"ok": True, "count": len(stopped), "vms": stopped}


@router.get("/jobs/scheduler")
def get_scheduler_status(
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    return {"paused": backup_ops.is_scheduler_paused(db)}


@router.post("/jobs/pause")
def pause_scheduler(
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    backup_ops.set_scheduler_paused(db, True)
    return {"ok": True, "paused": True}


@router.post("/jobs/resume")
def resume_scheduler(
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    backup_ops.set_scheduler_paused(db, False)
    return {"ok": True, "paused": False}


# ─── Backups & Restores ───────────────────────────────────────────────────────

@router.get("/backups")
def list_backups(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    try:
        return backup_ops.list_backups_grouped(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/restores", response_model=List[RestoreResponse])
def list_restores(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    return [RestoreResponse(**item) for item in backup_ops.list_restores(db, limit=limit)]


@router.post("/restores", response_model=RestoreResponse, status_code=202)
def create_restore(
    request: Request,
    body: RestoreCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    try:
        job = backup_ops.start_restore(
            db, body.target_esxi_id, body.source_ova, body.target_name, body.datastore, user.username, request.client.host
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RestoreResponse(**backup_ops.restore_to_dict(job))


@router.post("/restores/{job_id}/stop")
def stop_restore_job(
    request: Request,
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    try:
        job = backup_ops.stop_restore(db, job_id, user.username, request.client.host)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": job.id, "message": "Stop requested"}


@router.delete("/restores/{job_id}")
def delete_restore_job(
    request: Request,
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_role("admin", "operator")),
):
    if not backup_ops.delete_restore(db, job_id, user.username, request.client.host):
        raise HTTPException(status_code=404, detail="Restore job not found")
    return {"ok": True}


# ─── Logs & Monitoring ────────────────────────────────────────────────────────

@router.get("/logs/backup", response_model=List[BackupLogEntry])
def backup_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
):
    return [BackupLogEntry(**entry) for entry in backup_ops.list_backup_logs(db, limit=limit)]


@router.get("/logs/system", response_model=SystemLogsResponse)
def system_logs(
    service_lines: int = 100,
    service_search: str = "",
    worker_lines: int = 100,
    worker_search: str = "",
    user: User = Depends(require_api_role("admin", "operator")),
):
    logs = backup_ops.get_system_logs(service_lines, service_search, worker_lines, worker_search)
    return SystemLogsResponse(**logs)


@router.get("/jobs/progress")
def jobs_progress(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    return backup_ops.job_progress(db)


@router.get("/overview", response_model=OverviewResponse)
def overview(db: Session = Depends(get_db), user: User = Depends(get_api_user)):
    return OverviewResponse(**backup_ops.get_overview(db))
