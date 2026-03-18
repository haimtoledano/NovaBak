import uvicorn
from fastapi import FastAPI, Depends, Request, Form, status, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from models import SessionLocal, init_db, Config, VM, BackupLog, User, ESXiHost
import esxi_handler
import worker
from config_env import TEMPLATES_DIR, DATA_DIR
import auth
from fastapi.security import APIKeyCookie
import pyotp

app = FastAPI(title="VM Backup Manager")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
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
    import sqlite3
    import os
    from config_env import DATA_DIR
    
    db_path = os.path.join(DATA_DIR, "backup_system.db")
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.execute('ALTER TABLE vms ADD COLUMN progress INTEGER DEFAULT 0')
            conn.execute('ALTER TABLE vms ADD COLUMN current_action VARCHAR DEFAULT ""')
            conn.commit()
            conn.close()
        except Exception:
            pass
            
    init_db()
    # Create default admin and password = admin if no users exist
    db = SessionLocal()
    if not db.query(User).first():
        hashed = auth.get_password_hash("admin")
        admin = User(username="admin", hashed_password=hashed)
        db.add(admin)
        db.commit()
    db.close()
    
    # Keep a reference to the scheduler so it stays alive
    app.state.scheduler = worker.start_scheduler()

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
    smtp_to_email: str = Form(""),
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
    config.smtp_to_email = smtp_to_email
    
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
            new_vm = VM(vm_name=vm_data['name'], esxi_host_id=host.id)
            db.add(new_vm)
            
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
    db: Session = Depends(get_db)
):
    require_auth(request)
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        vm.schedule_hour = schedule_hour
        vm.schedule_minute = schedule_minute
        vm.retention_count = retention_count
        vm.is_job_active = is_job_active
        db.commit()
        # Reschedule using the global app state
        app.state.scheduler = worker.start_scheduler()
    return RedirectResponse(url="/", status_code=303)

@app.post("/run_now")
def run_now(request: Request, vm_id: int = Form(...), db: Session = Depends(get_db)):
    require_auth(request)
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if vm:
        import threading
        t = threading.Thread(target=worker.perform_backup, args=(vm.id,))
        t.start()
    return RedirectResponse(url="/", status_code=303)

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
        print(f"GET_BACKUPS CRASH: {err}")
        return {"error": f"System Error: {str(e)}"}

@app.post("/restore")
def run_restore(
    request: Request,
    target_esxi_id: int = Form(...),
    source_ova: str = Form(...),
    target_name: str = Form(...),
    datastore: str = Form(...),
    db: Session = Depends(get_db)
):
    require_auth(request)
    config = db.query(Config).first()
    host = db.query(ESXiHost).filter(ESXiHost.id == target_esxi_id).first()
    
    if not config or not host:
        return RedirectResponse(url="/", status_code=303)
        
    # Run the restore asynchronously
    import threading
    worker.authenticate_smb(config)
    t = threading.Thread(target=worker.perform_restore, args=(config, host.host_ip, host.username, host.password, source_ova, target_name, datastore))
    t.start()
    
    # In a real app we'd redirect to a progress page, for now just back to root
    return RedirectResponse(url="/", status_code=303)

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
        worker.stop_job(vm_id)
        import time; time.sleep(1)
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
            "current_action": vm.current_action or ""
        }
    return out

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
