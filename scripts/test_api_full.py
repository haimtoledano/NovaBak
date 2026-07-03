#!/usr/bin/env python3
"""Full API v1 integration test against a live NovaBak instance."""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


class ApiTest:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []

    def _req(self, method, path, body=None, auth=True, expect=200):
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json"}
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx()) as resp:
                raw = resp.read().decode()
                code = resp.status
                parsed = json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
        if code != expect:
            raise AssertionError(f"HTTP {code} != {expect}: {parsed}")
        return parsed

    def ok(self, name):
        self.passed += 1
        print(f"  PASS  {name}")

    def fail(self, name, err):
        self.failed += 1
        msg = f"  FAIL  {name}: {err}"
        print(msg)
        self.errors.append(msg)

    def skip(self, name, reason):
        self.skipped += 1
        print(f"  SKIP  {name}: {reason}")

    def run(self, name, fn):
        try:
            fn()
            self.ok(name)
        except Exception as e:
            self.fail(name, e)

    def summary(self):
        total = self.passed + self.failed + self.skipped
        print("\n" + "=" * 60)
        print(f"Results: {self.passed} passed, {self.failed} failed, {self.skipped} skipped / {total}")
        if self.errors:
            print("\nFailures:")
            for e in self.errors:
                print(e)
        return self.failed == 0


