import ssl
import atexit
import urllib3
import datetime
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

# Disable strict SSL verification warnings for ESXi self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def connect_esxi(host, user, pwd):
    """
    Connects to the ESXi host and returns the service instance.
    """
    context = ssl._create_unverified_context() # Ignore self-signed certs
    try:
        si = SmartConnect(host=host, user=user, pwd=pwd, sslContext=context)
        atexit.register(Disconnect, si)
        return si
    except Exception as e:
        print(f"Failed to connect to ESXi: {e}")
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
            "uuid": child.summary.config.uuid
        })
    
    return vm_list

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
        ds_list.append(child.summary.name)
        
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

def remove_snapshot(si, vm_name):
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
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
            
        if task.info.state == vim.TaskInfo.State.error:
            print(f"Failed to remove snapshot: {task.info.error}")
            return False
            
    return True

