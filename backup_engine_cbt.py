"""
backup_engine_cbt.py — Changed Block Tracking (CBT) for Incremental Backups

Uses VMware's QueryChangedDiskAreas API to identify changed blocks since the
last backup, then downloads only those blocks via HTTP Range Requests.

Incremental file format (.nb-incr):
    [4 bytes: magic "NBI1"]
    [2 bytes: version (1)]
    [1 byte:  flags (bit0=compressed, bit1=encrypted)]
    [1 byte:  reserved]
    [8 bytes: disk_total_size (uint64)]
    [4 bytes: block_count (uint32)]
    For each block:
        [8 bytes: offset (uint64)]
        [4 bytes: length (uint32)]
        [<length> bytes: data]
"""

import os
import json
import struct
import hashlib
import time
import requests
from pyVmomi import vim
from logger_util import log_info, log_warn, log_error

requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning)

NBI_MAGIC = b'NBI1'
NBI_VERSION = 1
NBI_HEADER_SIZE = 20  # 4 + 2 + 1 + 1 + 8 + 4


# ---------------------------------------------------------------------------
#  Enable CBT on a VM
# ---------------------------------------------------------------------------
def enable_cbt(si, vm_name):
    """Enable Changed Block Tracking on a VM if not already enabled.
    Note: CBT activation requires a VM stun (brief pause) or power cycle on older ESXi.
    Returns True if CBT is now enabled."""
    from backup_engine import _get_vm
    vm = _get_vm(si, vm_name)
    if not vm:
        raise ValueError(f"VM '{vm_name}' not found")

    if vm.config.changeTrackingEnabled:
        log_info(f"[CBT] CBT already enabled on {vm_name}")
        return True

    log_info(f"[CBT] Enabling CBT on {vm_name}...")
    spec = vim.vm.ConfigSpec()
    spec.changeTrackingEnabled = True
    task = vm.ReconfigVM_Task(spec)

    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(1)

    if task.info.state == vim.TaskInfo.State.success:
        log_info(f"[CBT] CBT enabled successfully on {vm_name}")
        return True
    else:
        log_error(f"[CBT] Failed to enable CBT on {vm_name}: {task.info.error}")
        return False


# ---------------------------------------------------------------------------
#  Get change ID from a snapshot
# ---------------------------------------------------------------------------
def get_snapshot_change_id(vm, snapshot):
    """Extract the changeId for each virtual disk from a snapshot.
    Returns dict: { disk_key: changeId, ... }"""
    change_ids = {}
    if snapshot and hasattr(snapshot, 'config') and snapshot.config:
        for device in snapshot.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                backing = device.backing
                if hasattr(backing, 'changeId') and backing.changeId:
                    change_ids[device.key] = backing.changeId
    return change_ids


# ---------------------------------------------------------------------------
#  Query changed blocks
# ---------------------------------------------------------------------------
def query_changed_blocks(vm, snapshot, disk_key, change_id="*"):
    """Query VMware for changed disk areas since a given changeId.
    change_id="*" returns all allocated blocks (for CBT-aware full backup).
    Returns list of (offset, length) tuples."""
    if not vm.config.changeTrackingEnabled:
        raise ValueError(f"CBT is not enabled on VM {vm.name}")

    disk_size = 0
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualDisk) and device.key == disk_key:
            disk_size = device.capacityInBytes or (device.capacityInKB * 1024)
            break

    blocks = []
    offset = 0
    while offset < disk_size:
        try:
            change_areas = vm.QueryChangedDiskAreas(
                snapshot=snapshot,
                deviceKey=disk_key,
                startOffset=offset,
                changeId=change_id
            )
            if change_areas.changedArea:
                for area in change_areas.changedArea:
                    blocks.append((area.start, area.length))
            # Move past the last area or break if no more
            if change_areas.changedArea:
                last = change_areas.changedArea[-1]
                offset = last.start + last.length
            else:
                break
        except Exception as e:
            if "change tracking" in str(e).lower() or "invalid" in str(e).lower():
                raise  # CBT issue, don't retry
            log_warn(f"[CBT] Error querying blocks at offset {offset}: {e}")
            break

    return blocks, disk_size


