import sys
import os

try:
    import winreg
    winreg_str = str(winreg)
except Exception as e:
    winreg_str = f"FAILED: {e}"

output = f"""
sys.platform: {sys.platform}
os.name: {os.name}
sys.executable: {sys.executable}
sys.path: {sys.path}
sys.builtin_module_names: {sys.builtin_module_names}
winreg: {winreg_str}
"""

with open(r"C:\VMBackup\VMBackup\data\diagnostic_output.txt", "w") as f:
    f.write(output)
