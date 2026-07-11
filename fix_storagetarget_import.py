import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('from models import SessionLocal, init_db, Config, VM, BackupLog, User, ESXiHost, RestoreJob', 
                          'from models import SessionLocal, init_db, Config, VM, BackupLog, User, ESXiHost, RestoreJob, StorageTarget')

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)

with open('services/backup_ops.py', 'r', encoding='utf-8') as f:
    ops_content = f.read()

ops_content = ops_content.replace('from models import Config, VM, ESXiHost, BackupLog, RestoreJob',
                                  'from models import Config, VM, ESXiHost, BackupLog, RestoreJob, StorageTarget')

with open('services/backup_ops.py', 'w', encoding='utf-8') as f:
    f.write(ops_content)

print("Imports updated")
