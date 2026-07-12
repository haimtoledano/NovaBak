import re

storage_dropdown = """
    <div class="mb-2 pt-2" style="border-top: 1px dashed var(--border-color)">
        <span class="input-label text-[10px]">Storage Target</span>
        <select name="storage_target_id"
                class="w-full py-1.5 px-2 text-xs font-semibold rounded border"
                style="border-color: var(--border-color); background: var(--bg-card); color: var(--text-main)"
                onchange="autoSaveJob({{ vm.id }}, this.form)">
            <option value="" {% if not vm.storage_target_id %}selected{% endif %}>Default</option>
            {% for target in storage_targets %}
            <option value="{{ target.id }}" {% if vm.storage_target_id == target.id %}selected{% endif %}>{{ target.name }}</option>
            {% endfor %}
        </select>
    </div>
"""

storage_dropdown_inline = """
        <div class="flex flex-col gap-1 w-24">
            <span class="input-label text-[10px]">Storage Target</span>
            <select name="storage_target_id" class="px-2 py-1 text-center font-mono text-sm border rounded" style="border-color:var(--border-color);background:var(--bg-card);color:var(--text-main)" onchange="debounceJobSave({{ vm.id }}, this.form)">
                <option value="" {% if not vm.storage_target_id %}selected{% endif %}>Default</option>
                {% for target in storage_targets %}
                <option value="{{ target.id }}" {% if vm.storage_target_id == target.id %}selected{% endif %}>{{ target.name }}</option>
                {% endfor %}
            </select>
        </div>
"""

# 1. job_schedule_form_popover.html
with open('templates/partials/job_schedule_form_popover.html', 'r', encoding='utf-8') as f:
    popover = f.read()

# Insert before <div class="pt-2 mb-2" style="border-top: 1px dashed var(--border-color)">
popover = popover.replace('<div class="pt-2 mb-2" style="border-top: 1px dashed var(--border-color)">',
                          storage_dropdown + '\n    <div class="pt-2 mb-2" style="border-top: 1px dashed var(--border-color)">')

with open('templates/partials/job_schedule_form_popover.html', 'w', encoding='utf-8') as f:
    f.write(popover)


# 2. job_schedule_form_card.html
with open('templates/partials/job_schedule_form_card.html', 'r', encoding='utf-8') as f:
    card = f.read()

# Insert before Mode toggle (similar structure)
card = card.replace('<div class="pt-2 mb-2" style="border-top: 1px dashed var(--border-color)">',
                    storage_dropdown + '\n    <div class="pt-2 mb-2" style="border-top: 1px dashed var(--border-color)">')

with open('templates/partials/job_schedule_form_card.html', 'w', encoding='utf-8') as f:
    f.write(card)


# 3. job_schedule_form.html (inline)
with open('templates/partials/job_schedule_form.html', 'r', encoding='utf-8') as f:
    inline = f.read()

# Insert after retention count div
retention_div = """        <div class="flex flex-col gap-1 w-16">
            <span class="input-label text-[10px]">Keep</span>
            <input type="number" name="retention_count" value="{{ vm.retention_count }}" min="1" max="60"
                   class="w-full px-2 py-1 text-center font-mono text-sm border rounded"
                   style="border-color: var(--border-color); background: var(--bg-card); color: var(--text-main)"
                   oninput="debounceJobSave({{ vm.id }}, this.form)">
        </div>"""

inline = inline.replace(retention_div, retention_div + storage_dropdown_inline)

with open('templates/partials/job_schedule_form.html', 'w', encoding='utf-8') as f:
    f.write(inline)

print("Updated partials with Storage Target selection")
