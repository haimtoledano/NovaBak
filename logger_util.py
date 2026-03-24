import datetime
import sys

def get_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(message, level="INFO"):
    """ Prints a timestamped message and flushes stdout. """
    msg = f"[{get_timestamp()}][{level}] {message}"
    print(msg)
    sys.stdout.flush()

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
