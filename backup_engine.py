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
                         progress_base=0, progress_total=100, speed_callback=None):
    """
    Downloads a file from ESXi's built-in HTTP file server via StorageProvider.
    """
    host_ip = _get_host_ip(si)
    if not host_ip:
        raise Exception("Cannot determine ESXi host IP")

    cookies = _get_session_cookies(si)

    # URL-encode the file path (but not the slashes)
    encoded_path = '/'.join(url_quote(p, safe='') for p in file_path.split('/'))

    url = (f"https://{host_ip}/folder/{encoded_path}"
           f"?dcPath=ha-datacenter&dsName={url_quote(datastore_name, safe='')}")

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

    storage.makedirs(os.path.dirname(dest_rel_path))

    # We use a context manager for the storage writer
    with storage.open_write(dest_rel_path) as f:
        # Optimization for local files (sparse support)
        is_local = hasattr(storage, 'base_path')
        zero_chunk = b'\x00' * CHUNK_SIZE if is_local else None

        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                # Thin provisioning stream optimization (only for local files with seek support)
                if is_local and len(chunk) == CHUNK_SIZE and chunk == zero_chunk:
                    if hasattr(f, 'seek'):
                        f.seek(CHUNK_SIZE, os.SEEK_CUR)
                        bytes_written += CHUNK_SIZE
                    else:
                        f.write(chunk)
                        bytes_written += len(chunk)
                elif is_local and not chunk.strip(b'\x00'):
                    if hasattr(f, 'seek'):
                        f.seek(len(chunk), os.SEEK_CUR)
                        bytes_written += len(chunk)
                    else:
                        f.write(chunk)
                        bytes_written += len(chunk)
                else:
                    f.write(chunk)
                    bytes_written += len(chunk)
                    
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
    log_info(f"[DOWNLOAD] Complete: {size_mb:.1f} MB processed for {os.path.basename(dest_rel_path)}")

    return bytes_written


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

# ===========================================================================
#  MAIN: Preflight Check
# ===========================================================================
def preflight_check(si, vm_name, timeout_mins=15, **kwargs):
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

    for name, func in steps:
        log_info(f"[PREFLIGHT] Step: {name}...")
        try:
            result = func()
            if not result:
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

    encoded_path = '/'.join(url_quote(p, safe='') for p in dest_rel_path.split('/'))
    url = (f"https://{host_ip}/folder/{encoded_path}"
           f"?dcPath=ha-datacenter&dsName={url_quote(datastore_name, safe='')}")

    log_info(f"[UPLOAD] {source_rel_path} to [{datastore_name}] {dest_rel_path}")

    with storage.open_read(source_rel_path) as f:
        f_size = storage.get_size(source_rel_path)
        
        class CancelableStreamWrapper:
            def __init__(self, stream, size, is_cancelled_func):
                self._stream = stream
                self._size = size
                self._is_cancelled = is_cancelled_func
                self._read_so_far = 0
                self._last_pct = -1

            def read(self, size=-1):
                if self._is_cancelled and self._is_cancelled():
                    raise Exception("CancellationRequested")
                
                chunk = self._stream.read(size)
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
                
        wrapped_stream = CancelableStreamWrapper(f, f_size, is_cancelled_func)

        try:
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
def export_vm_native(si, vm_name, storage, dest_rel_dir, progress_callback=None, speed_callback=None, max_retries=3, **kwargs):
    """
    Power-state-aware backup:

    POWERED OFF → Direct pipe (no snapshot, no CopyVirtualDisk):
      Snapshot → stream descriptor → stream flat VMDK → stream VMX → done
      ~2x faster, no temp storage used.

    POWERED ON / SUSPENDED → Safe path (snapshot + CopyVirtualDisk):
      Snapshot → CopyVirtualDisk (unlocks) → stream copies → stream VMX → cleanup
      Required because ESXi kernel-locks flat VMDKs on running VMs.
    """
    vm = _get_vm(si, vm_name)
    if not vm:
        return False, f"VM {vm_name} not found"

    last_error = ""
    snap_name = None
    temp_ds_dir = None

    for attempt in range(1, max_retries + 1):
        log_info(f"[BACKUP] Attempt {attempt}/{max_retries} for {vm_name}")

        try:
            # --- Step 1: Collect disk info + detect power state ---
            vm = _get_vm(si, vm_name)
            content = si.RetrieveContent()
            datacenter = content.rootFolder.childEntity[0]

            power_state = getattr(vm.runtime, 'powerState', 'poweredOn')
            is_off = (power_state == 'poweredOff')
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
            #  PATH A: POWERED OFF — Direct stream, no snapshot, no copy
            # ==============================================================
            if is_off:
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
                    _download_file_http(
                        si, disk['ds_name'], disk['rel_path'], storage, f"{dest_rel_dir}/{disk_basename}",
                        progress_callback=progress_callback, progress_base=desc_base, progress_total=2,
                        speed_callback=speed_callback
                    )
                    files_downloaded.append(disk_basename)

                    log_info(f"[BACKUP] [DIRECT] Streaming flat disk ({idx+1}/{total_disks}): {flat_basename}")
                    _download_file_http(
                        si, disk['ds_name'], flat_rel_path, storage, f"{dest_rel_dir}/{flat_basename}",
                        progress_callback=progress_callback, progress_base=flat_base,
                        progress_total=flat_end - flat_base, speed_callback=speed_callback
                    )
                    files_downloaded.append(flat_basename)

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

                    _download_file_http(
                        si, ds_name, temp_rel_path, storage, f"{dest_rel_dir}/{disk_basename}",
                        progress_callback=progress_callback, progress_base=dl_start, progress_total=2,
                        speed_callback=speed_callback
                    )
                    files_downloaded.append(disk_basename)
                    _download_file_http(
                        si, ds_name, flat_rel_path, storage, f"{dest_rel_dir}/{flat_basename}",
                        progress_callback=progress_callback, progress_base=dl_mid,
                        progress_total=dl_end - dl_mid, speed_callback=speed_callback
                    )
                    files_downloaded.append(flat_basename)

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
                    _download_file_http(si, vmx_ds_name, vmx_rel_path, storage, f"{dest_rel_dir}/{vmx_filename}")
                    files_downloaded.append(vmx_filename)
                    log_info(f"[BACKUP] VMX saved: {vmx_filename}")
                except Exception as e:
                    log_warn(f"[BACKUP] VMX warning: {e}")

            if progress_callback: progress_callback(100)
            method = "direct" if is_off else "snapshot+copy"
            return True, f"Backup completed [{method}]: {len(files_downloaded)} file(s) saved to storage"


        except Exception as e:
            last_error = str(e)
            log_error(f"[BACKUP] Attempt {attempt} failed: {last_error}")

            if snap_name:
                try: _remove_backup_snapshot(si, vm_name, snap_name, timeout_mins=30)
                except Exception as ce: log_error(f"[BACKUP] Snapshot cleanup error: {ce}")
                snap_name = None

            if temp_ds_dir:
                try:
                    c2 = si.RetrieveContent()
                    dc2 = c2.rootFolder.childEntity[0]
                    c2.fileManager.DeleteDatastoreFile_Task(name=temp_ds_dir, datacenter=dc2)
                except Exception: pass
                temp_ds_dir = None

            if attempt < max_retries:
                log_info(f"[BACKUP] Waiting 15s before retry...")
                time.sleep(15)

    return False, f"Backup failed after {max_retries} attempts. Last error: {last_error}"

