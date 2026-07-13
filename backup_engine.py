"""
backup_engine.py — Snapshot + Datastore HTTP Backup Engine (v2)
Uses the same approach as ghettoVCB: snapshot → download VMDKs via
ESXi's built-in HTTP file server → remove snapshot.
No ExportVm, no HttpNfcLease, no zombie tasks.
"""

import os
import re
import ssl
import time
import datetime
import requests
import hashlib
from pyVmomi import vim
from urllib.parse import quote as url_quote
from logger_util import log_info, log_warn, log_error

# Disable SSL warnings for ESXi self-signed certs
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning)

CHUNK_SIZE = 1024 * 1024  # 1MB chunks for download


# ---------------------------------------------------------------------------
#  Helper: Get VM object by name
# ---------------------------------------------------------------------------
def _get_vm(si, vm_name):
    """Finds a VM by inventory path (ESXi standalone) or by iterating."""
    content = si.RetrieveContent()

    # Try inventory path first (fast, ESXi standalone)
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    if vm:
        return vm

    # Fallback: iterate all VMs
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True)
    for child in container.view:
        if child.name == vm_name:
            container.Destroy()
            return child
    container.Destroy()
    return None


# ---------------------------------------------------------------------------
#  Helper: Parse "[datastore] path/to/file.vmdk" → (datastore, path)
# ---------------------------------------------------------------------------
def _parse_datastore_path(ds_path):
    """Parses a VMware datastore path like '[datastore1] vm/disk.vmdk'
    into (datastore_name, relative_path)."""
    match = re.match(r'\[([^\]]+)\]\s*(.*)', ds_path)
    if match:
        return match.group(1), match.group(2)
    return None, ds_path


# ---------------------------------------------------------------------------
#  Helper: Build auth cookies from SOAP session
# ---------------------------------------------------------------------------
def _get_session_cookies(si):
    """Extracts the session cookie from the pyVmomi connection."""
    cookie_str = si._stub.cookie
    if not cookie_str:
        return {}
    parts = cookie_str.split(';')
    if '=' in parts[0]:
        name, value = parts[0].split('=', 1)
        return {name.strip(): value.strip().strip('"')}
    return {}


# ---------------------------------------------------------------------------
#  Helper: Get ESXi host IP from the service instance
# ---------------------------------------------------------------------------
def _get_host_ip(si):
    """Extracts the ESXi host IP from the SOAP stub."""
    try:
        from urllib.parse import urlparse
        url = si._stub.soapStub.safeGetWsdlUrl()
        return urlparse(url).hostname
    except Exception:
        pass
    try:
        return si._stub.host
    except Exception:
        return None


# ---------------------------------------------------------------------------
#  Download VMDK via Datastore HTTP
# ---------------------------------------------------------------------------
def _download_file_http(si, datastore_name, file_path, storage, dest_rel_path, progress_callback=None,
                         progress_base=0, progress_total=100, speed_callback=None, is_cancelled_func=None):
    """
    Downloads a file from ESXi's built-in HTTP file server via StorageProvider.
    """
    host_ip = _get_host_ip(si)
    if not host_ip:
        raise Exception("Cannot determine ESXi host IP")

    cookies = _get_session_cookies(si)

    from esxi_handler import get_datacenter_name
    dc_name = get_datacenter_name(si, datastore_name)

    # URL-encode the file path (but not the slashes)
    encoded_path = '/'.join(url_quote(p, safe='') for p in file_path.split('/'))

    url = (f"https://{host_ip}/folder/{encoded_path}"
           f"?dcPath={url_quote(dc_name, safe='')}&dsName={url_quote(datastore_name, safe='')}")

    log_info(f"[DOWNLOAD] {file_path} from [{datastore_name}] to {dest_rel_path}")

    resp = requests.get(url, stream=True, cookies=cookies, verify=False, timeout=7200)

    if resp.status_code != 200:
        body = resp.text[:500] if resp.text else '(empty)'
        raise Exception(f"HTTP {resp.status_code} downloading {file_path}: {body}")

    # Get total size from Content-Length if available
    total_size = int(resp.headers.get('Content-Length', 0))
    bytes_written = 0
    speed_window_bytes = 0
    speed_window_start = time.time()
    hasher = hashlib.sha256()

    # Initialize encryption and compression from config
    from security import SecretManager
    from models import SessionLocal
    from models import Config
    import struct

    encryptor = None
    encryption_iv = None
    compressor = None
    compression_level = 0

    db = SessionLocal()
    try:
        config = db.query(Config).first()
        if config and config.encryption_key:
            encryption_iv = os.urandom(16)
            encryptor, _ = SecretManager.get_stream_cipher(config.encryption_key, encryption_iv)
        if config and getattr(config, 'perf_compression_level', 0) > 0:
            compression_level = config.perf_compression_level
            try:
                import zstandard
                compressor = zstandard.ZstdCompressor(level=compression_level)
                log_info(f"[DOWNLOAD] Compression enabled: zstd level {compression_level}")
            except ImportError:
                log_warn("[DOWNLOAD] zstandard not installed — compression disabled")
                compression_level = 0
    except Exception as e:
        log_warn(f"Failed to check encryption/compression config: {e}")
    finally:
        db.close()

    use_nb01 = bool(encryptor or compressor)

    storage.makedirs(os.path.dirname(dest_rel_path))

    # We use a context manager for the storage writer
    with storage.open_write(dest_rel_path) as f:
        # Write NB01 header if encryption or compression is active
        if use_nb01:
            flags = 0
            if encryptor: flags |= 0x01  # bit 0 = encrypted
            if compressor: flags |= 0x02  # bit 1 = compressed
            comp_algo = 1 if compressor else 0  # 1 = zstd
            iv_bytes = encryption_iv if encryption_iv else b'\x00' * 16
            header = b'NB01' + struct.pack('BBBB', flags, comp_algo, compression_level, 0) + iv_bytes
            f.write(header)  # 24 bytes total
            bytes_written += 24
            
        # Optimization for local files (sparse support) only if not encrypting/compressing
        is_local = hasattr(storage, 'base_path')
        can_sparse = is_local and not encryptor and not compressor
        zero_chunk = b'\x00' * CHUNK_SIZE if can_sparse else None

        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if is_cancelled_func and is_cancelled_func():
                raise Exception("Backup cancelled by user")
            if chunk:
                # Update hash (hash the original raw chunk for integrity!)
                hasher.update(chunk)
                
                write_chunk = chunk

                # Compress if needed (before encryption!)
                if compressor:
                    write_chunk = compressor.compress(write_chunk)
                    # Frame: [4 bytes compressed_size][compressed_data]
                    write_chunk = struct.pack('<I', len(write_chunk)) + write_chunk

                # Encrypt if needed (after compression!)
                if encryptor:
                    write_chunk = encryptor.update(write_chunk)
                
                # Thin provisioning stream optimization (only for raw local files)
                if can_sparse and len(chunk) == CHUNK_SIZE and chunk == zero_chunk:
                    if hasattr(f, 'seek'):
                        f.seek(CHUNK_SIZE, os.SEEK_CUR)
                        bytes_written += CHUNK_SIZE
                    else:
                        f.write(write_chunk)
                        bytes_written += len(write_chunk)
                elif can_sparse and not chunk.strip(b'\x00'):
                    if hasattr(f, 'seek'):
                        f.seek(len(chunk), os.SEEK_CUR)
                        bytes_written += len(chunk)
                    else:
                        f.write(write_chunk)
                        bytes_written += len(write_chunk)
                else:
                    f.write(write_chunk)
                    bytes_written += len(write_chunk)
                    
                # Update progress and speed every chunk
                now = time.time()
                speed_window_bytes += len(chunk)
                elapsed_window = now - speed_window_start
                if elapsed_window >= 2.0:  # report speed every 2s
                    speed_mbps = (speed_window_bytes / (1024 * 1024)) / elapsed_window
                    if speed_callback:
                        speed_callback(round(speed_mbps, 1))
                    speed_window_bytes = 0
                    speed_window_start = now

                if total_size > 0 and progress_callback:
                    file_pct = (bytes_written * 100) / total_size
                    overall_pct = progress_base + (file_pct * progress_total / 100)
                    progress_callback(min(int(overall_pct), 99))
                    
        # Force file boundaries if supported
        if is_local and bytes_written > 0 and hasattr(f, 'truncate'):
            try:
                f.truncate(bytes_written)
            except Exception as e:
                log_warn(f"[DOWNLOAD] Warning: Truncate skipped: {e}")

    size_mb = bytes_written / (1024 * 1024)
    final_hash = hasher.hexdigest()
    log_info(f"[DOWNLOAD] Complete: {size_mb:.1f} MB processed for {os.path.basename(dest_rel_path)}. SHA-256: {final_hash}")

    return bytes_written, final_hash


