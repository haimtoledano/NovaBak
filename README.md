<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="NovaBak Dashboard" width="800">
</p>

<h1 align="center">NovaBak</h1>
<p align="center"><strong>Enterprise VM Backup & Disaster Recovery for VMware ESXi</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.110-green?logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Docker-ready-blue?logo=docker" alt="Docker">
  <img src="https://img.shields.io/badge/HTTPS-self--signed-brightgreen?logo=letsencrypt" alt="HTTPS">
  <img src="https://img.shields.io/badge/MFA-required-orange?logo=authy" alt="MFA">
  <img src="https://img.shields.io/badge/CBT-incremental-purple" alt="CBT">
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="License">
</p>

---

## Overview

NovaBak is a self-hosted, web-based backup and disaster recovery platform for **VMware ESXi** environments. It runs as a lightweight Python service on Windows Server or inside Docker, and requires zero agents on the VMs being protected.

### Key Features

- **Agentless backup** — uses ESXi's native HTTP datastore access and snapshot API (pyVmomi), no software installed on VMs
- **Incremental Backups (CBT)** ⚡ — VMware Changed Block Tracking for dramatically faster backups after the initial full (typically 70–90% space savings)
- **Advanced Scheduling** — granular cron-like scheduling with Daily, Weekly (specific days), and Monthly (first occurrence) intervals per VM
- **Live or Power-Off backup modes** — backup running VMs safely, or power them off temporarily for faster throughput
- **Hierarchical Disaster Recovery** — browse backups by VM, choose a specific date version, and restore to any host/datastore — including automatic chain assembly for incremental restores
- **Chain-Aware Retention** — smart cleanup that keeps complete backup chains intact (a full backup is never deleted while incremental backups still depend on it)
- **Multiple storage backends** — SMB/CIFS, NFS, or S3-compatible (AWS, Wasabi, MinIO)
- **Multi-Host & vCenter support** — manage multiple ESXi hosts or connect through vCenter for centralized inventory discovery
- **Role-Based Access Control** — Admin / Operator / Viewer with forced MFA (TOTP) for all users
- **Multi-Theme UI** — modern Web UI with Light, Dark, and Cyberpunk visual modes and instant auto-save forms
- **Granular Email Notifications** — SMTP alerts with per-user event subscriptions (e.g. Backup Success, Restore Failure)
- **Encrypted Storage** — AES-256 stream cipher encryption for backups at rest, with zstd compression
- **HTTPS & Security** — auto-generated self-signed TLS certificate on first run, IP allow-listing, rate-limited login

---

## Incremental Backups (CBT) ⚡

NovaBak supports **VMware Changed Block Tracking (CBT)** for fast, space-efficient incremental backups.

### How it works

| Day | Backup Type | Size | What happens |
|-----|------------|------|-------------|
| Monday | **Full** | 500 GB | Complete disk image captured, CBT change IDs recorded |
| Tuesday | Incremental | ~20 GB | Only changed blocks since Monday are saved (`.nb-incr` format) |
| Wednesday | Incremental | ~15 GB | Only changed blocks since Tuesday |
| ... | Incremental | ... | ... |
| Next Monday | **Full** | 500 GB | New chain starts, old chain subject to retention policy |

**Result**: Instead of 500 GB × 7 = 3.5 TB/week, you use ~620 GB — a **~82% reduction** in storage.

### Configuration

1. Set the VM's backup frequency to **Daily** or **Weekly**
2. Change backup type to **Incremental (CBT) ⚡**
3. Choose the day of the week for the **full backup** (e.g. Monday)

> CBT is only available for Daily and Weekly schedules. Monthly schedules automatically use Full backups.

### Restoring from Incrementals

Restoring an incremental backup is **fully automatic** — NovaBak:
1. Identifies the backup chain (full + all incrementals up to the selected date)
2. Assembles a complete VMDK from the chain
3. Uploads and registers the VM on the target ESXi host
4. Cleans up temporary assembly files

No manual intervention required — the user experience is identical to restoring a full backup.

---

## Screenshots

<table>
  <tr>
    <td><img src="docs/screenshots/dashboard.png" alt="Backup Tasks" width="400"></td>
    <td><img src="docs/screenshots/recovery.png" alt="Disaster Recovery" width="400"></td>
  </tr>
  <tr>
    <td align="center"><em>Backup Tasks — per-VM schedule, status & progress</em></td>
    <td align="center"><em>Disaster Recovery — hierarchical VM & version picker</em></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/settings.png" alt="Engine Configuration" width="400"></td>
    <td><img src="docs/screenshots/users.png" alt="User Management" width="400"></td>
  </tr>
  <tr>
    <td align="center"><em>Engine Configuration — hosts, storage & worker settings</em></td>
    <td align="center"><em>User Management — roles, MFA status, admin actions</em></td>
  </tr>
</table>

---

## Quick Start

### Option A — Docker (recommended)

```bash
git clone https://github.com/haimtoledano/NovaBak.git
cd NovaBak

# Initialize a clean database with default admin/admin credentials
python init_db.py

# Start all services
docker-compose up -d
```

Open: **https://localhost:8001**

> Your browser will warn about a self-signed certificate. Click **Advanced → Proceed** to continue.

---

### Option B — Windows Native (Windows Server 2016+)

