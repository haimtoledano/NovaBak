import re

with open("main.py", "r", encoding="utf-8") as f:
    main_content = f.read()

old_update_job = """@app.post("/update_job")
def update_job(
    request: Request, 
    vm_id: int = Form(...), 
    schedule_hour: int = Form(...), 
    schedule_minute: int = Form(...),
    retention_count: int = Form(2),
    is_job_active: bool = Form(False),
    power_off_for_backup: bool = Form(False),
    schedule_frequency: str = Form("daily"),
    schedule_days: str = Form("0,1,2,3,4,5,6"),
    storage_target_id: Optional[int] = Form(None),
    db: Session = Depends(get_db)
):
    require_auth(request)
    try:
        backup_ops.update_vm_job(db, vm_id, {
            "schedule_hour": schedule_hour,
            "schedule_minute": schedule_minute,
            "retention_count": retention_count,
            "is_job_active": is_job_active,
            "power_off_for_backup": power_off_for_backup,
            "schedule_frequency": schedule_frequency,
            "schedule_days": schedule_days,
            "storage_target_id": storage_target_id
        })"""

new_update_job = """@app.post("/update_job")
def update_job(
    request: Request, 
    vm_id: int = Form(...), 
    schedule_hour: int = Form(...), 
    schedule_minute: int = Form(...),
    retention_count: int = Form(2),
    is_job_active: bool = Form(False),
    power_off_for_backup: bool = Form(False),
    schedule_frequency: str = Form("daily"),
    schedule_days: str = Form("0,1,2,3,4,5,6"),
    storage_target_id: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    require_auth(request)
    
    target_id = None
    if storage_target_id and storage_target_id.strip():
        try:
            target_id = int(storage_target_id)
        except ValueError:
            pass

    try:
        backup_ops.update_vm_job(db, vm_id, {
            "schedule_hour": schedule_hour,
            "schedule_minute": schedule_minute,
            "retention_count": retention_count,
            "is_job_active": is_job_active,
            "power_off_for_backup": power_off_for_backup,
            "schedule_frequency": schedule_frequency,
            "schedule_days": schedule_days,
            "storage_target_id": target_id
        })"""

main_content = main_content.replace(old_update_job, new_update_job)

with open("main.py", "w", encoding="utf-8") as f:
    f.write(main_content)

print("Updated /update_job in main.py to handle empty string properly")
