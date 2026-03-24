"""
init_db.py — Creates a clean, empty database with a default admin user.

Run this ONCE before first use, or to reset to factory defaults:
    python init_db.py

Default credentials: admin / admin  (change after first login!)
"""
import os
import sqlite3
from models import Base, engine, SessionLocal, User, Config, init_db
from auth import get_password_hash

print("Initializing NovaBak database...")

# Create all tables
init_db()

# Create clean admin user
db = SessionLocal()
try:
    existing = db.query(User).filter(User.username == "admin").first()
    if not existing:
        hashed = get_password_hash("admin")
        admin_user = User(username="admin", hashed_password=hashed, is_mfa_enabled=False)
        db.add(admin_user)
        db.commit()
        print("✅ Created default user: admin / admin")
    else:
        print("ℹ️  Admin user already exists, skipping.")

    # Ensure a config row exists
    from models import Config
    if not db.query(Config).first():
        db.add(Config())
        db.commit()
        print("✅ Created default config row.")

    print("\n✅ Database initialized successfully.")
    print("   DB location:", os.path.abspath(os.path.join("data", "backup_system.db")))
    print("   Login with: admin / admin")
    print("   ⚠️  Change your password after first login!")
finally:
    db.close()