1. Download the release ZIP (`VMBackupEnterprise_Release.zip`)
2. Extract to a folder (e.g. `C:\VMBackup\`)
3. Right-click **`setup.bat`** → **Run as Administrator**

That's it. The installer will set up Python, install dependencies, and register both services to start automatically on boot.

Open: **https://localhost:8001**

---

## First Login

| | |
|---|---|
| **URL** | `https://localhost:8001` |
| **Username** | `admin` |
| **Password** | `admin` |

> ⚠️ You will be forced to set up **MFA (TOTP)** on first login using Google Authenticator, Microsoft Authenticator, or any TOTP-compatible app.

> ⚠️ After logging in, go to **Users** tab and reset the admin password immediately.

---

## User Roles

| Role | Permissions |
|---|---|
| **Admin** | Full access: settings, backup, restore, user management |
| **Operator** | Run backups and restores, view logs |
| **Viewer** | Read-only dashboard — no action buttons |

All users are **required to set up MFA** on first login.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                      NovaBak                         │
│                                                      │
│  ┌──────────────────┐      ┌──────────────────────┐  │
│  │  Web UI + API    │      │  Worker Daemon        │  │
│  │  (FastAPI)       │      │  (APScheduler)        │  │
│  │  HTTPS :8001     │      │                       │  │
│  │  ┌────────────┐  │      │  ┌────────────────┐   │  │
│  │  │ Auth (MFA) │  │      │  │ Backup Engine  │   │  │
│  │  │ RBAC       │  │      │  │ + CBT Engine   │   │  │
│  │  └────────────┘  │      │  └───────┬────────┘   │  │
│  └────────┬─────────┘      │          │            │  │
│           │                │  ┌───────▼────────┐   │  │
│           │                │  │ Encrypt (AES)  │   │  │
│           │                │  │ Compress (zstd)│   │  │
│           │                │  └───────┬────────┘   │  │
│           │                └──────────┼────────────┘  │
│           └──────────┬───────────────┘               │
│                      │                                │
│              ┌───────▼────────┐                       │
│              │  SQLite DB     │                       │
│              │  (data/)       │                       │
│              └───────┬────────┘                       │
└──────────────────────┼────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
   ┌────▼─────┐  ┌─────▼─────┐  ┌────▼─────┐
   │ ESXi /   │  │ Storage   │  │  SMTP    │
   │ vCenter  │  │ SMB/NFS/S3│  │  Email   │
   └──────────┘  └───────────┘  └──────────┘
```

---

## Configuration

All configuration is managed through the **Settings** tab in the web UI:

| Section | Description |
|---|---|
| **Registered Hosts** | Add/remove ESXi or vCenter hosts (IP, credentials) |
| **Target Storage** | SMB share, NFS export, or S3 bucket for backup files |
| **Worker Parameters** | Parallel threads, API timeout |
| **Email Alerts** | SMTP server, TLS/SSL, recipient address |
| **IP Allow-list** | Restrict web UI access to specific IP addresses |
| **Encryption** | AES-256 at-rest encryption for backup files |

---

## Project Structure

```
NovaBak/
├── main.py              # FastAPI web application & API
├── worker.py            # Backup & restore job execution
├── worker_daemon.py     # APScheduler daemon process
├── backup_engine.py     # ESXi HTTP streaming backup engine
├── backup_engine_cbt.py # CBT incremental backup engine (NBI format)
├── esxi_handler.py      # pyVmomi API wrapper (snapshots, power, inventory)
├── models.py            # SQLAlchemy models + Alembic migrations
├── auth.py              # bcrypt passwords, TOTP MFA, JWT tokens
├── security.py          # AES-256 encryption, secret management
├── ssl_util.py          # Auto self-signed TLS certificate generation
├── config_env.py        # Paths & database URL
├── storage_util.py      # SMB / NFS / S3 abstraction layer
├── services/            # Business logic layer (backup_ops, etc.)
├── api/                 # REST API schemas & routes
├── templates/           # Jinja2 HTML templates
├── alembic/             # Database migration scripts
├── docs/screenshots/    # Screenshots for documentation
├── Dockerfile
├── docker-compose.yml
├── setup.bat            # Windows auto-installer
├── init_db.py           # Clean DB initializer (for fresh deployments)
└── requirements.txt
```

---

## Upgrading

NovaBak includes an automated upgrade script for production deployments:

```powershell
# On the remote server (PowerShell as Admin):
.\upgrade_remote.ps1
```

The script will automatically back up the existing installation, deploy new files, preserve the database and certificates, run database migrations, and restart services.

---

## Security Notes

- The `data/` directory (SQLite database + TLS certificates + logs) is **excluded from Git**
- Run `python init_db.py` to generate a **clean database** with only the default `admin` account
- Change the default password and complete MFA setup immediately after first login
- All API endpoints are protected by session token authentication
- Passwords stored with bcrypt hashing, secrets encrypted with AES-256
- Self-signed TLS expires in 10 years; replace with a CA-signed cert for production use
- IP allow-listing available for restricting access to trusted networks

---

## Requirements

- **Python 3.11+** (Windows native) or **Docker** (cross-platform)
- Network access to ESXi host(s) on port 443
- SMB share / NFS export / S3 bucket for backup storage
- Minimum 2 GB RAM recommended for the backup service

---

## License

MIT © [haimtoledano](https://github.com/haimtoledano)
