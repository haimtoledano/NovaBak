import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace wizardSaveStorage to save to /api/v1/storage-targets
new_wizardSaveStorage = '''    async function wizardSaveStorage() {
        const status = document.getElementById('wizard-storage-status');
        if (status) {
            status.classList.remove('hidden');
            status.textContent = 'Saving storage target...';
            status.style.background = 'rgba(59,130,246,0.08)';
            status.style.color = 'var(--text-muted)';
        }
        const payload = wizardGetStoragePayload();
        payload.name = "Primary Storage";
        payload.is_default = true;
        
        await wizardApi('POST', '/api/v1/storage-targets', payload);
        
        if (status) {
            status.textContent = '✓ Storage target saved successfully';
            status.style.background = 'rgba(34,197,94,0.12)';
            status.style.color = '#4ade80';
        }
    }'''

content = re.sub(r'async function wizardSaveStorage\(\) \{.*?\n    \}', new_wizardSaveStorage, content, flags=re.DOTALL)

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Setup wizard JS updated")
