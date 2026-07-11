import os

# Base Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR can be overridden via environment variable (useful for Docker volumes)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Ensure required directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Database path
SQLALCHEMY_DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'backup_system.db')}"

import secrets

# JWT Secret Key
SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
if not SECRET_KEY:
    _secret_file = os.path.join(DATA_DIR, ".jwt_secret")
    if os.path.exists(_secret_file):
        with open(_secret_file, "r") as f:
            SECRET_KEY = f.read().strip()
    else:
        SECRET_KEY = secrets.token_urlsafe(32)
        try:
            with open(_secret_file, "w") as f:
                f.write(SECRET_KEY)
        except OSError:
            pass
