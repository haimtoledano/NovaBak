import re

# Update main.py
with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Change worker.get_available_backups(config) to pass db as well if needed
content = content.replace('worker.get_available_backups(config)', 'worker.get_available_backups(db)')

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)

# Update worker.py
with open('worker.py', 'r', encoding='utf-8') as f:
    content = f.read()

new_get_available_backups = '''def get_available_backups(db):
    """ Scans ALL target storages and returns a list of available backups. """
    from models import StorageTarget
    from storage_util import get_storage, authenticate_smb
    
    targets = db.query(StorageTarget).all()
    backups = []
    
    for target in targets:
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

content = re.sub(r'def get_available_backups\(config\):.*?return backups', new_get_available_backups, content, flags=re.DOTALL)

with open('worker.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated get_available_backups")
