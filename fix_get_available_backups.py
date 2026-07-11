import re

# 1. Update worker.py
with open('worker.py', 'r', encoding='utf-8') as f:
    worker_content = f.read()

new_get_available_backups = '''def get_available_backups(target):
    """ Scans the target storage and returns a list of available backups. """
    from storage_util import get_storage
    
    backups = []
    if not target:
        return backups
        
    storage = get_storage(target)
    if target.storage_type == "SMB":
        authenticate_smb(target)
        
    try:
        vm_dirs = storage.list_dirs("")
        for vm_name in vm_dirs:
            date_folders = storage.list_dirs(vm_name)
            for date_folder in date_folders:
                rel_date_dir = f"{vm_name}/{date_folder}"
                files = storage.list_files(rel_date_dir)
                
                has_descriptor = any(f.endswith(".ovf") or f.endswith(".vmx") for f in files)
                if has_descriptor:
                    # Find main disk size
                    total_size = sum(storage.get_file_size(f"{rel_date_dir}/{f}") for f in files)
                    backups.append({
                        "vm_name": vm_name,
                        "date": date_folder,
                        "path": rel_date_dir,
                        "size": total_size,
                        "target_id": target.id,
                        "target_name": target.name
                    })
    except Exception as e:
        log_error(f"Failed to scan storage target {target.name}: {e}")
        
    return backups'''

worker_content = re.sub(r'def get_available_backups\(db\):.*?return backups', new_get_available_backups, worker_content, flags=re.DOTALL)
with open('worker.py', 'w', encoding='utf-8') as f:
    f.write(worker_content)


# 2. Update main.py
with open('main.py', 'r', encoding='utf-8') as f:
    main_content = f.read()

new_main_call = '''        targets = db.query(StorageTarget).all()
        backups = []
        for t in targets:
            backups.extend(worker.get_available_backups(t))'''

main_content = re.sub(r'backups = worker\.get_available_backups\(db\)', new_main_call, main_content)
with open('main.py', 'w', encoding='utf-8') as f:
    f.write(main_content)


# 3. Update services/backup_ops.py
with open('services/backup_ops.py', 'r', encoding='utf-8') as f:
    ops_content = f.read()

new_ops_call1 = '''    targets = db.query(StorageTarget).all()
    backups = []
    for t in targets:
        backups.extend(worker.get_available_backups(t))'''

ops_content = re.sub(r'backups = worker\.get_available_backups\(config\)', new_ops_call1, ops_content)

# line 594 is inside a loop over targets already!
# targets = db.query(StorageTarget).all()
# for target in targets:
#    worker.authenticate_smb(target) -> this fails! `authenticate_smb` is not in storage_util. We should remove it.
ops_content = re.sub(r'worker\.authenticate_smb\(target\)', 'pass', ops_content)

with open('services/backup_ops.py', 'w', encoding='utf-8') as f:
    f.write(ops_content)

print("Refactored get_available_backups")
