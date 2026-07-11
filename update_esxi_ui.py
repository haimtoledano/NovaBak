import re

with open('templates/partials/settings_tab.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Add edit button next to delete button
edit_btn = '''<button type="button" onclick="openEsxiEditModal('{{ host.id }}', '{{ host.name|escapejs }}', '{{ host.host_ip|escapejs }}', '{{ host.username|escapejs }}')" class="text-blue-500 hover:text-blue-400 transition-colors mr-3" title="Edit">
                                            <svg class="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"></path></svg>
                                        </button>'''

content = content.replace('<button type="submit" class="text-gray-500 hover:text-red-400 transition-colors" title="Delete">',
                          edit_btn + '\n                                        <button type="submit" class="text-gray-500 hover:text-red-400 transition-colors" title="Delete">')

# Add modal to the end of the file
modal_html = """
<!-- ESXi Edit Modal -->
<div id="esxi-edit-modal" class="hidden fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
    <div class="card p-6 w-full max-w-lg shadow-2xl border" style="background: var(--bg-main); border-color: var(--border-color)">
        <div class="flex justify-between items-center mb-4">
            <h3 class="font-bold text-lg">Edit ESXi Host</h3>
            <button type="button" onclick="closeEsxiEditModal()" class="text-gray-400 hover:text-white">✕</button>
        </div>
        <input type="hidden" id="edit-esxi-id">
        <div class="mb-3">
            <span class="input-label">Alias</span>
            <input type="text" id="edit-esxi-name" class="w-full p-2 text-sm font-sans border rounded" style="background:transparent; border-color:var(--border-color)">
        </div>
        <div class="mb-3">
            <span class="input-label">IP or FQDN</span>
            <input type="text" id="edit-esxi-ip" class="w-full p-2 text-sm font-sans border rounded" style="background:transparent; border-color:var(--border-color)">
        </div>
        <div class="mb-3">
            <span class="input-label">Username</span>
            <input type="text" id="edit-esxi-user" class="w-full p-2 text-sm font-sans border rounded" style="background:transparent; border-color:var(--border-color)">
        </div>
        <div class="mb-4">
            <span class="input-label">Password (leave blank to keep unchanged)</span>
            <input type="password" id="edit-esxi-pass" class="w-full p-2 text-sm font-sans border rounded" style="background:transparent; border-color:var(--border-color)">
        </div>
        <div id="edit-esxi-status" class="text-sm font-medium mb-4 rounded px-3 py-2 hidden"></div>
        <div class="flex justify-end gap-2">
            <button type="button" onclick="testEsxiConnection()" class="btn-secondary px-4 py-2 text-sm">Test Connection</button>
            <button type="button" onclick="saveEsxiHost()" class="btn-primary px-4 py-2 text-sm">Save</button>
        </div>
    </div>
</div>

<script>
function openEsxiEditModal(id, name, ip, user) {
    document.getElementById('edit-esxi-id').value = id;
    document.getElementById('edit-esxi-name').value = name;
    document.getElementById('edit-esxi-ip').value = ip;
    document.getElementById('edit-esxi-user').value = user;
    document.getElementById('edit-esxi-pass').value = '';
    document.getElementById('edit-esxi-status').classList.add('hidden');
    document.getElementById('esxi-edit-modal').classList.remove('hidden');
}

function closeEsxiEditModal() {
    document.getElementById('esxi-edit-modal').classList.add('hidden');
}

async function testEsxiConnection() {
    const status = document.getElementById('edit-esxi-status');
    status.classList.remove('hidden');
    status.className = 'text-sm font-medium mb-4 rounded px-3 py-2 bg-blue-500/10 text-blue-400 border border-blue-500/20';
    status.textContent = 'Testing connection...';
    
    try {
        const res = await fetch('/api/v1/hosts/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                host_ip: document.getElementById('edit-esxi-ip').value,
                username: document.getElementById('edit-esxi-user').value,
                password: document.getElementById('edit-esxi-pass').value
            })
        });
        const data = await res.json();
        if (data.ok) {
            status.className = 'text-sm font-medium mb-4 rounded px-3 py-2 bg-green-500/10 text-green-400 border border-green-500/20';
            status.textContent = '✓ ' + data.message;
        } else {
            status.className = 'text-sm font-medium mb-4 rounded px-3 py-2 bg-red-500/10 text-red-400 border border-red-500/20';
            status.textContent = '✗ ' + data.message;
        }
    } catch (e) {
        status.className = 'text-sm font-medium mb-4 rounded px-3 py-2 bg-red-500/10 text-red-400 border border-red-500/20';
        status.textContent = 'Error: ' + e;
    }
}

async function saveEsxiHost() {
    const id = document.getElementById('edit-esxi-id').value;
    try {
        const res = await fetch('/api/v1/hosts/' + id, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                name: document.getElementById('edit-esxi-name').value,
                host_ip: document.getElementById('edit-esxi-ip').value,
                username: document.getElementById('edit-esxi-user').value,
                password: document.getElementById('edit-esxi-pass').value || null
            })
        });
        if (res.ok) {
            window.location.reload();
        } else {
            const data = await res.json();
            alert('Failed to update host: ' + (data.detail || res.statusText));
        }
    } catch (e) {
        alert('Error: ' + e);
    }
}
</script>
"""

if "esxi-edit-modal" not in content:
    content += modal_html

# Custom jinja escapejs filter doesn't exist by default in FastAPI Jinja,
# so we should use default filter or manual. Wait, let's replace escapejs with nothing if it fails.
# Actually, Jinja2 has `tojson` but maybe it's simpler to just not escape if we trust the DB, 
# or just use standard HTML escaping. `escapejs` might cause a Jinja error if not defined.
content = content.replace("escapejs", "e")

with open('templates/partials/settings_tab.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("UI updated successfully")