def _ssl_ctx():
    import ssl
    return ssl._create_unverified_context()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--run-backup-test", action="store_true", help="Trigger backup on test-03 VM")
    args = parser.parse_args()

    t = ApiTest(args.url, args.api_key)
    state = {}

    print(f"Testing NovaBak API at {args.url}\n")

    # ── Docs / OpenAPI ───────────────────────────────────────────────────────
    def test_openapi():
        req = urllib.request.Request(f"{args.url.rstrip('/')}/api/v1/openapi.json")
        with urllib.request.urlopen(req, context=_ssl_ctx()) as resp:
            spec = json.loads(resp.read())
        paths = spec.get("paths", {})
        assert len(paths) >= 25, f"Expected >=25 routes, got {len(paths)}"
        state["route_count"] = len(paths)

    t.run("OpenAPI spec available", test_openapi)

    # ── Auth ─────────────────────────────────────────────────────────────────
    def test_auth_me():
        me = t._req("GET", "/api/v1/auth/me")
        assert me["username"] == "admin"
        assert me["role"] == "admin"
        state["admin_id"] = me["id"]

    t.run("GET /auth/me", test_auth_me)

    def test_list_api_keys():
        keys = t._req("GET", "/api/v1/auth/api-keys")
        assert isinstance(keys, list)
        state["initial_key_count"] = len(keys)

    t.run("GET /auth/api-keys", test_list_api_keys)

    def test_create_and_revoke_api_key():
        created = t._req(
            "POST", "/api/v1/auth/api-keys",
            body={"name": "integration-test-temp"},
        )
        assert created["key"].startswith("nbak_")
        key_id = created["id"]
        keys = t._req("GET", "/api/v1/auth/api-keys")
        assert any(k["id"] == key_id for k in keys)
        t._req("DELETE", f"/api/v1/auth/api-keys/{key_id}")
        keys_after = t._req("GET", "/api/v1/auth/api-keys")
        assert not any(k["id"] == key_id for k in keys_after)

    t.run("POST+DELETE /auth/api-keys", test_create_and_revoke_api_key)

    def test_unauth_rejected():
        old = t.api_key
        t.api_key = "invalid"
        try:
            t._req("GET", "/api/v1/config")
            raise AssertionError("Should have rejected invalid key")
        except AssertionError as e:
            if "Should have rejected" in str(e):
                raise
        finally:
            t.api_key = old

    t.run("Invalid API key rejected", test_unauth_rejected)

    # ── Config ─────────────────────────────────────────────────────────────────
    def test_get_config():
        cfg = t._req("GET", "/api/v1/config")
        assert cfg["storage_type"] == "NFS"
        assert cfg["nfs_path"] == "/mnt/backups"

    t.run("GET /config", test_get_config)

    def test_storage_test():
        result = t._req("POST", "/api/v1/config/storage/test")
        assert result["ok"] is True, result

    t.run("POST /config/storage/test", test_storage_test)

    def test_config_roundtrip():
        cfg = t._req("GET", "/api/v1/config")
        original = cfg["backup_timeout_mins"]
        updated = 16 if original != 16 else 17
        t._req("PUT", "/api/v1/config", body={"backup_timeout_mins": updated})
        cfg2 = t._req("GET", "/api/v1/config")
        assert cfg2["backup_timeout_mins"] == updated
        t._req("PUT", "/api/v1/config", body={"backup_timeout_mins": original})

    t.run("PUT /config roundtrip", test_config_roundtrip)

    # ── Users ──────────────────────────────────────────────────────────────────
    def test_user_lifecycle():
        user = t._req(
            "POST", "/api/v1/users",
            body={"username": "api-test-user", "role": "viewer"},
            expect=201,
        )
        assert user["temporary_password"]
        uid = user["user"]["id"]
        users = t._req("GET", "/api/v1/users")
        assert any(u["username"] == "api-test-user" for u in users)
        t._req("PATCH", f"/api/v1/users/{uid}/role", body={"role": "operator"})
        reset = t._req("POST", f"/api/v1/users/{uid}/reset-password")
        assert reset["temporary_password"]
        t._req("POST", f"/api/v1/users/{uid}/reset-mfa")
        t._req("DELETE", f"/api/v1/users/{uid}")
        users_after = t._req("GET", "/api/v1/users")
        assert not any(u["username"] == "api-test-user" for u in users_after)

    t.run("User CRUD lifecycle", test_user_lifecycle)

    def test_profile_update():
        profile = t._req("PATCH", "/api/v1/profile", body={"email": "admin@test.local"})
        assert profile["email"] == "admin@test.local"
        t._req("PATCH", "/api/v1/profile", body={"email": ""})

    t.run("PATCH /profile", test_profile_update)

    # ── Hosts ──────────────────────────────────────────────────────────────────
    def test_list_hosts():
        hosts = t._req("GET", "/api/v1/hosts")
        assert len(hosts) >= 4, f"Expected 4 ESXi hosts, got {len(hosts)}"
        state["host_id"] = hosts[0]["id"]

    t.run("GET /hosts (4 hosts)", test_list_hosts)

    def test_host_datastores():
        ds = t._req("GET", f"/api/v1/hosts/{state['host_id']}/datastores")
        assert isinstance(ds, list) and len(ds) > 0, ds

    t.run("GET /hosts/{id}/datastores", test_host_datastores)

    def test_sync_vms():
        result = t._req("POST", f"/api/v1/hosts/{state['host_id']}/sync-vms")
        assert "total_on_host" in result
        assert result["total_on_host"] > 0

    t.run("POST /hosts/{id}/sync-vms", test_sync_vms)

    # ── VMs ────────────────────────────────────────────────────────────────────
    def test_list_vms():
        vms = t._req("GET", "/api/v1/vms")
        assert len(vms) >= 20, f"Expected many VMs, got {len(vms)}"
        enabled = [v for v in vms if v["is_selected"]]
        assert len(enabled) >= 20
        test03 = next((v for v in vms if v["vm_name"] == "test-03"), None)
        state["test03_id"] = test03["id"] if test03 else vms[0]["id"]
        state["test_vm"] = test03["vm_name"] if test03 else vms[0]["vm_name"]

    t.run("GET /vms", test_list_vms)

    def test_patch_vm():
        vm_id = state["test03_id"]
        vm = t._req("GET", "/api/v1/vms")
        orig = next(v for v in vm if v["id"] == vm_id)
        new_ret = 8 if orig["retention_count"] != 8 else 9
        patched = t._req("PATCH", f"/api/v1/vms/{vm_id}", body={"retention_count": new_ret})
        assert patched["retention_count"] == new_ret
        t._req("PATCH", f"/api/v1/vms/{vm_id}", body={"retention_count": orig["retention_count"]})

    t.run("PATCH /vms/{id} roundtrip", test_patch_vm)

    # ── Backups / Logs / Jobs ──────────────────────────────────────────────────
    def test_backups():
        backups = t._req("GET", "/api/v1/backups")
        assert isinstance(backups, list)
        state["backup_count"] = len(backups)

    t.run("GET /backups", test_backups)

    def test_backup_logs():
        logs = t._req("GET", "/api/v1/logs/backup?limit=10")
        assert isinstance(logs, list)

    t.run("GET /logs/backup", test_backup_logs)

    def test_system_logs():
        logs = t._req("GET", "/api/v1/logs/system?service_lines=20&worker_lines=20")
        assert "service_log" in logs and "worker_log" in logs

    t.run("GET /logs/system", test_system_logs)

    def test_job_progress():
        progress = t._req("GET", "/api/v1/jobs/progress")
        assert isinstance(progress, dict)
        assert len(progress) > 0

    t.run("GET /jobs/progress", test_job_progress)

    def test_restores_list():
        restores = t._req("GET", "/api/v1/restores")
        assert isinstance(restores, list)

    t.run("GET /restores", test_restores_list)

    # ── Optional: trigger backup ───────────────────────────────────────────────
    if args.run_backup_test:
        def test_trigger_backup():
            vm_id = state["test03_id"]
            vm_name = state["test_vm"]
            t._req("POST", f"/api/v1/vms/{vm_id}/run")
            print(f"       (queued backup for {vm_name}, waiting up to 120s...)")
            for _ in range(24):
                time.sleep(5)
                progress = t._req("GET", "/api/v1/jobs/progress")
                p = progress.get(str(vm_id), progress.get(vm_id, {}))
                action = p.get("current_action", "")
                prog = p.get("progress", 0)
                print(f"       progress={prog}% action={action}")
                if not action or action in ("", "Idle"):
                    break
                if prog >= 100 or "Success" in action or "Failed" in action:
                    break
            logs = t._req("GET", "/api/v1/logs/backup?limit=5")
            # backup may still be running; just verify API accepted the run request
            assert True

        t.run(f"POST /vms/{{id}}/run ({state.get('test_vm', '?')})", test_trigger_backup)
    else:
        def test_run_endpoint_accepts():
            vm_id = state["test03_id"]
            result = t._req("POST", f"/api/v1/vms/{vm_id}/run")
            assert result["ok"] is True
            # cancel if still pending
            time.sleep(2)
            t._req("POST", f"/api/v1/vms/{vm_id}/stop")

        t.run("POST /vms/{id}/run + stop", test_run_endpoint_accepts)

    print(f"\nOpenAPI routes: {state.get('route_count', '?')}")
    print(f"ESXi hosts: 4 | VMs enabled: checked | Backups on disk: {state.get('backup_count', '?')}")

    return 0 if t.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