# ---------------------------------------------------------------------------
#  Preflight: Disconnect Removable Devices
# ---------------------------------------------------------------------------
def _disconnect_removable_devices(si, vm_name):
    """Disconnects CD-ROMs and Floppies to prevent export issues."""
    vm = _get_vm(si, vm_name)
    if not vm:
        return False

    changes = []
    for device in vm.config.hardware.device:
        if isinstance(device, vim.VirtualCdrom):
            needs_change = False
            if isinstance(device.backing, (vim.VirtualCdromIsoBackingInfo,
                                           vim.VirtualCdromAtapiBackingInfo,
                                           vim.VirtualCdromPassthroughBackingInfo)):
                needs_change = True
            elif device.connectable and device.connectable.connected:
                needs_change = True

            if needs_change:
                log_info(f"[PREFLIGHT] Disconnecting CD-ROM on {vm_name}")
                device.backing = vim.VirtualCdromRemoteAtapiBackingInfo(deviceName="")
                device.connectable.connected = False
                device.connectable.startConnected = False
                spec = vim.VirtualDeviceConfigSpec()
                spec.device = device
                spec.operation = vim.VirtualDeviceConfigSpec.Operation.edit
                changes.append(spec)

        elif isinstance(device, vim.VirtualFloppy):
            needs_change = False
            if isinstance(device.backing, (vim.VirtualFloppyImageBackingInfo,
                                           vim.VirtualFloppyDeviceBackingInfo)):
                needs_change = True
            elif device.connectable and device.connectable.connected:
                needs_change = True

            if needs_change:
                log_info(f"[PREFLIGHT] Disconnecting Floppy on {vm_name}")
                device.backing = vim.VirtualFloppyRemoteDeviceBackingInfo(deviceName="")
                device.connectable.connected = False
                device.connectable.startConnected = False
                spec = vim.VirtualDeviceConfigSpec()
                spec.device = device
                spec.operation = vim.VirtualDeviceConfigSpec.Operation.edit
                changes.append(spec)

    if not changes:
        return True

    config_spec = vim.vm.ConfigSpec()
    config_spec.deviceChange = changes

    task = vm.ReconfigVM_Task(spec=config_spec)
    while task.info.state not in [vim.TaskInfo.State.success,
                                  vim.TaskInfo.State.error]:
        time.sleep(2)

    if task.info.state == vim.TaskInfo.State.success:
        log_info(f"[PREFLIGHT] Removable devices disconnected for {vm_name}")
        return True
    else:
        log_error(f"[PREFLIGHT] Device disconnect failed: {task.info.error}")
        return False


# ---------------------------------------------------------------------------
#  Preflight: Consolidation Check & Trigger
# ---------------------------------------------------------------------------
def _handle_consolidation(si, vm_name, timeout_mins=15):
    """Checks if consolidation is needed and triggers it."""
    vm = _get_vm(si, vm_name)
    if not vm:
        return True

    if not getattr(vm.runtime, 'consolidationNeeded', False):
        return True

    log_info(f"[PREFLIGHT] VM {vm_name} needs consolidation. Triggering...")
    try:
        task = vm.ConsolidateVMDisks_Task()
        start = time.time()
        while task.info.state not in [vim.TaskInfo.State.success,
                                      vim.TaskInfo.State.error]:
            if (time.time() - start) > (timeout_mins * 60):
                log_error(f"[PREFLIGHT] Consolidation timeout ({timeout_mins}m) for {vm_name}")
                return False
            time.sleep(5)

        if task.info.state == vim.TaskInfo.State.success:
            log_info(f"[PREFLIGHT] Consolidation completed for {vm_name}")
            return True
        else:
            log_error(f"[PREFLIGHT] Consolidation failed: {task.info.error}")
            return False
    except Exception as e:
        log_error(f"[PREFLIGHT] Consolidation error: {e}")
        return False


# ---------------------------------------------------------------------------
#  Preflight: Remove Stale Snapshots
# ---------------------------------------------------------------------------
def _remove_stale_snapshots(si, vm_name, timeout_mins=10):
    """Removes any VMBACKUP_TEMP_ snapshots."""
    vm = _get_vm(si, vm_name)
    if not vm or not vm.snapshot:
        return True

    def find_backup_snaps(tree):
        out = []
        for s in tree:
            if s.name.startswith("VMBACKUP_TEMP_"):
                out.append(s.snapshot)
            out.extend(find_backup_snaps(s.childSnapshotList))
        return out

    snaps = find_backup_snaps(vm.snapshot.rootSnapshotList)
    for snap in snaps:
        log_info(f"[PREFLIGHT] Removing stale snapshot for {vm_name}...")
        task = snap.RemoveSnapshot_Task(removeChildren=False)
        start = time.time()
        while task.info.state not in [vim.TaskInfo.State.success,
                                      vim.TaskInfo.State.error]:
            if (time.time() - start) > (timeout_mins * 60):
                log_error(f"[PREFLIGHT] Snapshot removal timeout for {vm_name}")
                return False
            time.sleep(2)
        if task.info.state == vim.TaskInfo.State.error:
            log_error(f"[PREFLIGHT] Snapshot removal failed: {task.info.error}")
            return False
    return True


