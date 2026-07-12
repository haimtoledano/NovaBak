import re

with open("worker.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix authenticate_smb definition and body
old_auth_smb = """def authenticate_smb(config):
    if os.name == 'nt' and config.smb_unc_path:
        user_str = ""
        if config.smb_user:
            user_str = f"{config.smb_domain}\\\\{config.smb_user}" if config.smb_domain else config.smb_user
            
        if user_str:
            log_info(f"Authenticating to SMB share: {config.smb_unc_path} with user {user_str}")
            
        # Disconnect just in case there's a stale connection
        import subprocess
        subprocess.run(["net", "use", config.smb_unc_path, "/delete", "/y"], capture_output=True)
        
        # Connect
        cmd = ["net", "use", config.smb_unc_path]
        smb_pass = SecretManager.decrypt(getattr(config, 'smb_password', ''))
        if smb_pass and user_str:
            cmd.extend([smb_pass, f"/user:{user_str}"])"""

new_auth_smb = """def authenticate_smb(target):
    if os.name == 'nt' and target.smb_unc_path:
        user_str = ""
        if target.smb_user:
            user_str = f"{target.smb_domain}\\\\{target.smb_user}" if target.smb_domain else target.smb_user
            
        if user_str:
            log_info(f"Authenticating to SMB share: {target.smb_unc_path} with user {user_str}")
            
        # Disconnect just in case there's a stale connection
        import subprocess
        subprocess.run(["net", "use", target.smb_unc_path, "/delete", "/y"], capture_output=True)
        
        # Connect
        cmd = ["net", "use", target.smb_unc_path]
        smb_pass = SecretManager.decrypt(getattr(target, 'smb_password', ''))
        if smb_pass and user_str:
            cmd.extend([smb_pass, f"/user:{user_str}"])"""

content = content.replace(old_auth_smb, new_auth_smb)

# Fix perform_backup
old_perform_backup = """    storage = storage_util.get_storage(config)

    # SMB Authentication (only relevant for SMB storage type)
    if config.storage_type == "SMB":
        authenticate_smb(config)"""

new_perform_backup = """    from models import StorageTarget
    target = vm.storage_target
    if not target:
        target = db.query(StorageTarget).filter(StorageTarget.is_default == True).first()
    if not target:
        msg = f"Failed: No storage target configured for {vm.vm_name}"
        log_error(f"[PID {pid}] {msg}")
        db.add(BackupLog(vm_name=vm.vm_name, status="Failed", message=msg))
        vm.current_action = ""
        vm.progress = 0
        vm.last_status = "Failed"
        db.commit()
        db.close()
        _release_and_drain(host_id)
        return

    storage = storage_util.get_storage(target)

    # SMB Authentication (only relevant for SMB storage type)
    if target.storage_type == "SMB":
        authenticate_smb(target)"""

content = content.replace(old_perform_backup, new_perform_backup)

with open("worker.py", "w", encoding="utf-8") as f:
    f.write(content)

print("worker.py fixed")
