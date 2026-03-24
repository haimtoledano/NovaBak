# NovaBak — VM Backup Enterprise

**NovaBak** is a self-hosted VM backup solution for VMware ESXi environments.  
It provides scheduled backups, live progress monitoring, restore, and email alerts — all from a clean web UI.

---

## Quick Start (Docker — Recommended)

### Prerequisites
- Docker + Docker Compose installed

### Run
```bash
git clone https://github.com/YOUR_USERNAME/VMBackup.git
cd VMBackup

# Start both web UI and worker daemon
docker-compose up -d
```

Open your browser at: **http://localhost:8000**  
Default login: `admin` / `admin` ← **Change this after first login**

---

## Quick Start (Windows — Native)

### Prerequisites
- Python 3.11+
- Windows (for SMB storage support)

### Setup
```bat
setup.bat
```

### Initialize a clean database (first run only)
```bash
python init_db.py
```

### Start services
```bat
start_web.bat      # Web UI on port 8000
start_worker.bat   # Background backup scheduler
```

> For automatic startup on boot, run `install_service.ps1` as Administrator.

---

## Configuration

All settings are managed from the **Settings** tab in the Web UI:
- **Storage**: SMB / NFS / S3
- **Email alerts**: SMTP configuration  
- **ESXi hosts**: Add multiple hypervisor hosts
- **Per-VM backups**: Schedule, retention, power-off mode

---

## Storage Notes

- The `data/` directory holds the SQLite database and logs. Mount it as a volume (Docker) or keep it local (Windows).
- **Never commit `data/` to Git** — it may contain credentials.

---

## Docker Volume

The Docker setup uses a named volume `novabak_data` mounted at `/app/data`.  
To back up or inspect the database:

```bash
docker cp novabak_web:/app/data/backup_system.db ./backup_system.db
```

---

## License

© THIS Cyber Security Ltd. All rights reserved.