# ---------------------------------------------------------------------------
#  Preflight: Cleanup Stale Temp Directories
# ---------------------------------------------------------------------------
def _cleanup_stale_temp_dirs(si, datastore_name, hours=12):
    """
    Scans a datastore for abandoned _backup_temp_ directories and removes them.
    Only removes folders older than 'hours' to avoid interfering with running jobs.
    """
    try:
        content = si.RetrieveContent()
        datacenter = content.rootFolder.childEntity[0]
        
        # Find datastore object
        ds = next((d for d in datacenter.datastore if d.name == datastore_name), None)
        if not ds: return

        browser = ds.browser
        search_spec = vim.host.DatastoreBrowser.SearchSpec()
        search_spec.matchPattern = ["_backup_temp_*"]
        
        task = browser.SearchDatastore_Task(datastorePath=f"[{datastore_name}]", searchSpec=search_spec)
        
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(1)
            
        if task.info.state == vim.TaskInfo.State.success:
            results = task.info.result
            if results and hasattr(results, 'file'):
                fm = content.fileManager
                for f in results.file:
                    # We can't easily check folder modification time without more SearchSpec details
                    # To be safe, we just log and attempt deletion of any _backup_temp_ folder
                    # that matches our internal naming scheme
                    folder_path = f"[{datastore_name}] {f.path}"
                    log_info(f"[CLEANUP] Found potentially stale temp folder: {folder_path}. Attempting removal...")
                    try:
                        fm.DeleteDatastoreFile_Task(name=folder_path, datacenter=datacenter)
                    except: pass
    except Exception as e:
        log_warn(f"[CLEANUP] Error during temp folder scan on {datastore_name}: {e}")


def _get_datastore_summary(si, ds_name):
    """Returns capacity/free stats for a named datastore on the host."""
    from pyVmomi import vim
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
    try:
        for ds in container.view:
            if ds.summary.name == ds_name:
                cap = ds.summary.capacity or 0
                free = ds.summary.freeSpace or 0
                cap_gb = cap / (1024**3)
                free_gb = free / (1024**3)
                free_pct = (free / cap * 100) if cap else 0
                return {
                    "name": ds_name,
                    "capacity_gb": round(cap_gb, 1),
                    "free_gb": round(free_gb, 1),
                    "free_pct": round(free_pct, 1),
                }
    finally:
        container.Destroy()
    return None


def _vm_disk_gb(vm):
    if getattr(vm, "storage_gb", None) and vm.storage_gb > 0:
        return float(vm.storage_gb)
    total = 0.0
    if hasattr(vm, "layoutEx") and vm.layoutEx and vm.layoutEx.file:
        for f in vm.layoutEx.file:
            if f.type == "diskDescriptor" and getattr(f, "size", None):
                total += (f.size or 0) / (1024**3)
    return max(total, 1.0)


def _check_datastore_capacity(si, vm_name, config):
    """
    Verify source datastore(s) have enough free space for backup.
    Returns (ok: bool, message: str). Prefix [SKIP] on message when backup should be skipped.
    """
    vm = _get_vm(si, vm_name)
    if not vm:
        return False, "VM not found"

    min_pct = getattr(config, "datastore_min_free_pct", None)
    if min_pct is None:
        min_pct = 15
    headroom = getattr(config, "datastore_headroom_gb", None)
    if headroom is None:
        headroom = 10
    multiplier = getattr(config, "datastore_est_multiplier", None)
    if multiplier is None:
        multiplier = 2.0

    power_state = getattr(vm.runtime, "powerState", "poweredOn")
    if power_state == "poweredOff":
        multiplier = 1.0

    disk_gb = _vm_disk_gb(vm)
    need_gb = disk_gb * float(multiplier) + float(headroom)

    ds_names = set()
    if vm.config and vm.config.files and vm.config.files.vmPathName:
        name, _ = _parse_datastore_path(vm.config.files.vmPathName)
        if name:
            ds_names.add(name)
    if hasattr(vm, "layoutEx") and vm.layoutEx and vm.layoutEx.file:
        for f in vm.layoutEx.file:
            if f.type == "diskDescriptor":
                name, _ = _parse_datastore_path(f.name)
                if name:
                    ds_names.add(name)

    if not ds_names:
        return True, "No datastores to check"

    errors = []
    for ds_name in sorted(ds_names):
        summary = _get_datastore_summary(si, ds_name)
        if not summary:
            log_warn(f"[PREFLIGHT] Datastore '{ds_name}' not found for capacity check")
            continue
        if summary["free_pct"] < min_pct:
            errors.append(
                f"Datastore '{ds_name}' {summary['free_pct']}% free "
                f"({summary['free_gb']} GB of {summary['capacity_gb']} GB) — minimum {min_pct}% required"
            )
        if summary["free_gb"] < need_gb:
            errors.append(
                f"Datastore '{ds_name}' has {summary['free_gb']} GB free but ~{need_gb:.0f} GB "
                f"estimated need for {disk_gb:.0f} GB VM (×{multiplier} + {headroom} GB headroom)"
            )

    if errors:
        return False, "[SKIP] " + "; ".join(errors)
    return True, "Datastore capacity OK"

# ===========================================================================
#  MAIN: Preflight Check
# ===========================================================================
def preflight_check(si, vm_name, timeout_mins=15, config=None, **kwargs):
    """
    Runs a comprehensive pre-backup checklist.
    Returns (success: bool, message: str)
    """
    # Attempt cleanup on the VM's datastore(s)
    vm = _get_vm(si, vm_name)
    if vm and vm.config and vm.config.files:
        ds_name, _ = _parse_datastore_path(vm.config.files.vmPathName)
        if ds_name:
            _cleanup_stale_temp_dirs(si, ds_name)

    steps = [
        ("Remove stale snapshots", lambda: _remove_stale_snapshots(si, vm_name, timeout_mins)),
        ("Disconnect removable devices", lambda: _disconnect_removable_devices(si, vm_name)),
        ("Handle consolidation", lambda: _handle_consolidation(si, vm_name, timeout_mins)),
    ]
    if config is not None:
        steps.insert(0, ("Check datastore free space", lambda: _check_datastore_capacity(si, vm_name, config)))

    for name, func in steps:
        log_info(f"[PREFLIGHT] Step: {name}...")
        try:
            result = func()
            if isinstance(result, tuple):
                ok, msg = result
                if not ok:
                    return False, msg
            elif not result:
                return False, f"Preflight failed at: {name}"
        except Exception as e:
            return False, f"Preflight error at {name}: {e}"

    return True, "All preflight checks passed"


