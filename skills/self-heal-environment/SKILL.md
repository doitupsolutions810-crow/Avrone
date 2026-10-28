---
name: self-heal-environment
description: When a command or library is missing, fix your own environment and continue instead of stopping.
---
1. Read the error: "command not found" -> apt package; "No module named X" -> pip package.
2. env_install it (pip first for Python libs, apt for system tools; apt falls back to sudo -n).
3. Re-run the original step. If install fails twice, try an alternative tool or pure-Python route.
4. Every change is written to the resource ledger automatically; mention what you changed in your final result.
