import re

with open('worker.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix import
content = content.replace('from storage_util import get_storage, authenticate_smb', 'from storage_util import get_storage')

# Fix usage
content = re.sub(r'if target\.storage_type == "SMB":\s+authenticate_smb\(target\)', '', content)

with open('worker.py', 'w', encoding='utf-8') as f:
    f.write(content)

with open('api/v1/backup_ops.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Do the same for backup_ops.py if it exists there
if 'authenticate_smb' in content:
    content = content.replace('from storage_util import get_storage, authenticate_smb', 'from storage_util import get_storage')
    content = re.sub(r'if \w+\.storage_type == "SMB":\s+authenticate_smb\(\w+\)', '', content)
    with open('api/v1/backup_ops.py', 'w', encoding='utf-8') as f:
        f.write(content)

print("Fixed")