# ===========================================================================
#  MAIN: Create Snapshot
# ===========================================================================
def _create_backup_snapshot(si, vm_name, timeout_mins=10):
    """Creates a crash-consistent snapshot for backup. Returns snapshot object or None."""
    vm = _get_vm(si, vm_name)
    if not vm:
        return None, "VM not found"

    snap_name = f"VMBACKUP_TEMP_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    log_info(f"[SNAPSHOT] Creating {snap_name} for {vm_name}...")

    try:
        task = vm.CreateSnapshot_Task(
            name=snap_name,
            description="Temporary snapshot for automated backup",
            memory=False,
            quiesce=False
        )

        start = time.time()
        while task.info.state not in [vim.TaskInfo.State.success,
                                      vim.TaskInfo.State.error]:
            if (time.time() - start) > (timeout_mins * 60):
                return None, f"Snapshot creation timeout ({timeout_mins}m)"
            time.sleep(2)

        if task.info.state == vim.TaskInfo.State.success:
            log_info(f"[SNAPSHOT] Created successfully: {snap_name}")
            # Find the snapshot object
            vm = _get_vm(si, vm_name)  # Refresh
            if vm.snapshot:
                for s in vm.snapshot.rootSnapshotList:
                    if s.name == snap_name:
                        return s.snapshot, snap_name
            return True, snap_name
        else:
            return None, f"Snapshot failed: {task.info.error}"

    except Exception as e:
        return None, f"Snapshot error: {e}"


# ===========================================================================
#  MAIN: Remove Backup Snapshot
# ===========================================================================
def _remove_backup_snapshot(si, vm_name, snap_name, timeout_mins=60):
    """Removes the backup snapshot by name."""
    vm = _get_vm(si, vm_name)
    if not vm or not vm.snapshot:
        return True

    def find_snap(tree, name):
        for s in tree:
            if s.name == name:
                return s.snapshot
            found = find_snap(s.childSnapshotList, name)
            if found:
                return found
        return None

    snap = find_snap(vm.snapshot.rootSnapshotList, snap_name)
    if not snap:
        log_info(f"[SNAPSHOT] Snapshot {snap_name} not found (already removed?)")
        return True

    log_info(f"[SNAPSHOT] Removing {snap_name} for {vm_name}...")
    task = snap.RemoveSnapshot_Task(removeChildren=False)
    start = time.time()
    while task.info.state not in [vim.TaskInfo.State.success,
                                  vim.TaskInfo.State.error]:
        if (time.time() - start) > (timeout_mins * 60):
            log_error(f"[SNAPSHOT] Removal timeout ({timeout_mins}m)")
            return False
        time.sleep(3)

    if task.info.state == vim.TaskInfo.State.success:
        log_info(f"[SNAPSHOT] Removed successfully: {snap_name}")
        return True
    else:
        log_error(f"[SNAPSHOT] Removal failed: {task.info.error}")
        return False


