# NovaBak REST API v1

Base URL: `https://backups-us-east-1.stackblaze.cloud/api/v1`

Interactive docs: `https://backups-us-east-1.stackblaze.cloud/api/v1/docs`

## Authentication

NovaBak supports two token types:

| Type | Prefix | Lifetime | Use case |
|---|---|---|---|
| **JWT** | (none) | 7 days | Interactive scripts, creating API keys |
| **API key** | `nbak_` | Until revoked | Automation, CI/CD |

Pass either as: `Authorization: Bearer <token>`

### 1. Create a JWT (login)

```bash
curl -sk -X POST https://backups-us-east-1.stackblaze.cloud/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_PASSWORD","mfa_code":"123456"}'
```

Response:
```json
{"access_token":"eyJ...","token_type":"bearer"}
```

### 2. Create a long-lived API key

**Option A — with JWT:**
```bash
curl -sk -X POST https://backups-us-east-1.stackblaze.cloud/api/v1/auth/api-keys \
  -H "Authorization: Bearer eyJ..." \
  -H "Content-Type: application/json" \
  -d '{"name":"my-automation-key"}'
```

**Option B — with username/password in one call:**
```bash
curl -sk -X POST https://backups-us-east-1.stackblaze.cloud/api/v1/auth/api-keys \
  -H "Content-Type: application/json" \
  -d '{"name":"my-automation-key","username":"admin","password":"YOUR_PASSWORD","mfa_code":"123456"}'
```

Response (key shown once):
```json
{"id":1,"name":"my-automation-key","key":"nbak_...","message":"Store this key securely; it will not be shown again."}
```

### 3. Use the API key

```bash
export NOVABAK_KEY="nbak_..."

curl -sk https://backups-us-east-1.stackblaze.cloud/api/v1/vms \
  -H "Authorization: Bearer $NOVABAK_KEY"
```

## Roles

| Role | Permissions |
|---|---|
| `admin` | Full access including users, config, API keys |
| `operator` | Backups, restores, hosts (read), VM management |
| `viewer` | Read-only |

## Endpoints

### Auth
- `POST /auth/token` — create JWT
- `POST /auth/login` — alias for `/auth/token`
- `POST /auth/api-keys` — create API key
- `GET /auth/api-keys` — list your API keys (admin)
- `DELETE /auth/api-keys/{id}` — revoke key
- `GET /auth/me` — current user info

### Users (admin)
- `GET /users` — list users
- `POST /users` — create user (returns temp password)
- `DELETE /users/{id}` — delete user
- `PATCH /users/{id}/role` — change role
- `POST /users/{id}/reset-password` — reset password
- `POST /users/{id}/reset-mfa` — reset MFA

### Profile
- `PATCH /profile` — update email / notification subscriptions

### Config
- `GET /config` — read all settings
- `PUT /config` — update storage + email settings
- `PUT /config/storage` — update storage only
- `POST /config/storage/test` — test storage connection
- `POST /config/email/test` — send test email

### ESXi Hosts
- `GET /hosts` — list hosts
- `POST /hosts` — register host
- `DELETE /hosts/{id}` — remove host
- `GET /hosts/{id}/datastores` — list datastores
- `POST /hosts/{id}/sync-vms` — discover VMs

### VMs
- `GET /vms` — list all VMs
- `PATCH /vms/{id}` — update schedule/retention/enable
- `POST /vms/{id}/run` — trigger backup now
- `POST /vms/{id}/stop` — stop running backup

### Backups & Restores
- `GET /backups` — list backup versions grouped by VM
- `GET /restores` — list restore jobs
- `POST /restores` — start restore
- `POST /restores/{id}/stop` — cancel restore
- `DELETE /restores/{id}` — delete restore record

### Logs
- `GET /logs/backup?limit=100` — backup history
- `GET /logs/system` — service + worker log tails

### Monitoring
- `GET /jobs/progress` — live backup progress per VM

## Example: configure NFS storage

```bash
curl -sk -X PUT https://backups-us-east-1.stackblaze.cloud/api/v1/config/storage \
  -H "Authorization: Bearer $NOVABAK_KEY" \
  -H "Content-Type: application/json" \
  -d '{"storage_type":"NFS","nfs_path":"/mnt/backups"}'
```

### Backup guardrails (worker settings)

| Field | Default | Description |
|-------|---------|-------------|
| `max_global_backups` | 10 | Max concurrent backups across all hosts |
| `max_backups_per_host` | 2 | Max concurrent backups per ESXi host |
| `datastore_min_free_pct` | 15 | Skip backup if source datastore free % is below this |
| `datastore_headroom_gb` | 10 | Extra free GB required beyond estimated job size |
| `datastore_est_multiplier` | 2.0 | Estimated temp space = VM disk × multiplier (1.0 when VM is powered off) |

```bash
curl -sk -X PUT https://backups-us-east-1.stackblaze.cloud/api/v1/config \
  -H "Authorization: Bearer $NOVABAK_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_backups_per_host":2,"datastore_min_free_pct":15}'
```

## Example: trigger a backup

```bash
# Get VM id
curl -sk https://backups-us-east-1.stackblaze.cloud/api/v1/vms \
  -H "Authorization: Bearer $NOVABAK_KEY"

# Run backup on VM id 5
curl -sk -X POST https://backups-us-east-1.stackblaze.cloud/api/v1/vms/5/run \
  -H "Authorization: Bearer $NOVABAK_KEY"
```

## CLI helper (on server)

```bash
sudo docker compose -f /opt/NovaBak/docker-compose.yml exec -T web \
  python scripts/create_api_key.py --name automation
```
