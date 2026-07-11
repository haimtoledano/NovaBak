import re

html_to_replace = '''<div id="storage-target-modal" class="modal-overlay hidden">
    <div class="modal-content" style="max-width: 600px;">'''

new_html = '''<div id="storage-target-modal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm">
    <div class="w-full max-w-2xl rounded-xl shadow-2xl p-6 relative" style="background-color: var(--bg-card); color: var(--text-main); border: 1px solid var(--border-color);">'''

with open('templates/partials/settings_tab.html', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(html_to_replace, new_html)

# Also fix the input borders!
# The inputs in my modal lack borders. I should add `border rounded` to them.
content = content.replace('class="w-full py-2 px-3 font-mono text-sm mt-1"', 'class="w-full py-2 px-3 font-mono text-sm mt-1 border rounded" style="border-color: var(--border-color); background: transparent; color: var(--text-main)"')

with open('templates/partials/settings_tab.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Modal styling fixed")
