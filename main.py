import os
import sys
import uvicorn
from fastapi import FastAPI, Depends, Request, Form, status, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from models import SessionLocal, init_db, Config, VM, BackupLog, User, ESXiHost, RestoreJob
import esxi_handler
import worker
from config_env import TEMPLATES_DIR, DATA_DIR
import auth
from fastapi.security import APIKeyCookie
import pyotp
import threading
import time
from logger_util import log_info, log_warn, log_error, log_critical
from services import backup_ops

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from limiter import limiter

app = FastAPI(title="NovaBak")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

from api.v1.router import router as v1_router
v1_app = FastAPI(
    title="NovaBak API v1",
    version="1.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)
v1_app.include_router(v1_router)
app.mount("/api/v1", v1_app)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
cookie_sec = APIKeyCookie(name="session_token", auto_error=False)

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(request: Request, token: str = Depends(cookie_sec), db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    username = auth.decode_access_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user

@app.on_event("startup")
def startup_event():
    init_db()
    # Create default admin and password = admin if no users exist
    # Cleanup: Reset any stuck jobs flag in the DB on startup
    db = SessionLocal()
    pid = os.getpid()
    log_info(f"[PID {pid}] Application starting up...")
    vms = db.query(VM).all()
    for v in vms:
        if v.current_action:
            log_info(f"[PID {pid}] Clearing stale action '{v.current_action}' for VM {v.vm_name}")
            v.progress = 0
            v.current_action = ""
    
    # Create default admin and password = admin if no users exist
    if not db.query(User).first():
        hashed = auth.get_password_hash("admin")
        admin = User(username="admin", hashed_password=hashed)
        db.add(admin)
        log_info(f"[PID {pid}] Created default admin user.")
        
    db.commit()
    db.close()
    
    # Keep a reference to the scheduler so it stays alive
    log_info(f"[PID {pid}] Control Plane (Web UI) active. Worker Daemon handles scheduler externally.")


from fastapi import HTTPException

def require_auth(request: Request):
    """ Dependency hack to redirect to login if not authenticated for HTML pages """
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    username = auth.decode_access_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return username

@app.get("/login")
def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
@limiter.limit("5/minute")
def login_post(request: Request, username: str = Form(...), password: str = Form(...), mfa_code: str = Form(None), db: Session = Depends(get_db)):
    from datetime import datetime, timedelta
    user = db.query(User).filter(User.username == username).first()
    
    if user and user.locked_until and user.locked_until > datetime.utcnow():
        return templates.TemplateResponse("login.html", {"request": request, "error": f"Account locked until {user.locked_until.strftime('%H:%M:%S UTC')}"})
        
    if not user or not auth.verify_password(password, user.hashed_password):
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.locked_until = datetime.utcnow() + timedelta(minutes=15)
            db.commit()
        return templates.TemplateResponse("login.html", {"request": request, "error": "Incorrect username or password"})
        
    if user.is_mfa_enabled:
        if not mfa_code or not auth.verify_totp(user.mfa_secret, mfa_code):
            return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid MFA Code"})
            
    # Reset failed attempts
    if user.failed_login_attempts > 0:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()
            
    # Login success
    token = auth.create_access_token(username)
    refresh_token = auth.create_refresh_token(username)
    
    # If MFA not enabled, force setup
    if not user.is_mfa_enabled:
        secret = auth.generate_mfa_secret()
        uri = auth.get_totp_uri(secret, username)
        qr_b64 = auth.generate_qr_code(uri)
        
        from logger_util import log_audit
        log_audit(db, username, "login", "User logged in (Pending MFA setup)", request.client.host)
        
        response = templates.TemplateResponse("mfa_setup.html", {"request": request, "qr_code": qr_b64, "secret": secret})
        response.set_cookie(key="session_token", value=token, httponly=True)
        response.set_cookie(key="refresh_token", value=refresh_token, httponly=True, path="/refresh")
        return response

    from logger_util import log_audit
    log_audit(db, username, "login", "User logged in", request.client.host)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="session_token", value=token, httponly=True)
    response.set_cookie(key="refresh_token", value=refresh_token, httponly=True, path="/refresh")
    return response

@app.post("/refresh")
def refresh_token(request: Request):
    refresh_cookie = request.cookies.get("refresh_token")
    if not refresh_cookie:
        raise HTTPException(status_code=401, detail="No refresh token")
    username = auth.decode_refresh_token(refresh_cookie)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    
    new_access_token = auth.create_access_token(username)
    response = JSONResponse({"status": "ok"})
    response.set_cookie(key="session_token", value=new_access_token, httponly=True, path="/")
    return response

@app.post("/mfa_verify")
def mfa_verify(request: Request, secret: str = Form(...), mfa_code: str = Form(...), db: Session = Depends(get_db)):
    token = request.cookies.get("session_token")
    if not token:
        return RedirectResponse(url="/login", status_code=303)
    username = auth.decode_access_token(token)
    user = db.query(User).filter(User.username == username).first()
    
    if auth.verify_totp(secret, mfa_code):
        user.mfa_secret = secret
        user.is_mfa_enabled = True
        db.commit()
        return RedirectResponse(url="/", status_code=303)
    else:
        uri = auth.get_totp_uri(secret, username)
        qr_b64 = auth.generate_qr_code(uri)
        return templates.TemplateResponse("mfa_setup.html", {"request": request, "qr_code": qr_b64, "secret": secret, "error": "Invalid code, try again."})

@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("session_token")
    if token:
        try:
            username = auth.decode_access_token(token)
            from logger_util import log_audit
            log_audit(db, username, "logout", "User logged out", request.client.host)
        except Exception:
            pass
            
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response


# ─── Role Enforcement ─────────────────────────────────────────────────────────

def require_role(request: Request, *allowed_roles, db: Session = None):
    """Returns the current user if they have one of the allowed roles, else raises 403."""
    username = require_auth(request)
    if db is None:
        db = SessionLocal()
    user = db.query(User).filter(User.username == username).first()
    if not user or (user.role or "admin") not in allowed_roles:
        raise HTTPException(status_code=403, detail="Access denied")
    return user

def get_current_user_from_request(request: Request, db: Session = Depends(get_db)):
    username = require_auth(request)
    return db.query(User).filter(User.username == username).first()

# ─── Admin: User Management ───────────────────────────────────────────────────

from services import user_ops
import urllib.parse

@app.post("/admin/add_user")
def admin_add_user(
    request: Request,
    username: str = Form(...),
    role: str = Form("operator"),
    db: Session = Depends(get_db)
):
    current = require_role(request, "admin", db=db)
    try:
        new_user, temp_pw = user_ops.create_user(db, username, role, current.username, request.client.host)
        log_info(f"[ADMIN] Created user '{username}' with role '{role}'")
        return RedirectResponse(url=f"/?tab=users&new_user={urllib.parse.quote(username)}&new_pw={urllib.parse.quote(temp_pw)}", status_code=303)
    except ValueError as e:
        return RedirectResponse(url=f"/?tab=users&user_error=exists", status_code=303)

@app.post("/admin/delete_user")
def admin_delete_user(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db)
):
    current = require_role(request, "admin", db=db)
    try:
        target = user_ops.delete_user(db, user_id, current.username, request.client.host)
        log_info(f"[ADMIN] Deleted user '{target.username}'")
        return RedirectResponse(url="/?tab=users&user_ok=deleted", status_code=303)
    except ValueError as e:
        if "Cannot delete your own account" in str(e):
            return RedirectResponse(url="/?tab=users&user_error=self_delete", status_code=303)
        return RedirectResponse(url="/?tab=users", status_code=303)

