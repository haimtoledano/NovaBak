js_code = '''
<script>
async function loadStorageTargets() {
    try {
        const res = await fetch('/api/v1/storage-targets');
        if (!res.ok) throw new Error('Failed to fetch storage targets');
        const targets = await res.json();
        const tbody = document.getElementById('storage-targets-list');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (targets.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center py-4 text-sm" style="color: var(--text-muted)">No storage targets found.</td></tr>';
            return;
        }
        targets.forEach(t => {
            const tr = document.createElement('tr');
            let path = t.smb_unc_path || t.nfs_path || t.s3_endpoint || '';
            tr.innerHTML = `
                <td class="px-4 py-3 text-sm font-medium">${t.name}</td>
                <td class="px-4 py-3 text-sm">${t.storage_type}</td>
                <td class="px-4 py-3 text-sm font-mono">${path}</td>
                <td class="px-4 py-3 text-sm">${t.is_default ? '<span class="px-2 py-1 bg-green-900 text-green-200 rounded text-xs">Default</span>' : ''}</td>
                <td class="px-4 py-3 text-sm text-right space-x-2">
                    <button onclick="deleteStorageTarget(${t.id})" class="text-red-400 hover:text-red-300">Delete</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error(e);
    }
}

async function deleteStorageTarget(id) {
    if (!confirm('Are you sure you want to delete this target?')) return;
    try {
        await fetch(`/api/v1/storage-targets/${id}`, { method: 'DELETE' });
        loadStorageTargets();
    } catch (e) {
        console.error(e);
    }
}

async function saveEncryptionKey(e) {
    e.preventDefault();
    const key = document.getElementById('encryption_key_input').value;
    try {
        await fetch('/api/v1/config', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ encryption_key: key || null })
        });
        showAlert('Encryption key saved successfully');
    } catch (err) {
        console.error(err);
    }
}

function openStorageTargetModal() {
    showAlert('To add a new target, please use the API for now. (UI modal implementation pending)', {title: 'Not Implemented'});
}

document.addEventListener('DOMContentLoaded', () => {
    // Intercept switchTab
    const originalSwitchTab = window.switchTab;
    window.switchTab = function(tabId) {
        if (originalSwitchTab) originalSwitchTab(tabId);
        if (tabId === 'tab-settings') {
            loadStorageTargets();
        }
    };
    
    const oldShowSettingsPanel = window.showSettingsPanel;
    window.showSettingsPanel = function(panelId) {
        if (oldShowSettingsPanel) oldShowSettingsPanel(panelId);
        if (panelId === 'storage') {
            loadStorageTargets();
        }
    };
});
</script>
'''

with open('templates/partials/settings_tab.html', 'a', encoding='utf-8') as f:
    f.write(js_code)
print('Added JS to settings_tab.html')
