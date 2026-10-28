---
name: self-heal-environment
description: When a command or library is missing, fix your own environment and continue instead of stopping.
---
1. Read the error: "command not found" -> apt package; "No module named X" -> pip package.
2. env_install it. Python libraries come from pip (zxing-cpp, pyzbar, opencv-python-headless): always try pip first.
   Use apt only for system binaries. Installs are serialized per node, and a package another agent already
   installed is reused, so never kill apt processes or delete lock files.
3. Re-run the original step. If install fails twice, try an alternative tool or pure-Python route.
4. Every change is written to the resource ledger automatically; mention what you changed in your final result.