@app.post("/admin/reset_password")
def admin_reset_password(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db)
):
    current = require_role(request, "admin", db=db)
    try:
        target, temp_pw = user_ops.reset_password(db, user_id, current.username, request.client.host)
        log_info(f"[ADMIN] Reset password for user '{target.username}'")
        return RedirectResponse(url=f"/?tab=users&reset_user={urllib.parse.quote(target.username)}&reset_pw={urllib.parse.quote(temp_pw)}", status_code=303)
    except ValueError:
        return RedirectResponse(url="/?tab=users", status_code=303)

@app.post("/admin/reset_mfa")
def admin_reset_mfa(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db)
):
    current = require_role(request, "admin", db=db)
    try:
        target = user_ops.reset_mfa(db, user_id, current.username, request.client.host)
        log_info(f"[ADMIN] Reset MFA for user '{target.username}' — will be prompted on next login")
        return RedirectResponse(url="/?tab=users&user_ok=mfa_reset", status_code=303)
    except ValueError:
        return RedirectResponse(url="/?tab=users", status_code=303)

@app.post("/admin/update_role")
def admin_update_role(
    request: Request,
    user_id: int = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db)
):
    current = require_role(request, "admin", db=db)
    try:
        target = user_ops.update_role(db, user_id, role, current.username, request.client.host)
        log_info(f"[ADMIN] Changed role of '{target.username}' to '{role}'")
        return RedirectResponse(url="/?tab=users&user_ok=role_updated", status_code=303)
    except ValueError:
        return RedirectResponse(url="/?tab=users", status_code=303)