# ---------------------------------------------------------------------------
#  Upload file to ESXi via Datastore HTTP
# ---------------------------------------------------------------------------
def _upload_file_http(si, datastore_name, dest_rel_path, storage, source_rel_path, is_cancelled_func=None, progress_callback=None, base_pct=0, max_pct=100):
    """
    Uploads a file to ESXi's HTTP file server from StorageProvider.
    """
    host_ip = _get_host_ip(si)
    cookies = _get_session_cookies(si)

    from esxi_handler import get_datacenter_name
    dc_name = get_datacenter_name(si, datastore_name)

    encoded_path = '/'.join(url_quote(p, safe='') for p in dest_rel_path.split('/'))
    url = (f"https://{host_ip}/folder/{encoded_path}"
           f"?dcPath={url_quote(dc_name, safe='')}&dsName={url_quote(datastore_name, safe='')}")

    log_info(f"[UPLOAD] {source_rel_path} to [{datastore_name}] {dest_rel_path}")

    # Check encryption config
    from security import SecretManager
    from models import SessionLocal
    from models import Config
    import struct
    
    db = SessionLocal()
    encryption_key = None
    try:
        config = db.query(Config).first()
        if config:
            encryption_key = config.encryption_key
    finally:
        db.close()

    with storage.open_read(source_rel_path) as f:
        f_size = storage.get_size(source_rel_path)
        decryptor = None
        is_compressed = False
        
        # Check header: NB01 (new) or ENC1 (legacy) or raw
        header = f.read(4)
        if header == b'NB01':
            # New unified header: 4 bytes magic + 4 bytes flags + 16 bytes IV = 24 bytes
            meta = f.read(4)
            flags, comp_algo, comp_level, _reserved = struct.unpack('BBBB', meta)
            iv = f.read(16)
            f_size -= 24
            
            if flags & 0x01:  # encrypted
                if not encryption_key:
                    raise Exception("File is encrypted but no encryption key is configured!")
                _, decryptor = SecretManager.get_stream_cipher(encryption_key, iv)
            if flags & 0x02:  # compressed
                is_compressed = True
                log_info(f"[UPLOAD] File is compressed (zstd level {comp_level})")
                
        elif header == b'ENC1':
            # Legacy encryption-only header
            if not encryption_key:
                raise Exception("File is encrypted but no encryption key is configured!")
            iv = f.read(16)
            _, decryptor = SecretManager.get_stream_cipher(encryption_key, iv)
            f_size -= 20
        else:
            # Not encrypted/compressed, rewind if local or handle S3 gracefully
            if hasattr(f, 'seek'):
                f.seek(0)

        if is_compressed:
            # For compressed files, we need a special wrapper that reads framed chunks,
            # decrypts, then decompresses, producing original-size output for ESXi upload.
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            
            class DecompressStreamWrapper:
                """Reads compressed+encrypted backup and produces raw VMDK data for ESXi upload."""
                def __init__(self, stream, decryptor, dctx, is_cancelled_func):
                    self._stream = stream
                    self._decryptor = decryptor
                    self._dctx = dctx
                    self._is_cancelled = is_cancelled_func
                    self._buffer = b''
                    self._eof = False
                    self._read_so_far = 0
                    self._last_pct = -1

                def _read_exact(self, n):
                    """Read exactly n bytes from underlying stream."""
                    data = b''
                    while len(data) < n:
                        chunk = self._stream.read(n - len(data))
                        if not chunk:
                            return data  # EOF
                        if self._decryptor:
                            chunk = self._decryptor.update(chunk)
                        data += chunk
                    return data

                def _fill_buffer(self):
                    """Read one compressed frame and decompress it."""
                    # Read 4-byte frame length
                    size_data = self._read_exact(4)
                    if len(size_data) < 4:
                        self._eof = True
                        return
                    compressed_size = struct.unpack('<I', size_data)[0]
                    if compressed_size == 0:
                        self._eof = True
                        return
                    # Read compressed data
                    compressed = self._read_exact(compressed_size)
                    if len(compressed) < compressed_size:
                        self._eof = True
                        return
                    # Decompress
                    self._buffer += self._dctx.decompress(compressed)

                def read(self, size=-1):
                    if self._is_cancelled and self._is_cancelled():
                        raise Exception("CancellationRequested")
                    
                    while not self._eof and (size < 0 or len(self._buffer) < size):
                        self._fill_buffer()
                    
                    if size < 0:
                        result = self._buffer
                        self._buffer = b''
                    else:
                        result = self._buffer[:size]
                        self._buffer = self._buffer[size:]
                    
                    if result and progress_callback:
                        self._read_so_far += len(result)
                        # We don't know the uncompressed total, use compressed file size as estimate
                        file_pct = min(1.0, self._read_so_far / max(f_size * 2, 1))
                        overall_pct = int(base_pct + (max_pct - base_pct) * file_pct)
                        if overall_pct > self._last_pct:
                            progress_callback(min(overall_pct, max_pct - 1))
                            self._last_pct = overall_pct
                    
                    return result

                def __len__(self):
                    # ESXi needs Content-Length but we don't know exact uncompressed size
                    # Use a large estimate; requests will use chunked transfer
                    return 0  # Will trigger chunked transfer

            wrapped_stream = DecompressStreamWrapper(f, decryptor, dctx, is_cancelled_func)
        else:
            # Non-compressed path (raw or encrypted-only)
            class CancelableStreamWrapper:
                def __init__(self, stream, size, is_cancelled_func, decryptor, prefetched_header=None):
                    self._stream = stream
                    self._size = size
                    self._is_cancelled = is_cancelled_func
                    self._read_so_far = 0
                    self._last_pct = -1
                    self._decryptor = decryptor
                    self._prefetched = prefetched_header

                def read(self, size=-1):
                    if self._is_cancelled and self._is_cancelled():
                        raise Exception("CancellationRequested")
                    
                    chunk = b''
                    if self._prefetched:
                        if size > 0 and len(self._prefetched) > size:
                            chunk = self._prefetched[:size]
                            self._prefetched = self._prefetched[size:]
                            return chunk
                        else:
                            chunk = self._prefetched
                            self._prefetched = None
                            if size > 0:
                                size -= len(chunk)
                    
                    read_chunk = self._stream.read(size)
                    if not read_chunk and not chunk:
                        return b''
                        
                    if self._decryptor and read_chunk:
                        read_chunk = self._decryptor.update(read_chunk)
                        
                    chunk += read_chunk
                    
                    if chunk and self._size > 0 and progress_callback:
                        self._read_so_far += len(chunk)
                        file_pct = self._read_so_far / self._size
                        overall_pct = int(base_pct + (max_pct - base_pct) * file_pct)
                        if overall_pct > self._last_pct:
                            progress_callback(overall_pct)
                            self._last_pct = overall_pct
                    
                    return chunk

                def __len__(self):
                    return self._size
                    
            # Only pass header to wrapper if it wasn't a known magic and stream couldn't seek
            prefetched = header if (header not in [b'ENC1', b'NB01'] and not hasattr(f, 'seek')) else None
            wrapped_stream = CancelableStreamWrapper(f, f_size, is_cancelled_func, decryptor, prefetched)

        try:
            # For compressed files we don't know the uncompressed size, use chunked transfer
            if is_compressed:
                headers = {'Transfer-Encoding': 'chunked'}
            else:
                headers = {'Content-Length': str(f_size)}
            resp = requests.put(url, data=wrapped_stream, cookies=cookies, verify=False, timeout=7200, headers=headers)
            if resp.status_code not in [200, 201]:
                body = resp.text[:500] if resp.text else '(empty)'
                raise Exception(f"HTTP {resp.status_code} uploading {dest_rel_path}: {body}")
        except Exception as e:
            if "CancellationRequested" in str(e):
                raise Exception("Restore cancelled by user")
            raise e

    log_info(f"[UPLOAD] Complete: {dest_rel_path}")
    return True

