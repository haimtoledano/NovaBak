import ssl
import atexit
import urllib3
import datetime
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from logger_util import log_info, log_warn, log_error

# Disable strict SSL verification warnings for ESXi self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def connect_esxi(host, user, pwd):
    """
    Connects to the ESXi host and returns the service instance.
    """
    context = ssl._create_unverified_context() # Ignore self-signed certs
    try:
        log_info(f"[ESXI] Connecting to {host}...")
        import socket
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10.0) # 10 second timeout for ESXi connection
        try:
            si = SmartConnect(host=host, user=user, pwd=pwd, sslContext=context)
        finally:
            socket.setdefaulttimeout(old_timeout)
        log_info(f"[ESXI] Connected successfully to {host}")
        atexit.register(Disconnect, si)
        return si
    except Exception as e:
        log_error(f"Failed to connect to ESXi {host}: {e}")
        return None

def get_all_vms(si):
    """
    Retrieves a list of all VMs on the host.
    Returns a list of dictionaries with basic VM info.
    """
    if not si:
        return []
    
    content = si.RetrieveContent()
    container = content.rootFolder
    viewType = [vim.VirtualMachine]
    recursive = True
    
    containerView = content.viewManager.CreateContainerView(container, viewType, recursive)
    
    children = containerView.view
    vm_list = []
    
    for child in children:
        vm_list.append({
            "name": child.summary.config.name,
            "power_state": child.summary.runtime.powerState,
            "uuid": child.summary.config.uuid,
            "cpu_count": child.summary.config.numCpu if child.summary.config else 0,
            "memory_mb": child.summary.config.memorySizeMB if child.summary.config else 0,
            "storage_gb": round(child.summary.storage.committed / (1024**3), 2) if child.summary.storage else 0.0
        })
    
    return vm_list


def get_datacenter_name(si, datastore_name):
    """
    Finds a Datastore by name and traverses its parent hierarchy to find its Datacenter.
    If no Datacenter is found (or it's a standalone ESXi host without one), returns 'ha-datacenter'.
    """
    if not si:
        return "ha-datacenter"
    
    content = si.RetrieveContent()
    containerView = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
    
    target_ds = None
    for ds in containerView.view:
        if ds.name == datastore_name:
            target_ds = ds
            break
            
    if not target_ds:
        return "ha-datacenter"
        
    obj = target_ds.parent
    while obj:
        if isinstance(obj, vim.Datacenter):
            return obj.name
        if hasattr(obj, 'parent'):
            obj = obj.parent
        else:
            break
            
    return "ha-datacenter"

def get_datastores(si):
    """
    Retrieves a list of all datastore names on the host.
    """
    if not si:
        return []
    
    content = si.RetrieveContent()
    container = content.rootFolder
    viewType = [vim.Datastore]
    recursive = True
    
    containerView = content.viewManager.CreateContainerView(container, viewType, recursive)
    
    children = containerView.view
    ds_list = []
    
    for child in children:
        cap = child.summary.capacity or 0
        free = child.summary.freeSpace or 0
        cap_gb = round(cap / (1024**3), 1) if cap else 0.0
        free_gb = round(free / (1024**3), 1) if free else 0.0
        free_pct = round((free / cap) * 100, 1) if cap else 0.0
        ds_list.append({
            "name": child.summary.name,
            "capacity_gb": cap_gb,
            "free_gb": free_gb,
            "free_pct": free_pct,
        })
        
    return ds_list

def create_snapshot(si, vm_name):
    """ Creates a crash-consistent snapshot of a VM. Returns the task. """
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    
    if not vm:
        print(f"VM {vm_name} not found for snapshot.")
        return None
        
    snapshot_name = f"VMBACKUP_TEMP_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    description = "Temporary snapshot for automated backup."
    memory = False  # Crash-consistent only
    quiesce = False # Basic snapshot
    
    task = vm.CreateSnapshot_Task(name=snapshot_name, description=description, memory=memory, quiesce=quiesce)
    print(f"Creating snapshot {snapshot_name} for {vm_name}...")
    
    import time
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(2)
        
    if task.info.state == vim.TaskInfo.State.success:
        print(f"Snapshot created successfully.")
        # Return the actual snapshot object reference
        for snap in vm.snapshot.rootSnapshotList:
            if snap.name == snapshot_name:
                return snap.snapshot
        return True # Fallback if we can't find the exact ref
    else:
        print(f"Snapshot creation failed: {task.info.error}")
        return None