# ---------------------------------------------------------------------------
#  Download changed blocks via HTTP Range Requests
# ---------------------------------------------------------------------------
def download_changed_blocks(si, datastore_name, file_path, changed_blocks,
                             storage, dest_path, disk_total_size,
                             progress_callback=None, progress_base=0,
                             progress_total=100, speed_callback=None,
                             is_cancelled_func=None):
    """Download only the changed blocks from a VMDK using HTTP Range Requests.
    Writes to .nb-incr format."""
    from backup_engine import _get_host_ip, _get_session_cookies
    from esxi_handler import get_datacenter_name
    from urllib.parse import quote as url_quote

    host_ip = _get_host_ip(si)
    if not host_ip:
        raise Exception("Cannot determine ESXi host IP")

    cookies = _get_session_cookies(si)
    dc_name = get_datacenter_name(si, datastore_name)

    encoded_path = '/'.join(url_quote(p, safe='') for p in file_path.split('/'))
    base_url = (f"https://{host_ip}/folder/{encoded_path}"
                f"?dcPath={url_quote(dc_name, safe='')}&dsName={url_quote(datastore_name, safe='')}")

    total_blocks = len(changed_blocks)
    total_bytes_to_download = sum(length for _, length in changed_blocks)
    bytes_downloaded = 0
    hasher = hashlib.sha256()
    speed_window_bytes = 0
    speed_window_start = time.time()

    log_info(f"[CBT] Downloading {total_blocks} changed blocks "
             f"({total_bytes_to_download / (1024*1024):.1f} MB) from {file_path}")

    storage.makedirs(os.path.dirname(dest_path))

    with storage.open_write(dest_path) as f:
        # Write NBI1 header
        flags = 0  # TODO: add compression/encryption flags
        header = NBI_MAGIC + struct.pack('<HBBqI',
                                         NBI_VERSION, flags, 0,
                                         disk_total_size, total_blocks)
        f.write(header)

        # Download each changed block
        for idx, (offset, length) in enumerate(changed_blocks):
            if is_cancelled_func and is_cancelled_func():
                raise Exception("Backup cancelled by user")

            # HTTP Range Request
            range_end = offset + length - 1
            headers = {'Range': f'bytes={offset}-{range_end}'}

            resp = requests.get(base_url, headers=headers, cookies=cookies,
                                verify=False, timeout=300, stream=True)

            if resp.status_code not in (200, 206):
                raise Exception(f"HTTP {resp.status_code} for range {offset}-{range_end}")

            # Read the block data
            block_data = b''
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    block_data += chunk

            # Verify we got the expected length
            if len(block_data) != length:
                log_warn(f"[CBT] Block at offset {offset}: expected {length} bytes, "
                         f"got {len(block_data)} — padding/trimming")
                if len(block_data) < length:
                    block_data += b'\x00' * (length - len(block_data))
                else:
                    block_data = block_data[:length]

            hasher.update(block_data)

            # Write block entry: [offset: 8][length: 4][data]
            f.write(struct.pack('<qI', offset, length))
            f.write(block_data)

            bytes_downloaded += length
            speed_window_bytes += length

            # Progress & speed
            now = time.time()
            elapsed = now - speed_window_start
            if elapsed >= 2.0:
                speed_mbps = (speed_window_bytes / (1024 * 1024)) / elapsed
                if speed_callback:
                    speed_callback(round(speed_mbps, 1))
                speed_window_bytes = 0
                speed_window_start = now

            if progress_callback and total_bytes_to_download > 0:
                pct = progress_base + (bytes_downloaded / total_bytes_to_download) * progress_total
                progress_callback(min(int(pct), 99))

    final_hash = hasher.hexdigest()
    size_mb = bytes_downloaded / (1024 * 1024)
    log_info(f"[CBT] Download complete: {size_mb:.1f} MB in {total_blocks} blocks. "
             f"SHA-256: {final_hash}")

    return bytes_downloaded, final_hash


