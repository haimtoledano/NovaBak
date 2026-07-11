import re

# 1. Update api/schemas.py
with open("api/schemas.py", "r", encoding="utf-8") as f:
    schemas_content = f.read()

new_schema = """class ESXiHostUpdate(BaseModel):
    name: Optional[str] = None
    host_ip: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    host_type: Optional[str] = None

class ESXiHostResponse(BaseModel):"""
schemas_content = schemas_content.replace("class ESXiHostResponse(BaseModel):", new_schema)

with open("api/schemas.py", "w", encoding="utf-8") as f:
    f.write(schemas_content)


# 2. Update api/v1/router.py
with open("api/v1/router.py", "r", encoding="utf-8") as f:
    router_content = f.read()

router_imports = "ESXiHostCreate, ESXiHostResponse,"
router_new_imports = "ESXiHostCreate, ESXiHostUpdate, ESXiHostResponse,"
router_content = router_content.replace(router_imports, router_new_imports)

router_new_routes = """@router.post("/hosts/test", response_model=TestResult)
def test_esxi_host(body: dict, user: User = Depends(require_api_role("admin", "operator"))):
    host_ip = body.get("host_ip")
    username = body.get("username")
    password = body.get("password")
    ok, message = backup_ops.test_esxi_connection(host_ip, username, password)
    return TestResult(ok=ok, message=message)

@router.put("/hosts/{host_id}", response_model=ESXiHostResponse)
def update_esxi_host(
    host_id: int, 
    body: ESXiHostUpdate, 
    db: Session = Depends(get_db), 
    user: User = Depends(require_api_role("admin", "operator"))
):
    try:
        return backup_ops.update_esxi_host(db, host_id, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/hosts/{host_id}")"""

router_content = router_content.replace('@router.delete("/hosts/{host_id}")', router_new_routes)

with open("api/v1/router.py", "w", encoding="utf-8") as f:
    f.write(router_content)


# 3. Update services/backup_ops.py
with open("services/backup_ops.py", "r", encoding="utf-8") as f:
    ops_content = f.read()

new_ops = """def test_esxi_connection(host_ip, username, password):
    import esxi_handler
    try:
        si = esxi_handler.connect_esxi(host_ip, username, password)
        if si:
            esxi_handler.Disconnect(si)
            return True, "Connection successful"
        else:
            return False, "Invalid credentials or unreachable"
    except Exception as e:
        return False, str(e)

def update_esxi_host(db: Session, host_id: int, data: dict):
    host = db.query(ESXiHost).filter(ESXiHost.id == host_id).first()
    if not host:
        raise ValueError("Host not found")
    for k, v in data.items():
        if v is not None:
            if k == "password" and v == "":
                continue
            setattr(host, k, v)
    db.commit()
    db.refresh(host)
    return host

def delete_esxi_host(db: Session, host_id: int):"""

ops_content = ops_content.replace('def delete_esxi_host(db: Session, host_id: int):', new_ops)

with open("services/backup_ops.py", "w", encoding="utf-8") as f:
    f.write(ops_content)

print("Backend API updated for ESXi hosts edit & test")