def remove_snapshot(si, vm_name, timeout_mins=60):
    """ Consolidates and removes all VMBACKUP_TEMP snapshots for a VM. """
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    
    if not vm or not vm.snapshot:
        return True
        
    def find_backup_snapshots(snap_tree):
        snaps = []
        for snap in snap_tree:
            if snap.name.startswith("VMBACKUP_TEMP_"):
                snaps.append(snap.snapshot)
            snaps.extend(find_backup_snapshots(snap.childSnapshotList))
        return snaps
        
    backup_snaps = find_backup_snapshots(vm.snapshot.rootSnapshotList)
    import time
    for snap_obj in backup_snaps:
        print(f"Consolidating/Removing backup snapshot for {vm_name}...")
        task = snap_obj.RemoveSnapshot_Task(removeChildren=False)
        
        start_wait = time.time()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            if (time.time() - start_wait) > (timeout_mins * 60):
                print(f"[WARN] Timeout ({timeout_mins}m) reached while waiting for snapshot removal on {vm_name}.")
                return False
            time.sleep(2)
            
        if task.info.state == vim.TaskInfo.State.error:
            print(f"Failed to remove snapshot: {task.info.error}")
            return False
            
    return True

def wait_for_vm_idle(si, vm_name, timeout_mins=15):
    """ Polls the VM's recent tasks and waits until none are in progress. """
    content = si.RetrieveContent()
    container = content.rootFolder
    viewType = [vim.VirtualMachine]
    recursive = True
    containerView = content.viewManager.CreateContainerView(container, viewType, recursive)
    
    vm = None
    for child in containerView.view:
        if child.name == vm_name:
            vm = child
            break
            
    if not vm:
        print(f"wait_for_vm_idle: VM {vm_name} not found.")
        return True
        
    import time
    start_time = time.time()
    print(f"Checking if VM {vm_name} is busy with tasks...")
    
    while (time.time() - start_time) < (timeout_mins * 60):
        busy = False
        active_tasks_info = []
        
        # recentTask includes tasks from the last few minutes
        for task in vm.recentTask:
            if task.info.state in [vim.TaskInfo.State.running, vim.TaskInfo.State.queued]:
                busy = True
                
                # Calculate duration
                duration_str = "Unknown"
                if task.info.startTime:
                    now = datetime.datetime.now(task.info.startTime.tzinfo)
                    diff = now - task.info.startTime
                    duration_str = str(diff).split('.')[0] # Remove microseconds
                
                task_desc = task.info.descriptionId or "Unknown Task"
                progress = task.info.progress if task.info.progress is not None else 0
                
                info_msg = f"{task_desc} (State: {task.info.state}, Progress: {progress}%, Started: {duration_str} ago)"
                active_tasks_info.append(info_msg)
        
        if not busy:
            print(f"VM {vm_name} is idle. Proceeding...")
            return True, ""
            
        status_report = " | ".join(active_tasks_info)
        print(f"VM {vm_name} is busy: {status_report}. Waiting 15s...")
        time.sleep(15)
        
    final_msg = f"Timeout ({timeout_mins}m) waiting for VM {vm_name} to be idle."
    if active_tasks_info:
        final_msg += f" Last active tasks: {status_report}"
    
    print(f"[WARN] {final_msg}")
    return False, final_msg

def disconnect_removable_devices(si, vm_name):
    """ Disconnects any ISO images from CD-ROM drives and Floppy drives before backup. """
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    
    if not vm:
        print(f"disconnect_removable_devices: VM {vm_name} not found.")
        return False
        
    devices_to_change = []
    for device in vm.config.hardware.device:
        if isinstance(device, (vim.VirtualCdrom, vim.VirtualFloppy)):
            # Check if it has a backing that points to a file (ISO/FLP)
            has_file_backing = False
            if hasattr(device.backing, 'fileName') and device.backing.fileName:
                has_file_backing = True
                
            if has_file_backing:
                print(f"Disconnecting removable media {device.backing.fileName} from {vm_name}...")
                
                if isinstance(device, vim.VirtualCdrom):
                    new_backing = vim.VirtualCdromAtapiBackingInfo(deviceName="")
                else:
                    new_backing = vim.VirtualFloppyDeviceBackingInfo(deviceName="")
                
                device.backing = new_backing
                device.connectable.connected = False
                device.connectable.startConnected = False
                
                spec = vim.VirtualDeviceConfigSpec()
                spec.device = device
                spec.operation = vim.VirtualDeviceConfigSpec.Operation.edit
                devices_to_change.append(spec)
                
    if not devices_to_change:
        return True
        
    config_spec = vim.vm.ConfigSpec()
    config_spec.deviceChange = devices_to_change
    
    task = vm.ReconfigVM_Task(spec=config_spec)
    
    import time
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(2)
        
    return task.info.state == vim.TaskInfo.State.success

def check_consolidation_needed(si, vm_name):
    """ Returns True if the VM requires disk consolidation. """
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    if vm and hasattr(vm.runtime, 'consolidationNeeded'):
        return vm.runtime.consolidationNeeded
    return False

