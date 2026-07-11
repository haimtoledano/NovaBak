import re

# Fix main.py
with open("main.py", "r", encoding="utf-8") as f:
    main_content = f.read()

old_get_ds = """    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        return {"error": "Could not connect to ESXi host"}"""

new_get_ds = """    from security import SecretManager
    try:
        real_password = SecretManager.decrypt(host.password)
    except Exception:
        real_password = host.password  # Fallback if unencrypted

    si = esxi_handler.connect_esxi(host.host_ip, host.username, real_password)
    if not si:
        return {"error": "Could not connect to ESXi host"}"""

main_content = main_content.replace(old_get_ds, new_get_ds)

with open("main.py", "w", encoding="utf-8") as f:
    f.write(main_content)

# Fix backup_ops.py
with open("services/backup_ops.py", "r", encoding="utf-8") as f:
    ops_content = f.read()

old_update = """    for k, v in data.items():
        if v is not None:
            if k == "password" and v == "":
                continue
            setattr(host, k, v)"""

new_update = """    from security import SecretManager
    for k, v in data.items():
        if v is not None:
            if k == "password":
                if v == "":
                    continue
                v = SecretManager.encrypt(v)
            setattr(host, k, v)"""

ops_content = ops_content.replace(old_update, new_update)

with open("services/backup_ops.py", "w", encoding="utf-8") as f:
    f.write(ops_content)

print("Fixed encryption issues")
