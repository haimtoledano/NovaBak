import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add storage_targets to the index route
# "users = db.query(User).all()"
# We'll add "storage_targets = db.query(StorageTarget).all()" after it
if 'storage_targets = db.query(StorageTarget).all()' not in content:
    content = content.replace(
        'users = db.query(User).all()',
        'users = db.query(User).all()\n    from models import StorageTarget\n    storage_targets = db.query(StorageTarget).all()'
    )

    content = content.replace(
        '"notify_events": NOTIFY_EVENTS,',
        '"notify_events": NOTIFY_EVENTS,\n        "storage_targets": storage_targets,'
    )

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Modified main.py")
