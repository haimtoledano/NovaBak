import sys
import os
import types
import traceback

# Redirect stdout and stderr to a file internally
log_file_path = r"C:\VMBackup\VMBackup\data\trace_session_output.txt"
os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
sys.stdout = open(log_file_path, "w", encoding="utf-8")
sys.stderr = sys.stdout

# Ensure we are in the application directory
sys.path.append(r"C:\VMBackup\VMBackup")
os.chdir(r"C:\VMBackup\VMBackup")

import models

class TraceModule(types.ModuleType):
    def __init__(self, original_module):
        super().__init__(original_module.__name__)
        self.__dict__.update(original_module.__dict__)
        
    def __setattr__(self, name, value):
        if name == "SessionLocal":
            print(f"SessionLocal set to {value}!")
            traceback.print_stack()
        super().__setattr__(name, value)

sys.modules['models'] = TraceModule(models)

print("Importing services.backup_ops...")
from services import backup_ops
print("Done!")
