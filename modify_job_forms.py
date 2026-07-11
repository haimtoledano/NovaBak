import re

file_paths = ['templates/partials/job_schedule_form.html', 'templates/partials/job_schedule_form_popover.html']

storage_target_html = '''
    <div class="field-label-row mt-2">
        <span class="text-xs font-semibold" style="color: var(--text-muted)">Storage Target</span>
    </div>
    <select name="storage_target_id" class="w-full px-2 py-1 mt-1 text-xs font-semibold rounded border" style="border-color: var(--border-color); background: var(--bg-card); color: var(--text-main)">
        <option value="">-- Default Storage --</option>
        {% for target in storage_targets %}
            <option value="{{ target.id }}" {% if vm.storage_target_id == target.id %}selected{% endif %}>{{ target.name }}</option>
        {% endfor %}
    </select>
'''

for file_path in file_paths:
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'storage_target_id' not in content:
        content = content.replace('<!-- End advanced row -->', storage_target_html + '\n<!-- End advanced row -->')
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Modified {file_path}")
    else:
        print(f"Already modified {file_path}")
