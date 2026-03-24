from models import SessionLocal, BackupLog
db = SessionLocal()
logs = db.query(BackupLog).order_by(BackupLog.timestamp.desc()).limit(20).all()
for log in logs:
    print(f"{log.timestamp} | {log.vm_name} | {log.status} | {log.message}")
db.close()
