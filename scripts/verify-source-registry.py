#!/usr/bin/env python3
"""Verify imported bytes and all declared local integration resources."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    data = json.loads((root / "avrone/static/source-registry.json").read_text())
    overrides = {r["path"]: r["sha256"] for r in data["local_changes"]}
    entries = [f for s in data["sources"] for f in s.get("files", [])] + data.get("local_resources", [])
    checked, problems = set(), []
    for entry in entries:
        name = entry["path"]
        if name in checked:
            problems.append("duplicate resource: " + name)
            continue
        checked.add(name)
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            problems.append("missing or invalid resource: " + name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != overrides.get(name, entry["sha256"]):
            problems.append("digest mismatch: " + name)
    undeclared = set(overrides) - checked
    problems.extend("undeclared override: " + p for p in sorted(undeclared))
    return {"ok": not problems, "resources_checked": len(checked), "problems": problems}


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