# ---------------------------------------------------------------------------
#  Assemble full VMDK from chain (full + incrementals)
# ---------------------------------------------------------------------------
def assemble_full_from_chain(storage, full_flat_path, incremental_paths, output_path):
    """Assemble a complete flat VMDK by applying incremental .nb-incr files
    on top of a full backup's flat VMDK.

    Args:
        storage: StorageProvider instance
        full_flat_path: relative path to the full backup's -flat.vmdk
        incremental_paths: list of .nb-incr file paths in chronological order
        output_path: relative path for the assembled output flat VMDK
    """
    log_info(f"[CBT] Assembling chain: 1 full + {len(incremental_paths)} incremental(s)")

    # Step 1: Copy the full flat VMDK as the base
    storage.makedirs(os.path.dirname(output_path))

    log_info(f"[CBT] Copying base full backup: {full_flat_path}")
    with storage.open_read(full_flat_path) as src, storage.open_write(output_path) as dst:
        while True:
            chunk = src.read(4 * 1024 * 1024)  # 4MB chunks
            if not chunk:
                break
            dst.write(chunk)

    # Step 2: Apply each incremental on top
    for incr_idx, incr_path in enumerate(incremental_paths):
        log_info(f"[CBT] Applying incremental {incr_idx + 1}/{len(incremental_paths)}: {incr_path}")

        with storage.open_read(incr_path) as f:
            # Read and validate header
            header = f.read(NBI_HEADER_SIZE)
            if len(header) < NBI_HEADER_SIZE:
                raise Exception(f"Invalid NBI file: {incr_path} (truncated header)")

            magic = header[:4]
            if magic != NBI_MAGIC:
                raise Exception(f"Invalid NBI magic in {incr_path}: {magic}")

            version, flags, _, disk_size, block_count = struct.unpack('<HBBqI', header[4:])

            if version != NBI_VERSION:
                raise Exception(f"Unsupported NBI version {version} in {incr_path}")

            log_info(f"[CBT]   {block_count} blocks, disk size: {disk_size / (1024**3):.1f} GB")

            # Read blocks and apply to output
            # We need random-access write, which requires local storage or a seekable file
            # For SMB/local: open output for read+write
            blocks_applied = 0
            block_entries = []

            # First read all block metadata and data
            for _ in range(block_count):
                entry_header = f.read(12)  # 8 + 4
                if len(entry_header) < 12:
                    raise Exception(f"Truncated block entry in {incr_path}")
                offset, length = struct.unpack('<qI', entry_header)
                data = f.read(length)
                if len(data) < length:
                    raise Exception(f"Truncated block data in {incr_path}")
                block_entries.append((offset, data))

        # Apply blocks (need seek support)
        # Open the output file for writing at specific offsets
        full_path = storage.get_full_path(output_path) if hasattr(storage, 'get_full_path') else None
        if full_path and os.path.exists(full_path):
            with open(full_path, 'r+b') as out:
                for offset, data in block_entries:
                    out.seek(offset)
                    out.write(data)
                    blocks_applied += 1
        else:
            log_warn(f"[CBT] Cannot apply incremental to non-local storage — "
                     f"skipping {incr_path}. Remote assembly not yet supported.")
            continue

        log_info(f"[CBT]   Applied {blocks_applied} blocks")

    log_info(f"[CBT] Chain assembly complete: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
#  Write backup metadata JSON
# ---------------------------------------------------------------------------
def write_backup_metadata(storage, dest_rel_dir, metadata):
    """Write a backup.json file with metadata about the backup."""
    json_path = f"{dest_rel_dir}/backup.json"
    storage.makedirs(dest_rel_dir)

    json_bytes = json.dumps(metadata, indent=2, default=str).encode('utf-8')
    with storage.open_write(json_path) as f:
        f.write(json_bytes)

    log_info(f"[CBT] Metadata written: {json_path}")
    return json_path


# ---------------------------------------------------------------------------
#  Read backup metadata JSON
# ---------------------------------------------------------------------------
def read_backup_metadata(storage, backup_dir):
    """Read backup.json from a backup directory. Returns dict or None."""
    json_path = f"{backup_dir}/backup.json"
    try:
        if not storage.exists(json_path):
            return None
        with storage.open_read(json_path) as f:
            return json.loads(f.read().decode('utf-8'))
    except Exception as e:
        log_warn(f"[CBT] Failed to read metadata from {json_path}: {e}")
        return None


# ---------------------------------------------------------------------------
#  Find the backup chain for a given incremental backup
# ---------------------------------------------------------------------------
def find_backup_chain(storage, vm_name, target_backup_dir):
    """Find the complete chain (full + incrementals) needed to restore a backup.
    Returns (full_dir, [incr_dirs]) in chronological order."""
    meta = read_backup_metadata(storage, target_backup_dir)
    if not meta:
        return target_backup_dir, []  # Assume it's a full backup

    if meta.get('type') == 'full':
        return target_backup_dir, []

    # Walk the chain backwards
    chain = [target_backup_dir]
    current_meta = meta

    while current_meta and current_meta.get('type') == 'incremental':
        parent_dir = current_meta.get('parent_backup_dir')
        if not parent_dir:
            raise Exception(f"Broken chain: {chain[-1]} has no parent_backup_dir")
        chain.append(parent_dir)
        current_meta = read_backup_metadata(storage, parent_dir)

    # chain is [target, ..., full] — reverse to get [full, ..., target]
    chain.reverse()
    full_dir = chain[0]
    incr_dirs = chain[1:]

    log_info(f"[CBT] Chain for {vm_name}: 1 full + {len(incr_dirs)} incremental(s)")
    return full_dir, incr_dirs
