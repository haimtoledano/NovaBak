import os
import sys
import time
import hashlib
from models import SessionLocal, VM, init_db
import worker
from logger_util import log_info, log_error, log_critical

def get_schedule_hash():
    """ Computes a hash of the current backup schedule for all VMs to detect changes. """
    db = SessionLocal()
    try:
        vms = db.query(VM).filter(VM.is_selected == True).all()
        # Create a string representation of the schedule state
        state_str = "".join([f"{v.id}:{v.schedule_hour}:{v.schedule_minute}:{v.is_job_active}" for v in vms])
        return hashlib.md5(state_str.encode()).hexdigest()
    finally:
        db.close()

def run_daemon():
    init_db()
    pid = os.getpid()
    log_info(f"[PID {pid}] Starting Backup Engine Daemon...")
    
    # Initial scheduler start
    worker.start_scheduler()
    last_hash = get_schedule_hash()
    
    log_info(f"[PID {pid}] Enter polling loop. Monitoring DB for manual triggers and schedule changes.")
    
    while True:
        try:
            # 1. Check for schedule changes
            current_hash = get_schedule_hash()
            if current_hash != last_hash:
                log_info(f"[PID {pid}] Detected schedule changes in DB. Reloading scheduler...")
                worker.start_scheduler()
                last_hash = current_hash

            # 2. Poll for Manual Run Requests ("PENDING_RUN")
            db = SessionLocal()
            pending_runs = db.query(VM).filter(VM.current_action == "PENDING_RUN").all()
            for vm in pending_runs:
                log_info(f"[PID {pid}] Found manual trigger request for VM: {vm.vm_name}")
                vm.current_action = "Queued..."
                db.commit()
                # Submit to worker thread pool
                worker.queue_backup(vm.id)
                
            # 3. Poll for Manual Stop Requests ("PENDING_STOP")
            pending_stops = db.query(VM).filter(VM.current_action == "PENDING_STOP").all()
            for vm in pending_stops:
                log_info(f"[PID {pid}] Found manual stop request for VM: {vm.vm_name}")
                worker.stop_job(vm.id)
                vm.current_action = ""
                vm.progress = 0
                db.commit()
                
            db.close()
            
        except Exception as e:
            log_error(f"{e}")
            
        # Poll every 5 seconds
        time.sleep(5)

if __name__ == "__main__":
    lock_file = "daemon.lock"
    lock_fh = None
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except:
            log_critical("Another instance of Backup Daemon is already running.")
            sys.exit(1)
            
    try:
        lock_fh = open(lock_file, "w")
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
        
        run_daemon()
    finally:
        if lock_fh:
            lock_fh.close()
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except:
                pass

