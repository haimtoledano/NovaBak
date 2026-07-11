import re

modal_html = """
<!-- Storage Target Modal -->
<div id="storage-target-modal" class="modal-overlay hidden">
    <div class="modal-content" style="max-width: 600px;">
        <h3 id="storage-target-modal-title" class="text-lg font-semibold mb-4">Add Storage Target</h3>
        <form id="storage-target-form" onsubmit="submitStorageTarget(event)">
            <input type="hidden" id="st_id">
            
            <div class="mb-4">
                <span class="input-label">Target Name</span>
                <input type="text" id="st_name" required class="w-full py-2 px-3 font-mono text-sm mt-1" placeholder="e.g. Primary NAS">
            </div>
            
            <div class="mb-4 flex items-center space-x-2">
                <input type="checkbox" id="st_is_default" class="w-4 h-4">
                <span class="input-label mb-0">Set as Default Storage Target</span>
            </div>

            <div class="mb-4">
                <span class="input-label">Storage Type</span>
                <select id="st_type" required onchange="toggleStFields()" class="w-full py-2 px-3 text-sm mt-1 border rounded" style="border-color: var(--border-color); background: var(--bg-card); color: var(--text-main)">
                    <option value="SMB">SMB (Windows Share)</option>
                    <option value="NFS">NFS</option>
                    <option value="S3">S3 Compatible</option>
                </select>
            </div>

            <!-- SMB Fields -->
            <div id="st_fields_SMB" class="st-fields">
                <div class="mb-4">
                    <span class="input-label">UNC Path</span>
                    <input type="text" id="st_smb_unc_path" placeholder="\\\\server\\share\\backups" class="w-full py-2 px-3 font-mono text-sm mt-1">
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div><span class="input-label">Username</span><input type="text" id="st_smb_user" class="w-full py-2 px-3 font-mono text-sm mt-1"></div>
                    <div><span class="input-label">Password</span><input type="password" id="st_smb_password" class="w-full py-2 px-3 font-mono text-sm mt-1"></div>
                </div>
            </div>

            <!-- NFS Fields -->
            <div id="st_fields_NFS" class="st-fields hidden">
                <span class="input-label">NFS Export Path</span>
                <input type="text" id="st_nfs_path" placeholder="/mnt/backups" class="w-full py-2 px-3 font-mono text-sm mt-1">
            </div>

            <!-- S3 Fields -->
            <div id="st_fields_S3" class="st-fields hidden">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                    <div class="sm:col-span-2">
                        <span class="input-label">S3 Endpoint (optional for AWS)</span>
                        <input type="text" id="st_s3_endpoint" placeholder="https://s3.wasabisys.com" class="w-full py-2 px-3 font-mono text-sm mt-1">
                    </div>
                    <div><span class="input-label">Access Key</span><input type="text" id="st_s3_access_key" class="w-full py-2 px-3 font-mono text-sm mt-1"></div>
                    <div><span class="input-label">Secret Key</span><input type="password" id="st_s3_secret_key" class="w-full py-2 px-3 font-mono text-sm mt-1"></div>
                    <div><span class="input-label">Bucket Name</span><input type="text" id="st_s3_bucket" class="w-full py-2 px-3 font-mono text-sm mt-1"></div>
                    <div><span class="input-label">Region</span><input type="text" id="st_s3_region" placeholder="us-east-1" class="w-full py-2 px-3 font-mono text-sm mt-1"></div>
                </div>
            </div>

            <div class="mt-6 flex justify-end space-x-3">
                <button type="button" onclick="closeStorageTargetModal()" class="px-4 py-2 text-sm font-semibold text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200">Cancel</button>
                <button type="submit" class="btn-primary px-6 py-2 text-sm font-semibold">Save Target</button>
            </div>
        </form>
    </div>
</div>
"""

js_code = """
function toggleStFields() {
    const type = document.getElementById('st_type').value;
    document.querySelectorAll('.st-fields').forEach(el => el.classList.add('hidden'));
    document.getElementById(`st_fields_${type}`).classList.remove('hidden');
}

function openStorageTargetModal(target = null) {
    const modal = document.getElementById('storage-target-modal');
    const form = document.getElementById('storage-target-form');
    form.reset();
    
    if (target) {
        document.getElementById('storage-target-modal-title').innerText = 'Edit Storage Target';
        document.getElementById('st_id').value = target.id;
        document.getElementById('st_name').value = target.name;
        document.getElementById('st_is_default').checked = target.is_default;
        document.getElementById('st_type').value = target.storage_type;
        document.getElementById('st_smb_unc_path').value = target.smb_unc_path || '';
        document.getElementById('st_smb_user').value = target.smb_user || '';
        document.getElementById('st_nfs_path').value = target.nfs_path || '';
        document.getElementById('st_s3_endpoint').value = target.s3_endpoint || '';
        document.getElementById('st_s3_access_key').value = target.s3_access_key || '';
        document.getElementById('st_s3_bucket').value = target.s3_bucket || '';
        document.getElementById('st_s3_region').value = target.s3_region || '';
    } else {
        document.getElementById('storage-target-modal-title').innerText = 'Add Storage Target';
        document.getElementById('st_id').value = '';
    }
    
    toggleStFields();
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function closeStorageTargetModal() {
    const modal = document.getElementById('storage-target-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

async function submitStorageTarget(e) {
    e.preventDefault();
    const id = document.getElementById('st_id').value;
    const isEdit = !!id;
    
    const payload = {
        name: document.getElementById('st_name').value,
        is_default: document.getElementById('st_is_default').checked,
        storage_type: document.getElementById('st_type').value,
        smb_unc_path: document.getElementById('st_smb_unc_path').value || null,
        smb_user: document.getElementById('st_smb_user').value || null,
        smb_password: document.getElementById('st_smb_password').value || null,
        nfs_path: document.getElementById('st_nfs_path').value || null,
        s3_endpoint: document.getElementById('st_s3_endpoint').value || null,
        s3_access_key: document.getElementById('st_s3_access_key').value || null,
        s3_secret_key: document.getElementById('st_s3_secret_key').value || null,
        s3_bucket: document.getElementById('st_st_s3_bucket') ? document.getElementById('st_st_s3_bucket').value : document.getElementById('st_s3_bucket').value || null,
        s3_region: document.getElementById('st_s3_region').value || null
    };

    try {
        const url = isEdit ? `/api/v1/storage-targets/${id}` : '/api/v1/storage-targets';
        const method = isEdit ? 'PUT' : 'POST';
        
        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to save target');
        }
        
        closeStorageTargetModal();
        loadStorageTargets();
        showAlert('Storage target saved successfully!');
    } catch (err) {
        alert(err.message);
    }
}
"""

with open('templates/partials/settings_tab.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the dummy JS with the real one
content = re.sub(r'function openStorageTargetModal\(\) \{.*?\n\}', js_code, content, flags=re.DOTALL)
content = content + '\n' + modal_html

with open('templates/partials/settings_tab.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Modal added")