@app.get("/")
def read_root(request: Request, db: Session = Depends(get_db)):
    try:
        username = require_auth(request)
    except HTTPException as e:
        return RedirectResponse(url="/login", status_code=303)
        
    user = db.query(User).filter(User.username == username).first()
    config = db.query(Config).first()
    vms = db.query(VM).all()
    logs = db.query(BackupLog).order_by(BackupLog.timestamp.desc()).limit(10).all()
    users = db.query(User).all()
    esxi_hosts = db.query(ESXiHost).all()
    selected_vm_count = db.query(VM).filter(VM.is_selected == True).count()
    setup_wizard_suggested = len(esxi_hosts) == 0 or selected_vm_count == 0
            
    from models import NOTIFY_EVENTS
    return templates.TemplateResponse("index.html", {
        "request": request,
        "config": config,
        "vms": vms,
        "logs": logs,
        "users": users,
        "current_user": user,
        "esxi_hosts": esxi_hosts,
        "setup_wizard_suggested": setup_wizard_suggested,
        "notify_events": NOTIFY_EVENTS,
    })


@app.post("/save_config")
def save_config(
    request: Request,
    smb_unc_path: str = Form(""),
    smb_user: str = Form(""),
    smb_password: str = Form(""),
    smtp_server: str = Form(""),
    smtp_port: int = Form(587),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: bool = Form(True),
    smtp_use_ssl: bool = Form(False),
    imap_server: str = Form(""),
    imap_port: int = Form(993),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    imap_use_ssl: bool = Form(True),
    perf_compression_level: int = Form(0),
    perf_parallel_threads: int = Form(0),
    backup_timeout_mins: int = Form(15),
    max_global_backups: int = Form(10),
    max_backups_per_host: int = Form(2),
    datastore_min_free_pct: int = Form(15),
    datastore_headroom_gb: int = Form(10),
    datastore_est_multiplier: float = Form(2.0),

    storage_type: str = Form("SMB"),
    nfs_path: str = Form(""),
    s3_endpoint: str = Form(""),
    s3_access_key: str = Form(""),
    s3_secret_key: str = Form(""),
    s3_bucket: str = Form(""),
    s3_region: str = Form("us-east-1"),
    db: Session = Depends(get_db)
):
    try:
        require_auth(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
        
    config = db.query(Config).first()
    config.smb_unc_path = smb_unc_path
    config.smb_user = smb_user
    if smb_password:
        config.smb_password = smb_password
    config.smtp_server = smtp_server
    config.smtp_port = smtp_port
    config.smtp_user = smtp_user
    if smtp_password:
        config.smtp_password = smtp_password
    config.smtp_to_email = smtp_to_email
    config.smtp_use_tls = smtp_use_tls
    config.smtp_use_ssl = smtp_use_ssl
    config.imap_server = imap_server
    config.imap_port = imap_port
    config.imap_user = imap_user
    if imap_password:
        config.imap_password = imap_password
    config.imap_use_ssl = imap_use_ssl
    
    config.perf_parallel_threads = perf_parallel_threads
    config.perf_compression_level = perf_compression_level
    config.backup_timeout_mins = backup_timeout_mins
    config.max_global_backups = max(1, min(32, max_global_backups))
    config.max_backups_per_host = max(1, min(8, max_backups_per_host))
    config.datastore_min_free_pct = max(5, min(50, datastore_min_free_pct))
    config.datastore_headroom_gb = max(0, min(500, datastore_headroom_gb))
    config.datastore_est_multiplier = max(1.0, min(3.0, float(datastore_est_multiplier)))
    
    config.storage_type = storage_type
    config.nfs_path = nfs_path
    config.s3_endpoint = s3_endpoint
    config.s3_access_key = s3_access_key
    if s3_secret_key:
        config.s3_secret_key = s3_secret_key
    config.s3_bucket = s3_bucket
    config.s3_region = s3_region
    
    db.commit()

    try:
        worker.configure_concurrency(config)
    except Exception:
        pass

    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse({"ok": True, "message": "Configuration saved."})
    return RedirectResponse(url="/?saved=settings", status_code=303)

@app.post("/add_esxi_host")
def add_esxi_host(
    request: Request,
    name: str = Form(...),
    host_ip: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    db: Session = Depends(get_db)
):
    require_auth(request)
    try:
        backup_ops.add_esxi_host(db, name, host_ip, username, password)
    except ValueError as e:
        import urllib.parse
        return RedirectResponse(url=f"/?tab=hosts&error={urllib.parse.quote(str(e))}", status_code=303)
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_esxi_host")
def delete_esxi_host(request: Request, host_id: int = Form(...), db: Session = Depends(get_db)):
    require_auth(request)
    backup_ops.delete_esxi_host(db, host_id)
    return RedirectResponse(url="/", status_code=303)

@app.post("/fetch_vms")
def fetch_vms(request: Request, esxi_host_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        require_auth(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
        
    try:
        backup_ops.sync_vms_for_host(db, esxi_host_id)
    except Exception as e:
        return {"error": str(e)}
        
    return RedirectResponse(url="/", status_code=303)


@app.post("/toggle_vm")
def toggle_vm(request: Request, vm_id: int = Form(...), is_selected: bool = Form(False), db: Session = Depends(get_db)):
    require_auth(request)
    try:
        backup_ops.update_vm_job(db, vm_id, {"is_selected": is_selected})
    except ValueError:
        pass
    return RedirectResponse(url="/", status_code=303)

@app.post("/update_job")
def update_job(
    request: Request, 
    vm_id: int = Form(...), 
    schedule_hour: int = Form(...), 
    schedule_minute: int = Form(...),
    retention_count: int = Form(2),
    is_job_active: bool = Form(False),
    power_off_for_backup: bool = Form(False),
    schedule_frequency: str = Form("daily"),
    schedule_days: str = Form("0,1,2,3,4,5,6"),
    db: Session = Depends(get_db)
):
    require_auth(request)
    try:
        backup_ops.update_vm_job(db, vm_id, {
            "schedule_hour": schedule_hour,
            "schedule_minute": schedule_minute,
            "retention_count": retention_count,
            "is_job_active": is_job_active,
            "power_off_for_backup": power_off_for_backup,
            "schedule_frequency": schedule_frequency,
            "schedule_days": schedule_days
        })
    except ValueError:
        pass
    return RedirectResponse(url="/", status_code=303)

@app.post("/run_now")
def run_now(request: Request, vm_id: int = Form(...), db: Session = Depends(get_db)):
    username = require_auth(request)
    try:
        from services import backup_ops
        backup_ops.trigger_backup(db, vm_id, username, request.client.host)
    except ValueError as e:
        pass
    return RedirectResponse(url="/", status_code=303)

@app.post("/test_storage")
def test_storage(request: Request, db: Session = Depends(get_db)):
    require_auth(request)
    config = db.query(Config).first()
    if not config:
        return {"status": "error", "message": "No configuration found."}
    
    try:
        import storage_util
        storage = storage_util.get_storage(config)
        if config.storage_type == "SMB":
            success, msg = worker.authenticate_smb(config)
            if not success: return {"status": "error", "message": msg}
        
        # Try a simple 'exists' or 'list' to verify
        storage.list_dirs("")
        return {"status": "success", "message": f"Successfully connected to {config.storage_type} storage."}
    except Exception as e:
        return {"status": "error", "message": f"Connection failed: {str(e)}"}

@app.post("/test_smb")
def test_smb(request: Request, db: Session = Depends(get_db)):
    require_auth(request)
    config = db.query(Config).first()
    if not config or not config.smb_unc_path:
        return {"status": "error", "message": "No SMB path configured. Please save settings first."}
    
    success, msg = worker.authenticate_smb(config)
    return {"status": "success" if success else "error", "message": msg}

@app.post("/api/test_smtp")
def api_test_smtp(request: Request, db: Session = Depends(get_db)):
    require_auth(request)
    config = db.query(Config).first()
    if not config or not config.smtp_server:
        return JSONResponse({"ok": False, "message": "SMTP server not configured. Please save settings first."})
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText("This is a test email from NovaBak Enterprise.")
        msg["Subject"] = "[NovaBak] Test Email"
        msg["From"] = config.smtp_user if config.smtp_user else "novabak@local"
        msg["To"] = config.smtp_to_email
        if config.smtp_use_ssl:
            server = smtplib.SMTP_SSL(config.smtp_server, config.smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(config.smtp_server, config.smtp_port, timeout=10)
        if not config.smtp_use_ssl and config.smtp_use_tls:
            server.starttls()
        if config.smtp_user and config.smtp_password:
            server.login(config.smtp_user, config.smtp_password)
        server.sendmail(msg["From"], [config.smtp_to_email], msg.as_string())
        server.quit()
        log_info(f"[SMTP TEST] Test email sent to {config.smtp_to_email}")
        return JSONResponse({"ok": True, "message": f"Test email sent to {config.smtp_to_email}"})
    except Exception as e:
        log_warn(f"[SMTP TEST] Failed: {e}")
        return JSONResponse({"ok": False, "message": f"SMTP test failed: {str(e)}"})


@app.get("/get_datastores/{host_id}")
def get_datastores(request: Request, host_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if not host:
        return {"error": "Invalid host"}
        
    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        return {"error": "Could not connect to ESXi host"}
        
    datastores = esxi_handler.get_datastores(si)
    esxi_handler.Disconnect(si)
    return datastores

@app.get("/get_backups")
def get_backups(request: Request, db: Session = Depends(get_db)):
    try:
        require_auth(request)
    except HTTPException:
        return {"error": "Authentication required"}
        
    try:
        config = db.query(Config).first()
        if not config:
            return {"error": "No configuration found"}
        backups = worker.get_available_backups(config)
        return backups
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        log_error(f"GET_BACKUPS CRASH: {err}")
        return {"error": f"System Error: {str(e)}"}

@app.get("/api/backups_grouped")
def get_backups_grouped(request: Request, db: Session = Depends(get_db)):
    """Returns backups grouped by VM name for hierarchical restore UI."""
    try:
        require_auth(request)
    except HTTPException:
        return {"error": "Authentication required"}
    try:
        config = db.query(Config).first()
        if not config:
            return {"error": "No configuration found"}
        backups = worker.get_available_backups(config)
        # Group by vm_name
        grouped = {}
        for b in backups:
            vm = b["vm_name"]
            if vm not in grouped:
                grouped[vm] = []
            grouped[vm].append({"date": b["date"], "path": b["path"], "size": b["size"]})
        # Convert to sorted list of {vm_name, versions: [...]}
        result = [
            {"vm_name": vm, "versions": versions}
            for vm, versions in sorted(grouped.items())
        ]
        return result
    except Exception as e:
        import traceback
        log_error(f"GET_BACKUPS_GROUPED CRASH: {traceback.format_exc()}")
        return {"error": f"System Error: {str(e)}"}

@app.post("/restore")
async def restore(
    request: Request,
    target_esxi_id: int = Form(...),
    source_ova: str = Form(...),
    target_name: str = Form(...),
    datastore: str = Form(...),
    is_test_restore: bool = Form(False),
    db: Session = Depends(get_db)
):
    require_auth(request)
    config = db.query(Config).first()
    target_host = db.query(ESXiHost).filter(ESXiHost.id == target_esxi_id).first()
    
    if not config or not target_host:
        return RedirectResponse(url="/", status_code=303)
        
    # Run the restore asynchronously
    worker.authenticate_smb(config)
    # Create Restore Job Entry
    restore_job = RestoreJob(
        target_name=target_name,
        target_esxi_host=target_host.name,
        datastore=datastore,
        source_path=source_ova,
        status="In Progress",
        progress=0,
        current_action="Initializing...",
        is_test_restore=is_test_restore
    )
    db.add(restore_job)
    db.commit()
    db.refresh(restore_job)

    # Add to Queue
    worker.restore_queue_executor.submit(
        worker.perform_restore,
        config, target_host.host_ip, target_host.username, target_host.password,
        source_ova, target_name, datastore, restore_job.id
    )
    return RedirectResponse(url="/", status_code=303)

@app.get("/api/restores")
def get_restores(request: Request, db: Session = Depends(get_db)):
    require_auth(request)
    restores = db.query(RestoreJob).order_by(RestoreJob.start_time.desc()).limit(10).all()
    # Convert to list of dicts for JSON
    return [{
        "id": r.id,
        "target_name": r.target_name,
        "target_esxi": r.target_esxi_host,
        "status": r.status,
        "progress": r.progress,
        "action": r.current_action,
        "start": r.start_time.strftime("%H:%M:%S") if r.start_time else "",
        "error": r.error_message
    } for r in restores]

@app.post("/api/stop_restore/{job_id}")
def stop_restore(request: Request, job_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    job = db.query(RestoreJob).filter(RestoreJob.id == job_id).first()
    if job and job.status == "In Progress":
        job.is_cancelled = True
        job.current_action = "Stopping..."
        db.commit()
        return {"status": "ok"}
    return {"status": "error", "message": "Job not found or already completed"}

@app.post("/api/delete_restore/{job_id}")
def delete_restore(request: Request, job_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    job = db.query(RestoreJob).filter(RestoreJob.id == job_id).first()
    if job:
        db.delete(job)
        db.commit()
        return {"status": "ok"}
    return {"status": "error", "message": "Job not found"}
    

@app.post("/profile/update")
def profile_update(
    request: Request,
    email: str = Form(""),
    notify_subscriptions: str = Form(""),
    db: Session = Depends(get_db)
):
    """Lets any logged-in user update their own email address and notification subscriptions."""
    username = require_auth(request)
    user = db.query(User).filter(User.username == username).first()
    if user:
        user.email = email.strip()
        # notify_subscriptions arrives as a comma-separated string built by JS from checked checkboxes
        user.notify_subscriptions = notify_subscriptions.strip()
        db.commit()
    return RedirectResponse(url="/?tab=settings&profile_saved=1", status_code=303)


@app.post("/profile/password")
def profile_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db)
):
    username = require_auth(request)
    try:
        from services import user_ops
        user_ops.change_password(db, username, current_password, new_password, request.client.host)
    except ValueError as e:
        return RedirectResponse(url=f"/?tab=settings&pw_error={e}", status_code=303)
    return RedirectResponse(url="/?tab=settings&pw_saved=1", status_code=303)



@app.post("/stop_job")
def stop_job(request: Request, vm_id: int = Form(...), db: Session = Depends(get_db)):
    username = require_auth(request)
    from services import backup_ops
    try:
        backup_ops.stop_backup(db, vm_id, username, request.client.host)
    except ValueError:
        pass
    return RedirectResponse(url="/", status_code=303)
@app.get("/job_progress")
def get_job_progress(request: Request, db: Session = Depends(get_db)):
    try:
        require_auth(request)
    except HTTPException:
        return {}
    
    vms = db.query(VM).all()
    out = {}
    for vm in vms:
        out[vm.id] = {
            "progress": vm.progress or 0,
            "current_action": vm.current_action or "",
            "speed_mbps": round(getattr(vm, 'speed_mbps', 0) or 0, 1)
        }
    return out


@app.get("/overview")
def get_overview(request: Request, db: Session = Depends(get_db)):
    try:
        require_auth(request)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from services import backup_ops
    return backup_ops.get_overview(db)

@app.post("/cleanup_all_snapshots")
def cleanup_all_snapshots(request: Request, db: Session = Depends(get_db)):
    require_auth(request)
    vms = db.query(VM).all()
    
    # We'll do this in a thread because it can take a long time
    def run_global_cleanup():
        # Create a fresh session for the background thread
        from models import SessionLocal
        bg_db = SessionLocal()
        try:
            vms_bg = bg_db.query(VM).all()
            host_sis = {}
            for vm in vms_bg:
                if not vm.esxi_host: continue
                h = vm.esxi_host
                if h.id not in host_sis:
                    si = esxi_handler.connect_esxi(h.host_ip, h.username, h.password)
                    if si:
                        host_sis[h.id] = si
                
                si = host_sis.get(h.id)
                if si:
                    log_info(f"[GLOBAL CLEANUP] Cleaning {vm.vm_name}...")
                    esxi_handler.remove_snapshot(si, vm.vm_name)
            
            for si in host_sis.values():
                esxi_handler.Disconnect(si)
            log_info("[GLOBAL CLEANUP] Finished.")
        finally:
            bg_db.close()

    thread = threading.Thread(target=run_global_cleanup)
    thread.start()
    worker.send_event_notification(
        "snapshot_cleanup",
        "[NovaBak] Snapshot Purge Triggered",
        f"A global snapshot consolidation was initiated by a user at {time.strftime('%Y-%m-%d %H:%M')}."
    )
    return RedirectResponse(url="/?purge_started=1", status_code=303)


@app.get("/api/syslogs")
def get_syslogs(request: Request, s_lines: int = 100, s_search: str = "", w_lines: int = 100, w_search: str = "", db: Session = Depends(get_db)):
    try:
        require_auth(request)
    except HTTPException:
        return {"error": "Authentication required"}
        
    def tail_file(filename, lines=100, search_str=""):
        import os
        from config_env import DATA_DIR
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            return f"[{filename} not found or empty]"
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                lines_list = f.readlines()
                if search_str:
                    search_str = search_str.lower()
                    lines_list = [l for l in lines_list if search_str in l.lower()]
                return "".join(lines_list[-lines:])
        except Exception as e:
            return f"Error reading {filename}: {e}"
            
    return {
        "service_log": tail_file("service.log", s_lines, s_search),
        "worker_log": tail_file("worker.log", w_lines, w_search)
    }

if __name__ == "__main__":
    lock_file = "app.lock"
    lock_fh = None
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except:
            log_critical("Another instance of VM Backup Enterprise is already running.")
            log_critical("Please stop the existing service or manual process before starting a new one.")
            sys.exit(1)
            
    try:
        # Open and keep open to hold the lock on Windows
        lock_fh = open(lock_file, "w")
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()

        # Generate / verify SSL certificate
        from ssl_util import ensure_ssl_cert
        cert_file, key_file = ensure_ssl_cert()

        import uvicorn.config
        l_config = uvicorn.config.LOGGING_CONFIG
        l_config["formatters"]["access"]["fmt"] = "[%(asctime)s] %(levelprefix)s %(message)s"
        l_config["formatters"]["default"]["fmt"] = "[%(asctime)s] %(levelprefix)s %(message)s"

        log_info("=" * 55)
        log_info("  NovaBak is starting on HTTPS port 8001")
        log_info("Open: https://localhost:8001")
        log_info("  (Browser will warn about self-signed cert - click Advanced -> Proceed)")
        log_info("=" * 55)

        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8001,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
            log_config=l_config,
        )
    finally:
        if lock_fh:
            lock_fh.close()
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except:
                pass