# ===========================================================================
#  MAIN: Import VM via Datastore HTTP + Register
# ===========================================================================
def import_vm_native(si, storage, source_rel_dir, target_ds, target_name, progress_callback=None, is_cancelled_func=None):
    """
    Restores a VM by uploading files from StorageProvider to ESXi and registering the VMX.
    """
    try:
        log_info(f"[RESTORE] Starting import_vm_native for {target_name} on {target_ds}")
        content = si.RetrieveContent()
        
        # More robust datacenter find
        def find_obj(container, vim_type):
            for obj in container.childEntity:
                if isinstance(obj, vim_type): return obj
                if hasattr(obj, 'childEntity'):
                    res = find_obj(obj, vim_type)
                    if res: return res
            return None
            
        log_info(f"[RESTORE] Resolving datacenter...")
        datacenter = find_obj(content.rootFolder, vim.Datacenter)
        if not datacenter:
            log_warn("[RESTORE] Could not find Datacenter via traversal, falling back to index 0.")
            datacenter = content.rootFolder.childEntity[0]
            
        fm = content.fileManager

        # 1. Create target directory
        res_dir = f"[{target_ds}] {target_name}"
        log_info(f"[RESTORE] Creating target directory: {res_dir}")
        try:
            fm.MakeDirectory(name=res_dir, datacenter=datacenter, createParentDirectories=True)
            log_info(f"[RESTORE] Directory {res_dir} created/verified.")
        except Exception as dm_err:
            log_error(f"[RESTORE] MakeDirectory failed: {dm_err}")
            raise dm_err

        if progress_callback: progress_callback(5)

        # 2. List source files
        files = storage.list_files(source_rel_dir)
        if not files:
            return False, f"No files found in source directory {source_rel_dir}"

        # 3. Separate files (VMX must be uploaded last or we just upload all)
        vmx_file = next((f for f in files if f.endswith('.vmx')), None)
        if not vmx_file:
            return False, f"No VMX file found in {source_rel_dir}"

        total_files = len(files)
        for idx, filename in enumerate(files):
            source_p = f"{source_rel_dir}/{filename}"
            dest_p = f"{target_name}/{filename}"
            
            step_pct_start = 5 + (90 * idx // total_files)
            step_pct_end = 5 + (90 * (idx + 1) // total_files)
            
            if progress_callback: progress_callback(step_pct_start)
            
            log_info(f"[RESTORE] Uploading {filename} ({idx+1}/{total_files})...")
            if is_cancelled_func and is_cancelled_func():
                raise Exception("Restore cancelled by user")
            _upload_file_http(si, target_ds, dest_p, storage, source_p, is_cancelled_func, progress_callback, step_pct_start, step_pct_end)

        if progress_callback: progress_callback(95)

        # 4. Register VM
        from esxi_handler import register_vm
        vmx_rel_on_ds = f"{target_name}/{vmx_file}"
        ok, msg = register_vm(si, target_ds, vmx_rel_on_ds, target_name)
        
        if ok:
            if progress_callback: progress_callback(100)
            return True, f"VM {target_name} restored and registered successfully."
        else:
            return False, f"Registration failed: {msg}"

    except Exception as e:
        log_error(f"[RESTORE] Native restore failed: {e}")
        return False, str(e)

# ===========================================================================
#  MAIN: Export VM - Power-State Aware Backup
# ===========================================================================
def export_vm_native(si, vm_name, storage, dest_rel_dir, progress_callback=None, speed_callback=None, max_retries=3, is_cancelled_func=None, backup_mode="full", previous_change_id=None, **kwargs):
    """
    Power-state-aware backup with optional incremental (CBT) support:

    FULL BACKUP:
      POWERED OFF → Direct pipe (no snapshot, no CopyVirtualDisk)
      POWERED ON  → Snapshot + CopyVirtualDisk (safe)

    INCREMENTAL BACKUP (backup_mode="incremental"):
      Requires CBT enabled + previous_change_id from last backup.
      Snapshot → QueryChangedDiskAreas → download only changed blocks → .nb-incr
      Falls back to full if CBT is unavailable or change_id is stale.
    """
    vm = _get_vm(si, vm_name)
    if not vm:
        return False, f"VM {vm_name} not found", None

    last_error = ""
    snap_name = None
    temp_ds_dir = None

    for attempt in range(1, max_retries + 1):
        log_info(f"[BACKUP] Attempt {attempt}/{max_retries} for {vm_name}")
        if is_cancelled_func and is_cancelled_func():
            return False, "Backup cancelled by user"

        try:
            # --- Step 1: Collect disk info + detect power state ---
            vm = _get_vm(si, vm_name)
            content = si.RetrieveContent()
            datacenter = content.rootFolder.childEntity[0]

            power_state = getattr(vm.runtime, 'powerState', 'poweredOn')
            is_off = (power_state == 'poweredOff')

            # Incremental backup decision
            use_incremental = False
            if backup_mode == "incremental" and previous_change_id:
                if getattr(vm.config, 'changeTrackingEnabled', False):
                    log_info(f"[BACKUP] Incremental backup requested (CBT changeId: {previous_change_id[:20]}...)")
                    use_incremental = True
                else:
                    log_warn(f"[BACKUP] Incremental requested but CBT not enabled — falling back to full")
            elif backup_mode == "incremental":
                log_warn(f"[BACKUP] Incremental requested but no previous changeId — doing full backup")

            if use_incremental:
                log_info(f"[BACKUP] VM power state: {power_state} -> using INCREMENTAL (CBT) method")
            else:
                log_info(f"[BACKUP] VM power state: {power_state} -> using {'DIRECT' if is_off else 'SNAPSHOT+COPY'} method")

            disk_descriptors = []
            if hasattr(vm, 'layoutEx') and vm.layoutEx and vm.layoutEx.file:
                for f in vm.layoutEx.file:
                    if f.type == 'diskDescriptor':
                        ds_name, rel_path = _parse_datastore_path(f.name)
                        if ds_name:
                            disk_descriptors.append({
                                'ds_name': ds_name,
                                'ds_path': f.name,
                                'rel_path': rel_path,
                            })

            if not disk_descriptors:
                raise Exception(f"No disk files found in layoutEx for {vm_name}")

            vmx_ds_name = None
            vmx_rel_path = None
            if vm.config and vm.config.files and vm.config.files.vmPathName:
                vmx_ds_name, vmx_rel_path = _parse_datastore_path(vm.config.files.vmPathName)

            log_info(f"[BACKUP] Found {len(disk_descriptors)} disk(s) for {vm_name}:")
            for d in disk_descriptors:
                log_info(f"  - {d['ds_path']}")

            # ==============================================================
            #  PATH C: INCREMENTAL — CBT changed blocks only
            # ==============================================================
            if use_incremental:
                import backup_engine_cbt as cbt

                if progress_callback: progress_callback(2)
                snap_obj, snap_name = _create_backup_snapshot(si, vm_name)
                if not snap_obj:
                    raise Exception(f"Snapshot creation failed: {snap_name}")
                if progress_callback: progress_callback(5)

                # Get new change IDs from the snapshot
                new_change_ids = cbt.get_snapshot_change_id(vm, snap_obj)
                storage.makedirs(dest_rel_dir)
                files_downloaded = []
                total_bytes_saved = 0
                total_disk_bytes = 0
                total_disks = len(disk_descriptors)

                try:
                    for idx, disk in enumerate(disk_descriptors):
                        disk_basename = os.path.basename(disk['rel_path'])
                        incr_name = disk_basename.replace('.vmdk', '.nb-incr')
                        flat_rel_path = disk['rel_path'].replace('.vmdk', '-flat.vmdk')

                        # Find the matching disk key
                        disk_key = None
                        for device in vm.config.hardware.device:
                            if isinstance(device, vim.vm.device.VirtualDisk):
                                _, dev_path = _parse_datastore_path(device.backing.fileName)
                                if dev_path and os.path.basename(dev_path) == disk_basename:
                                    disk_key = device.key
                                    break

                        if disk_key is None:
                            log_warn(f"[BACKUP] [INCR] Could not find disk key for {disk_basename} — skipping")
                            continue

                        # Query changed blocks
                        try:
                            changed_blocks, disk_size = cbt.query_changed_blocks(
                                vm, snap_obj, disk_key, change_id=previous_change_id
                            )
                        except Exception as cbt_err:
                            log_warn(f"[BACKUP] [INCR] CBT query failed: {cbt_err} — falling back to full")
                            # Clean up and fall back to full
                            _remove_backup_snapshot(si, vm_name, snap_name, timeout_mins=30)
                            snap_name = None
                            use_incremental = False
                            break

                        total_disk_bytes += disk_size
                        changed_bytes = sum(length for _, length in changed_blocks)
                        total_bytes_saved += (disk_size - changed_bytes)
                        pct_changed = (changed_bytes / disk_size * 100) if disk_size > 0 else 0

                        log_info(f"[BACKUP] [INCR] Disk {idx+1}/{total_disks}: {disk_basename} "
                                 f"— {len(changed_blocks)} blocks changed "
                                 f"({changed_bytes / (1024**2):.1f} MB / {disk_size / (1024**3):.1f} GB = {pct_changed:.1f}%)")

                        # Download changed blocks
                        dl_base = 10 + (80 * idx // total_disks)
                        dl_total = 80 // total_disks

                        bytes_written, block_hash = cbt.download_changed_blocks(
                            si, disk['ds_name'], flat_rel_path, changed_blocks,
                            storage, f"{dest_rel_dir}/{incr_name}", disk_size,
                            progress_callback=progress_callback,
                            progress_base=dl_base, progress_total=dl_total,
                            speed_callback=speed_callback,
                            is_cancelled_func=is_cancelled_func
                        )
                        files_downloaded.append((incr_name, block_hash))

                    if not use_incremental:
                        # CBT failed mid-way, restart as full
                        raise Exception("CBT_FALLBACK_TO_FULL")

                except Exception as incr_err:
                    if "CBT_FALLBACK_TO_FULL" in str(incr_err):
                        log_info("[BACKUP] Falling back to full backup after CBT failure")
                        backup_mode = "full"
                        use_incremental = False
                        # Continue to PATH A/B below
                    else:
                        raise

                if use_incremental:
                    # Write backup metadata
                    import json as json_mod
                    savings_pct = (total_bytes_saved / total_disk_bytes * 100) if total_disk_bytes > 0 else 0
                    metadata = {
                        'type': 'incremental',
                        'vm_name': vm_name,
                        'timestamp': datetime.datetime.now().isoformat(),
                        'change_id': previous_change_id,
                        'new_change_ids': new_change_ids,
                        'parent_backup_dir': kwargs.get('parent_backup_dir', ''),
                        'total_disk_bytes': total_disk_bytes,
                        'changed_bytes': total_disk_bytes - total_bytes_saved,
                        'savings_pct': round(savings_pct, 1),
                        'files': {f: h for f, h in files_downloaded},
                    }
                    cbt.write_backup_metadata(storage, dest_rel_dir, metadata)

                    # Remove snapshot
                    if progress_callback: progress_callback(93)
                    _remove_backup_snapshot(si, vm_name, snap_name, timeout_mins=60)
                    snap_name = None

                    # VMX download
                    if progress_callback: progress_callback(96)
                    if vmx_ds_name and vmx_rel_path:
                        vmx_filename = os.path.basename(vmx_rel_path)
                        try:
                            _, vmx_hash = _download_file_http(si, vmx_ds_name, vmx_rel_path, storage, f"{dest_rel_dir}/{vmx_filename}")
                            files_downloaded.append((vmx_filename, vmx_hash))
                        except Exception as e:
                            log_warn(f"[BACKUP] VMX warning: {e}")

                    if progress_callback: progress_callback(100)
                    checksum_data = {f: h for f, h in files_downloaded}
                    checksum_json = json_mod.dumps(checksum_data)
                    changed_mb = (total_disk_bytes - total_bytes_saved) / (1024**2)
                    total_mb = total_disk_bytes / (1024**2)

                    # Return new change IDs for the worker to save
                    result_msg = (f"Incremental backup completed: {len(files_downloaded)} file(s), "
                                  f"{changed_mb:.0f} MB changed of {total_mb:.0f} MB total "
                                  f"({savings_pct:.0f}% saved)")
                    return True, result_msg, checksum_json, new_change_ids, total_disk_bytes - total_bytes_saved, total_disk_bytes

            # ==============================================================
            #  PATH A: POWERED OFF — Direct stream, no snapshot, no copy
            # ==============================================================
            if is_off and not use_incremental:
                if progress_callback: progress_callback(5)
                storage.makedirs(dest_rel_dir)
                files_downloaded = []
                total_disks = len(disk_descriptors)

                for idx, disk in enumerate(disk_descriptors):
                    disk_basename = os.path.basename(disk['rel_path'])
                    flat_basename = disk_basename.replace('.vmdk', '-flat.vmdk')
                    flat_rel_path = disk['rel_path'].replace('.vmdk', '-flat.vmdk')

                    desc_base = 5  + (85 * (idx * 2)     // (total_disks * 2))
                    flat_base = 5  + (85 * (idx * 2 + 1) // (total_disks * 2))
                    flat_end  = 5  + (85 * (idx * 2 + 2) // (total_disks * 2))

                    log_info(f"[BACKUP] [DIRECT] Streaming descriptor ({idx+1}/{total_disks}): {disk_basename}")
                    _, desc_hash = _download_file_http(
                        si, disk['ds_name'], disk['rel_path'], storage, f"{dest_rel_dir}/{disk_basename}",
                        progress_callback=progress_callback, progress_base=desc_base, progress_total=2,
                        speed_callback=speed_callback, is_cancelled_func=is_cancelled_func
                    )
                    files_downloaded.append((disk_basename, desc_hash))

                    try:
                        log_info(f"[BACKUP] [DIRECT] Streaming flat disk ({idx+1}/{total_disks}): {flat_basename}")
                        _, flat_hash = _download_file_http(
                            si, disk['ds_name'], flat_rel_path, storage, f"{dest_rel_dir}/{flat_basename}",
                            progress_callback=progress_callback, progress_base=flat_base,
                            progress_total=flat_end - flat_base, speed_callback=speed_callback,
                            is_cancelled_func=is_cancelled_func
                        )
                        files_downloaded.append((flat_basename, flat_hash))
                    except Exception as flat_err:
                        if 'HTTP 404' in str(flat_err):
                            log_info(f"[BACKUP] [DIRECT] No flat VMDK — monolithic disk detected. {disk_basename} is the full data file.")
                        else:
                            raise

            # ==============================================================
            #  PATH B: POWERED ON — Snapshot + CopyVirtualDisk (safe)
            # ==============================================================
            else:
                # Step B1: Snapshot
                if progress_callback: progress_callback(2)
                snap_obj, snap_name = _create_backup_snapshot(si, vm_name)
                if not snap_obj:
                    raise Exception(f"Snapshot creation failed: {snap_name}")
                if progress_callback: progress_callback(5)

                # Step B2: CopyVirtualDisk to temp dir
                temp_folder = f"_backup_temp_{vm_name}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
                first_ds = disk_descriptors[0]['ds_name']
                temp_ds_dir = f"[{first_ds}] {temp_folder}"
                fm = content.fileManager
                fm.MakeDirectory(name=temp_ds_dir, datacenter=datacenter, createParentDirectories=True)
                log_info(f"[BACKUP] Temp directory created: {temp_ds_dir}")

                vdm = content.virtualDiskManager
                total_disks = len(disk_descriptors)
                copied_disks = []

                for idx, disk in enumerate(disk_descriptors):
                    disk_basename = os.path.basename(disk['rel_path'])
                    dst_path = f"[{first_ds}] {temp_folder}/{disk_basename}"
                    copy_start = 5 + (40 * idx // total_disks)
                    copy_end   = 5 + (40 * (idx + 1) // total_disks)
                    if progress_callback: progress_callback(copy_start)

                    log_info(f"[BACKUP] Copying disk {idx+1}/{total_disks}: {disk_basename}...")
                    spec = vim.VirtualDiskManager.VirtualDiskSpec()
                    spec.diskType = 'thin'
                    spec.adapterType = 'lsiLogic'
                    task = vdm.CopyVirtualDisk_Task(
                        sourceName=disk['ds_path'], sourceDatacenter=datacenter,
                        destName=dst_path, destDatacenter=datacenter,
                        destSpec=spec, force=True
                    )
                    t0 = time.time()
                    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                        if is_cancelled_func and is_cancelled_func():
                            raise Exception("Backup cancelled by user")
                        if time.time() - t0 > 7200:
                            raise Exception(f"Disk copy timeout for {disk_basename}")
                        if task.info.progress and progress_callback:
                            pct = copy_start + (task.info.progress * (copy_end - copy_start) // 100)
                            progress_callback(min(pct, copy_end))
                        time.sleep(5)
                    if task.info.state == vim.TaskInfo.State.error:
                        raise Exception(f"Disk copy failed: {task.info.error}")
                    log_info(f"[BACKUP] Disk copy done: {disk_basename}")
                    copied_disks.append((first_ds, f"{temp_folder}/{disk_basename}"))

                # Step B3: Stream unlocked copies to storage
                if progress_callback: progress_callback(50)
                storage.makedirs(dest_rel_dir)
                files_downloaded = []

                for idx, (ds_name, temp_rel_path) in enumerate(copied_disks):
                    disk_basename = os.path.basename(temp_rel_path)
                    flat_basename = disk_basename.replace('.vmdk', '-flat.vmdk')
                    flat_rel_path = temp_rel_path.replace('.vmdk', '-flat.vmdk')
                    dl_start = 50 + (38 * idx // len(copied_disks))
                    dl_mid   = dl_start + 2
                    dl_end   = 50 + (38 * (idx + 1) // len(copied_disks))

                    _, desc_hash = _download_file_http(
                        si, ds_name, temp_rel_path, storage, f"{dest_rel_dir}/{disk_basename}",
                        progress_callback=progress_callback, progress_base=dl_start, progress_total=2,
                        speed_callback=speed_callback, is_cancelled_func=is_cancelled_func
                    )
                    files_downloaded.append((disk_basename, desc_hash))
                    
                    try:
                        _, flat_hash = _download_file_http(
                            si, ds_name, flat_rel_path, storage, f"{dest_rel_dir}/{flat_basename}",
                            progress_callback=progress_callback, progress_base=dl_mid,
                            progress_total=dl_end - dl_mid, speed_callback=speed_callback,
                            is_cancelled_func=is_cancelled_func
                        )
                        files_downloaded.append((flat_basename, flat_hash))
                    except Exception as flat_err:
                        if 'HTTP 404' in str(flat_err):
                            log_info(f"[BACKUP] No flat VMDK — monolithic disk. {disk_basename} is the full data file.")
                        else:
                            raise

                # Step B4: Cleanup temp dir
                if progress_callback: progress_callback(90)
                try:
                    fm.DeleteDatastoreFile_Task(name=temp_ds_dir, datacenter=datacenter)
                    temp_ds_dir = None
                    log_info("[BACKUP] Temp directory removed.")
                except Exception as e:
                    log_warn(f"[BACKUP] Temp cleanup warning: {e}")

                # Step B5: Remove snapshot
                if progress_callback: progress_callback(93)
                _remove_backup_snapshot(si, vm_name, snap_name, timeout_mins=60)
                snap_name = None

            # ==============================================================
            #  SHARED: VMX config download (both paths)
            # ==============================================================
            if progress_callback: progress_callback(96)
            if vmx_ds_name and vmx_rel_path:
                vmx_filename = os.path.basename(vmx_rel_path)
                try:
                    _, vmx_hash = _download_file_http(si, vmx_ds_name, vmx_rel_path, storage, f"{dest_rel_dir}/{vmx_filename}")
                    files_downloaded.append((vmx_filename, vmx_hash))
                    log_info(f"[BACKUP] VMX saved: {vmx_filename}")
                except Exception as e:
                    log_warn(f"[BACKUP] VMX warning: {e}")

            if progress_callback: progress_callback(100)
            method = "direct" if is_off else "snapshot+copy"
            
            # Create a combined JSON string of checksums to store in DB
            import json
            checksum_data = {f: h for f, h in files_downloaded}
            checksum_json = json.dumps(checksum_data)

            # Extract CBT change IDs if CBT is enabled (for future incremental backups)
            new_change_ids = {}
            if getattr(vm.config, 'changeTrackingEnabled', False):
                try:
                    import backup_engine_cbt as cbt
                    # For full backups we need to get the changeId from the current VM state
                    # The changeId is available on disk backing after any snapshot operation
                    for device in vm.config.hardware.device:
                        if isinstance(device, vim.vm.device.VirtualDisk):
                            backing = device.backing
                            if hasattr(backing, 'changeId') and backing.changeId:
                                new_change_ids[device.key] = backing.changeId
                    if new_change_ids:
                        log_info(f"[BACKUP] CBT change IDs captured for {len(new_change_ids)} disk(s)")
                except Exception as e:
                    log_warn(f"[BACKUP] CBT changeId extraction warning: {e}")

            # Write backup metadata JSON
            try:
                import backup_engine_cbt as cbt
                metadata = {
                    'type': 'full',
                    'vm_name': vm_name,
                    'timestamp': datetime.datetime.now().isoformat(),
                    'method': method,
                    'new_change_ids': new_change_ids,
                    'files': checksum_data,
                }
                cbt.write_backup_metadata(storage, dest_rel_dir, metadata)
            except Exception as e:
                log_warn(f"[BACKUP] Metadata write warning: {e}")
            
            return True, f"Backup completed [{method}]: {len(files_downloaded)} file(s) saved to storage", checksum_json, new_change_ids, None, None


        except Exception as e:
            if (is_cancelled_func and is_cancelled_func()) or "cancelled" in str(e).lower():
                return False, "Backup cancelled by user", None, None, None, None
            last_error = str(e)
            log_error(f"[BACKUP] Attempt {attempt} failed: {last_error}")

            if snap_name:
                try: _remove_backup_snapshot(si, vm_name, snap_name, timeout_mins=30)
                except Exception as ce: log_error(f"[BACKUP] Snapshot cleanup error: {ce}")

            try:
                if temp_ds_dir and 'fm' in locals() and 'datacenter' in locals():
                    fm.DeleteDatastoreFile_Task(name=temp_ds_dir, datacenter=datacenter)
            except Exception as ce:
                log_error(f"[BACKUP] Temp cleanup error: {ce}")

            if attempt < max_retries:
                log_warn(f"[BACKUP] Retrying in 10s... (Attempt {attempt+1}/{max_retries})")
                time.sleep(10)

    return False, f"Backup failed after 3 attempts. Last error: {last_error}", None, None, None, None
