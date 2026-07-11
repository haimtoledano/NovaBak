import re

with open('templates/partials/settings_tab.html', 'r', encoding='utf-8') as f:
    content = f.read()

new_storage_panel = '''<!-- Storage Targets -->
                        <div id="settings-panel-storage" class="settings-panel hidden">
                            <div class="flex justify-between items-center mb-4">
                                <div>
                                    <h2 class="settings-panel-title mb-0">Storage Targets</h2>
                                    <p class="settings-panel-desc">Manage multiple backup destinations.</p>
                                </div>
                                <button type="button" onclick="openStorageTargetModal()" class="btn-primary px-4 py-2 text-sm font-semibold flex items-center gap-2">
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
                                    Add Target
                                </button>
                            </div>

                            <div class="card overflow-hidden">
                                <table class="min-w-full divide-y" style="border-color: var(--border-color)">
                                    <thead class="bg-opacity-50" style="background: var(--bg-hover)">
                                        <tr>
                                            <th class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style="color: var(--text-muted)">Name</th>
                                            <th class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style="color: var(--text-muted)">Type</th>
                                            <th class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style="color: var(--text-muted)">Path / Endpoint</th>
                                            <th class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider" style="color: var(--text-muted)">Default</th>
                                            <th class="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wider" style="color: var(--text-muted)">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody id="storage-targets-list" class="divide-y" style="border-color: var(--border-color)">
                                        <tr><td colspan="5" class="text-center py-4 text-sm" style="color: var(--text-muted)">Loading...</td></tr>
                                    </tbody>
                                </table>
                            </div>

                            <!-- Encryption Settings -->
                            <h2 class="settings-panel-title mt-8">Global Encryption</h2>
                            <p class="settings-panel-desc">Set a global AES-256 encryption key for all backups.</p>
                            <div class="card p-5">
                                <form action="/api/v1/config" method="PUT" id="settings-encryption-form" class="flex gap-4 items-end" onsubmit="saveEncryptionKey(event)">
                                    <div class="flex-1">
                                        <span class="input-label">Encryption Key (Leave empty to disable)</span>
                                        <input type="password" id="encryption_key_input" placeholder="Base64 Encoded Key (or raw string)" class="w-full py-2 px-3 font-mono text-sm mt-1">
                                    </div>
                                    <button type="submit" class="btn-primary px-6 py-2 text-sm font-semibold">Save Key</button>
                                </form>
                            </div>
                        </div>'''

content = re.sub(r'<!-- Storage -->.*?</div>\s*</div>\s*</div>', new_storage_panel, content, flags=re.DOTALL)
with open('templates/partials/settings_tab.html', 'w', encoding='utf-8') as f:
    f.write(content)
print('Replaced Storage panel in settings_tab.html')
