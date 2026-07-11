import datetime
import sys
import os

import contextvars
import json

request_id_var = contextvars.ContextVar("request_id", default="")
job_id_var = contextvars.ContextVar("job_id", default="")

def get_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _get_log_path():
    """Returns the absolute path to the current process's log file."""
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    os.makedirs(data_dir, exist_ok=True)
    log_file = os.environ.get("LOG_FILE", "service.log")
    return os.path.join(data_dir, log_file)

def log(message, level="INFO", exc_info=None):
    """Prints a timestamped message to stdout AND appends to the log file."""
    req_id = request_id_var.get()
    job_id = job_id_var.get()
    
    # Check if JSON logging is requested
    if os.environ.get("LOG_FORMAT", "").lower() == "json":
        log_entry = {
            "timestamp": get_timestamp(),
            "level": level,
            "message": str(message),
        }
        if req_id:
            log_entry["request_id"] = req_id
        if job_id:
            log_entry["job_id"] = job_id
        if exc_info:
            import traceback
            log_entry["exception"] = traceback.format_exc()
        msg = json.dumps(log_entry)
    else:
        ctx = ""
        if req_id:
            ctx += f"[req:{req_id}]"
        if job_id:
            ctx += f"[job:{job_id}]"
            
        msg = f"[{get_timestamp()}][{level}]{ctx} {message}"
        if exc_info:
            import traceback
            msg += "\n" + traceback.format_exc()

    # Always print to stdout (visible in `docker logs`) — force UTF-8 to avoid cp1252 crashes
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    print(msg, flush=True)
    # Also write to file (visible in Diagnostics Console)
    try:
        with open(_get_log_path(), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass  # Never crash the app because of a logging failure

def log_info(message):
    log(message, "INFO")

def log_warn(message):
    log(message, "WARN")

def log_error(message, exc_info=None):
    log(message, "ERROR", exc_info=exc_info)

def log_critical(message):
    log(message, "CRITICAL")

def log_debug(message):
    log(message, "DEBUG")

def log_audit(db_session, username: str, action: str, details: str = None, ip_address: str = None):
    """Writes an audit log entry to the database."""
    try:
        from models import AuditLog
        new_log = AuditLog(
            username=username,
            action=action,
            details=details,
            ip_address=ip_address
        )
        db_session.add(new_log)
        db_session.commit()
    except Exception as e:
        log_error(f"Failed to write audit log: {e}")
