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

app = FastAPI(title="NovaBak")
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
def login_post(request: Request, username: str = Form(...), password: str = Form(...), mfa_code: str = Form(None), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Incorrect username or password"})
        
    if user.is_mfa_enabled:
        if not mfa_code or not auth.verify_totp(user.mfa_secret, mfa_code):
            return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid MFA Code"})
            
    # Login success
    token = auth.create_access_token(username)
    
    # If MFA not enabled, force setup
    if not user.is_mfa_enabled:
        secret = auth.generate_mfa_secret()
        uri = auth.get_totp_uri(secret, username)
        qr_b64 = auth.generate_qr_code(uri)
        
        response = templates.TemplateResponse("mfa_setup.html", {"request": request, "qr_code": qr_b64, "secret": secret})
        response.set_cookie(key="session_token", value=token, httponly=True)
        return response

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="session_token", value=token, httponly=True)
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
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response

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
            
    return templates.TemplateResponse("index.html", {
        "request": request,
        "config": config,
        "vms": vms,
        "logs": logs,
        "users": users,
        "current_user": user,
        "esxi_hosts": esxi_hosts
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
    backup_timeout_mins: int = Form(15),
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
    
    config.storage_type = storage_type
    config.nfs_path = nfs_path
    config.s3_endpoint = s3_endpoint
    config.s3_access_key = s3_access_key
    if s3_secret_key:
        config.s3_secret_key = s3_secret_key
    config.s3_bucket = s3_bucket
    config.s3_region = s3_region
    
    db.commit()
    return RedirectResponse(url="/", status_code=303)

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
    new_host = ESXiHost(name=name, host_ip=host_ip, username=username, password=password)
    db.add(new_host)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_esxi_host")
def delete_esxi_host(request: Request, host_id: int = Form(...), db: Session = Depends(get_db)):
    require_auth(request)
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if host:
        db.delete(host)
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/fetch_vms")
def fetch_vms(request: Request, esxi_host_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        require_auth(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
        
    host = db.query(ESXiHost).filter(ESXiHost.id == esxi_host_id).first()
    if not host:
        return {"error": "Invalid ESXi host selected."}
        
    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        return {"error": "Could not connect to ESXi."}
        
    vm_list = esxi_handler.get_all_vms(si)
    esxi_handler.Disconnect(si)
    
    # Update DB
    existing_vms = {vm.vm_name: vm for vm in db.query(VM).all()}
    
    for vm_data in vm_list:
        if vm_data['name'] not in existing_vms:
            new_vm = VM(
                vm_name=vm_data['name'], 
                esxi_host_id=host.id,
                cpu_count=vm_data.get('cpu_count', 0),
                memory_mb=vm_data.get('memory_mb', 0),
                storage_gb=vm_data.get('storage_gb', 0.0),
                power_state=vm_data.get('power_state', 'Unknown')
            )
            db.add(new_vm)
        else:
            vm = existing_vms[vm_data['name']]
            vm.cpu_count = vm_data.get('cpu_count', 0)
            vm.memory_mb = vm_data.get('memory_mb', 0)
            vm.storage_gb = vm_data.get('storage_gb', 0.0)
            vm.power_state = vm_data.get('power_state', 'Unknown')
            
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/toggle_vm")
def toggle_vm(request: Request, vm_id: int = Form(...), is_selected: bool = Form(False), db: Session = Depends(get_db)):
    require_auth(request)
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        vm.is_selected = is_selected
        db.commit()
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
    db: Session = Depends(get_db)
):
    require_auth(request)
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        vm.schedule_hour = schedule_hour
        vm.schedule_minute = schedule_minute
        vm.retention_count = retention_count
        vm.is_job_active = is_job_active
        vm.power_off_for_backup = power_off_for_backup
        db.commit()
        # The external worker_daemon.py will auto-detect this schedule change via md5 hash polling.
    return RedirectResponse(url="/", status_code=303)


@app.post("/run_now")
def run_now(request: Request, vm_id: int = Form(...), db: Session = Depends(get_db)):
    require_auth(request)
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        vm.current_action = "PENDING_RUN"
        db.commit()
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
        current_action="Initializing..."
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
    
@app.post("/add_user")
def add_user(request: Request, new_username: str = Form(...), new_password: str = Form(...), db: Session = Depends(get_db)):
    require_auth(request)
    existing = db.query(User).filter(User.username == new_username).first()
    if not existing:
        hashed = auth.get_password_hash(new_password)
        new_user = User(username=new_username, hashed_password=hashed)
        db.add(new_user)
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_user")
def delete_user(request: Request, user_id: int = Form(...), db: Session = Depends(get_db)):
    current_username = require_auth(request)
    user_to_delete = db.query(User).filter(User.id == user_id).first()
    
    if user_to_delete and user_to_delete.username != current_username:
        db.delete(user_to_delete)
        db.commit()
        
    return RedirectResponse(url="/", status_code=303)

@app.post("/stop_job")
def stop_job(request: Request, vm_id: int = Form(...), db: Session = Depends(get_db)):
    require_auth(request)
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        vm.current_action = "PENDING_STOP"
        db.commit()
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
    return RedirectResponse(url="/", status_code=303)

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
        
        import uvicorn.config
        l_config = uvicorn.config.LOGGING_CONFIG
        l_config["formatters"]["access"]["fmt"] = "[%(asctime)s] %(levelprefix)s %(message)s"
        l_config["formatters"]["default"]["fmt"] = "[%(asctime)s] %(levelprefix)s %(message)s"
        uvicorn.run("main:app", host="0.0.0.0", port=8000, log_config=l_config)
    finally:
        if lock_fh:
            lock_fh.close()
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except:
                pass
