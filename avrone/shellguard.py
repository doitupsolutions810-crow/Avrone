"""Opt-in command guard for hosts without a container sandbox.

AVRONE_SHELL_GUARD=on refuses clearly destructive shell commands before they run
(the LocalRunner has no OS sandbox). It is a tripwire for obvious mistakes and
prompt-injected one-liners, NOT a security boundary: use AVRONE_RUNNER=docker
for real isolation. Extra regexes: AVRONE_SHELL_DENY (semicolon-separated).
"""
from __future__ import annotations

import os
import re

DEFAULT_DENY = [
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+[^|;&]*\bof=/dev/",
    r">\s*/dev/(sd|nvme|vd|hd)\w*",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",              # fork bomb
    r"\bcurl\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b",                # curl | sh
    r"\bwget\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b",
    r"(^|[;&|(]\s*|\bsudo\s+)(shutdown|reboot|halt|poweroff)(\s|$)",   # only as a command, not as an argument
    r"\bchmod\s+(-[a-zA-Z]+\s+)*[0-7]*777\s+/(\s|$)",
    r"\bgit\s+push\b[^;&|]*(--force|-f\b)",
]


# Targets that must never be recursively removed: root, home, a bare wildcard, or their children-of-root forms.
_BAD_TARGET = re.compile(r"^(/+\*?|~/*\*?|\$HOME/*\*?|\$\{HOME\}/*\*?|\*|\.|\.\.|/(bin|boot|dev|etc|lib\w*|proc|root|sbin|sys|usr|var|home|opt)/*\*?)$")


def _rm_hits(cmd: str) -> bool:
    """True if any rm segment is recursive and has a catastrophic target (checks every argument)."""
    for seg in re.split(r"[;&|]+|\$\(|`", cmd):
        toks = seg.replace("\\\n", " ").split()
        while toks and toks[0] in ("sudo", "command", "exec", "nohup", "time"):
            toks = toks[1:]
        if not toks or toks[0] != "rm":
            continue
        args = toks[1:]
        flags = [a for a in args if a.startswith("-")]
        recursive = any(a in ("--recursive",) or (not a.startswith("--") and ("r" in a or "R" in a)) for a in flags)
        if recursive and any(_BAD_TARGET.match(a.strip("\"'")) for a in args if not a.startswith("-")):
            return True
    return False


def enabled() -> bool:
    return os.getenv("AVRONE_SHELL_GUARD", "off").strip().lower() in ("1", "on", "true", "yes")


def patterns() -> list[re.Pattern]:
    extra = [p.strip() for p in os.getenv("AVRONE_SHELL_DENY", "").split(";") if p.strip()]
    return [re.compile(p, re.I) for p in DEFAULT_DENY + extra]


def check(cmd: str) -> str | None:
    """Return the matched pattern if `cmd` must be refused, else None."""
    if not enabled():
        return None
    if _rm_hits(cmd):
        return "recursive rm of a protected path"
    for rx in patterns():
        if rx.search(cmd):
            return rx.pattern
    return None