def cleanup_ghost_tasks(si, vm_name, timeout_mins=60):
    """ Finds any existing 'exportVm' tasks for this VM and cancels them if they are too old. """
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    if not vm:
        return
        
    for task in vm.recentTask:
        if task.info.descriptionId == "vim.VirtualMachine.exportVm" and task.info.state == vim.TaskInfo.State.running:
            # Check age
            if task.info.startTime:
                now = datetime.datetime.now(task.info.startTime.tzinfo)
                diff = now - task.info.startTime
                if diff.total_seconds() > (timeout_mins * 60):
                    print(f"[RECOVERY] Found ghost exportVm task for {vm_name} running for {int(diff.total_seconds()/60)}m. Attempting to cancel...")
                    try:
                        task.CancelTask()
                    except Exception as e:
                        print(f"[RECOVERY] Failed to cancel task: {e}")

def shutdown_vm(si, vm_name, graceful_timeout_mins=5):
    """
    Gracefully shuts down a VM using VMware Tools guest shutdown.
    Falls back to hard PowerOff if the guest doesn't respond within graceful_timeout_mins.
    Returns (success: bool, message: str)
    """
    import time
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    if not vm:
        return False, f"VM {vm_name} not found"

    power_state = getattr(vm.runtime, 'powerState', None)
    if power_state == 'poweredOff':
        log_info(f"[POWER] VM {vm_name} is already powered off.")
        return True, "Already powered off"

    # Try graceful shutdown via VMware Tools
    try:
        log_info(f"[POWER] Sending graceful shutdown to {vm_name}...")
        vm.ShutdownGuest()
    except Exception as e:
        log_warn(f"[POWER] Guest shutdown failed (tools may not be running): {e}. Falling back to hard power off.")
        try:
            task = vm.PowerOffVM_Task()
            start = time.time()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                if time.time() - start > 120:
                    return False, "Hard power-off timeout"
                time.sleep(3)
            if task.info.state == vim.TaskInfo.State.success:
                log_info(f"[POWER] Hard power-off successful for {vm_name}")
                return True, "Hard power-off successful"
            else:
                return False, f"Hard power-off failed: {task.info.error}"
        except Exception as e2:
            return False, f"Hard power-off error: {e2}"

    # Wait for graceful shutdown to complete
    deadline = time.time() + (graceful_timeout_mins * 60)
    log_info(f"[POWER] Waiting up to {graceful_timeout_mins}m for {vm_name} to shut down...")
    while time.time() < deadline:
        time.sleep(5)
        try:
            # Refresh VM state
            vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
            if vm and vm.runtime.powerState == 'poweredOff':
                log_info(f"[POWER] {vm_name} shut down gracefully.")
                return True, "Graceful shutdown successful"
        except Exception:
            pass

    # Graceful timeout — fall back to hard power off
    log_warn(f"[POWER] Graceful shutdown timeout for {vm_name}. Issuing hard power-off...")
    try:
        task = vm.PowerOffVM_Task()
        start = time.time()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            if time.time() - start > 120:
                return False, "Hard power-off timeout after graceful timeout"
            time.sleep(3)
        if task.info.state == vim.TaskInfo.State.success:
            log_info(f"[POWER] Hard power-off successful for {vm_name} (after graceful timeout)")
            return True, "Hard power-off successful (graceful timeout)"
        else:
            return False, f"Hard power-off failed: {task.info.error}"
    except Exception as e:
        return False, f"Hard power-off error after graceful timeout: {e}"


def poweron_vm(si, vm_name, timeout_mins=3):
    """
    Powers on a VM. Waits until it reaches poweredOn state.
    Returns (success: bool, message: str)
    """
    import time
    content = si.RetrieveContent()
    vm = content.searchIndex.FindByInventoryPath(f"ha-datacenter/vm/{vm_name}")
    if not vm:
        return False, f"VM {vm_name} not found"

    power_state = getattr(vm.runtime, 'powerState', None)
    if power_state == 'poweredOn':
        log_info(f"[POWER] VM {vm_name} is already powered on.")
        return True, "Already powered on"

    log_info(f"[POWER] Powering on {vm_name}...")
    try:
        task = vm.PowerOnVM_Task()
        start = time.time()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            if time.time() - start > (timeout_mins * 60):
                return False, f"Power-on timeout ({timeout_mins}m)"
            time.sleep(3)
        if task.info.state == vim.TaskInfo.State.success:
            log_info(f"[POWER] {vm_name} powered on successfully.")
            return True, "Power-on successful"
        else:
            return False, f"Power-on failed: {task.info.error}"
    except Exception as e:
        return False, f"Power-on error: {e}"


