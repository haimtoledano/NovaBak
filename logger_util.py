import datetime
import sys
import os

def get_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _get_log_path():
    """Returns the absolute path to the current process's log file."""
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    os.makedirs(data_dir, exist_ok=True)
    log_file = os.environ.get("LOG_FILE", "service.log")
    return os.path.join(data_dir, log_file)

def log(message, level="INFO"):
    """Prints a timestamped message to stdout AND appends to the log file."""
    msg = f"[{get_timestamp()}][{level}] {message}"
    # Always print to stdout (visible in `docker logs`)
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

def log_error(message):
    log(message, "ERROR")

def log_critical(message):
    log(message, "CRITICAL")

def log_debug(message):
    log(message, "DEBUG")

