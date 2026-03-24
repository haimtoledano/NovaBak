import os
import platform

# Base Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR can be overridden via environment variable (useful for Docker volumes)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
BIN_DIR = os.path.join(BASE_DIR, "bin")

# Ensure required directories exist
# (These were created manually by the user, but we keep this check for safety)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(BIN_DIR, exist_ok=True)

# Database path
SQLALCHEMY_DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'backup_system.db')}"


# ovftool Path Resolution
if platform.system() == "Windows":
    OVFTOOL_PATH = os.path.join(BIN_DIR, "ovftool", "ovftool.exe")
else:
    OVFTOOL_PATH = os.path.join(BIN_DIR, "ovftool", "ovftool")
