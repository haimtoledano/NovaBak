import esxi_handler
import sys
from models import SessionLocal, ESXiHost

def check_status(vm_name):
    db = SessionLocal()
    host = db.query(ESXiHost).first() # Just check first host for now or all
    if not host:
        print("No ESXi host configured.")
        return

    si = esxi_handler.connect_esxi(host.host_ip, host.username, host.password)
    if not si:
        print(f"Could not connect to {host.host_ip}")
        return

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
        print(f"VM {vm_name} not found.")
        return

    print(f"--- VM Status: {vm_name} ---")
    print(f"Power State: {vm.runtime.powerState}")
    print(f"Overall Status: {vm.overallStatus}")
    
    # Check for snapshots
    if vm.snapshot:
        print("Snapshots found:")
        def list_snaps(tree, indent=0):
            for s in tree:
                print("  " * indent + f"- {s.name} ({s.createTime})")
                list_snaps(s.childSnapshotList, indent + 1)
        list_snaps(vm.snapshot.rootSnapshotList)
    else:
        print("No snapshots found.")

    # Check for recent tasks
    print("\nRecent Tasks on VM:")
    tasks = vm.recentTask
    if not tasks:
        print("No recent tasks found.")
    else:
        for t in tasks:
            print(f"- Task: {t.info.descriptionId} | State: {t.info.state} | Progress: {t.info.progress}%")

    esxi_handler.Disconnect(si)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_vm_status.py <vm_name>")
    else:
        check_status(sys.argv[1])