def register_vm(si, datastore_name, vmx_rel_path, vm_name):
    """
    Registers a VM in the ESXi inventory from an existing VMX file on a datastore.
    """
    import time
    content = si.RetrieveContent()

    # Find the target datastore to trace its datacenter and host
    containerView = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
    target_ds = None
    for ds in containerView.view:
        if ds.name == datastore_name:
            target_ds = ds
            break
            
    if not target_ds:
        raise Exception(f"Datastore {datastore_name} not found")
        
    # Traverse to find the Datacenter
    dc = None
    obj = target_ds.parent
    while obj:
        if isinstance(obj, vim.Datacenter):
            dc = obj
            break
        if hasattr(obj, 'parent'):
            obj = obj.parent
        else:
            break
            
    if not dc:
        dc = content.rootFolder.childEntity[0]

    folder = dc.vmFolder

    resource_pool = None
    try:
        for child in dc.hostFolder.childEntity:
            if hasattr(child, 'resourcePool') and child.resourcePool:
                resource_pool = child.resourcePool
                break
    except Exception:
        pass

    if resource_pool is None and target_ds.host:
        try:
            host_mount = target_ds.host[0]
            host = host_mount.key
            resource_pool = host.parent.resourcePool
        except Exception:
            pass

    vmx_path = f"[{datastore_name}] {vmx_rel_path}"

    log_info(f"Registering VM {vm_name} from {vmx_path}... (pool: {resource_pool})")
    try:
        task = folder.RegisterVM_Task(
            path=vmx_path,
            name=vm_name,
            asTemplate=False,
            pool=resource_pool
        )

        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)

        if task.info.state == vim.TaskInfo.State.success:
            log_info(f"VM {vm_name} registered successfully.")
            return True, "Success"
        else:
            return False, str(task.info.error)
    except Exception as e:
        return False, str(e)


# --- CBT (Changed Block Tracking) Helpers ---
import time

def enable_cbt(vm):
    """Enables Changed Block Tracking (CBT) on a VM if not already enabled."""
    log_info(f"Enabling CBT on VM {vm.name}...")
    spec = vim.vm.ConfigSpec()
    task = vm.Reconfigure(spec)
    
    # Wait for task
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(1)
        
    if task.info.state == vim.TaskInfo.State.success:
        log_info(f"Successfully enabled CBT on VM {vm.name}.")
        return True
    else:
        log_error(f"Failed to enable CBT on VM {vm.name}: {task.info.error}")
        return False

def query_changed_blocks(vm, snapshot, change_id="*"):
    """
    Queries changed disk areas for a VM against a snapshot.
    change_id='*' gets all allocated blocks (Full backup CBT).
    Otherwise pass a previous change_id for Incremental.
    Returns a dict mapping disk_key -> list of (offset, length).
    """
    if not vm.config.changeTrackingEnabled:
        raise ValueError(f"CBT is not enabled on VM {vm.name}")
        
    disk_changes = {}
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualDisk):
            try:
                # Query changed blocks
                changes = vm.QueryChangedDiskAreas(snapshot=snapshot, deviceKey=device.key, offset=0, changeId=change_id)
                blocks = []
                if changes.diskArea:
                    for area in changes.diskArea:
                        blocks.append((area.start, area.length))
                disk_changes[device.key] = blocks
            except Exception as e:
                log_warn(f"Failed to query changed blocks for disk {device.key}: {e}")
                
    return disk_changes


def disconnect_vnics(si, vm_name):
    """Disconnects all virtual network adapters for a VM (useful for Test Restores)."""
    vm = _get_vm(si, vm_name)
    if not vm:
        return False, "VM not found"

    specs = []
    for device in vm.config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualEthernetCard):
            device.connectable.connected = False
            device.connectable.startConnected = False
            
            spec = vim.vm.device.VirtualDeviceSpec()
            spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            spec.device = device
            specs.append(spec)

    if specs:
        config_spec = vim.vm.ConfigSpec(deviceChange=specs)
        task = vm.Reconfigure(config_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(1)
        if task.info.state == vim.TaskInfo.State.success:
            return True, "vNICs disconnected"
        else:
            return False, f"Failed to disconnect vNICs: {task.info.error}"
    return True, "No vNICs found"

def unregister_and_delete_vm(si, vm_name):
    """Unregisters and securely deletes a VM and its files from the datastore."""
    vm = _get_vm(si, vm_name)
    if not vm:
        return False, "VM not found"
        
    try:
        # First ensure powered off
        if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn:
            task = vm.PowerOffVM_Task()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(1)
                
        # Destroy VM (removes from inventory AND deletes files)
        task = vm.Destroy_Task()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
            
        if task.info.state == vim.TaskInfo.State.success:
            return True, "VM destroyed successfully"
        else:
            return False, f"Destroy task failed: {task.info.error}"
    except Exception as e:
        return False, str(e)
