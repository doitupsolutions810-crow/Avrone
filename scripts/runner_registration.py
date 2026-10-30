#!/usr/bin/env python3
"""Register a swarm-vm-runner minted token against an Avrone repo.

This is the effector half of the boot-and-register pair. mac-local-squad.sh
boots the Apple Silicon VM and starts Ollama; this module takes the token
that runner mints, verifies it against the attested-history ledger chain,
and only then calls gh to register it as a self-hosted runner.

Five fail-closed paths, none of which ever produce a fake green:
  HOLD_UNMEASURED  — ledger file missing or unreadable
  HOLD_POISONED    — ledger chain fails integrity check
  HOLD_UNLINEAGED  — no token supplied
  PASS             — token verifies at chain depth >= 1
  HOLD_UNREGISTERED — gh CLI absent or registration rejected

A registered runner is the first piece of infrastructure the compile can
boot that it did not build. Every other layer was authored inside the
perimeter; a self-hosted runner is external compute the system recruits.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LEDGER_ENV = "ATTESTED_HISTORY_LEDGER"
DEFAULT_LEDGER = Path("tmp/emergent-system/attested-history/index.jsonl")


@dataclass
class LedgerEntry:
    digest: str
    parent: str
    kind: str
    payload_hash: str
    line: int


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def load_ledger(path: Path) -> list[LedgerEntry]:
    if not path.is_file():
        raise FileNotFoundError(f"ledger missing: {path}")
    entries: list[LedgerEntry] = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ledger line {i} not valid JSON") from exc
        entries.append(
            LedgerEntry(
                digest=str(obj.get("digest", "")),
                parent=str(obj.get("parent", "")),
                kind=str(obj.get("kind", "")),
                payload_hash=str(obj.get("payload_hash", "")),
                line=i,
            )
        )
    return entries


def verify_chain(entries: list[LedgerEntry]) -> tuple[bool, str]:
    """Recompute each entry's digest from parent+kind+payload_hash.

    Returns (ok, reason). On failure, reason names the breaking line.
    """
    prev = ""
    for e in entries:
        if e.parent != prev:
            return False, f"parent mismatch at line {e.line}: expected {prev!r}, got {e.parent!r}"
        recomputed = _sha256(f"{e.parent}|{e.kind}|{e.payload_hash}")
        if recomputed != e.digest:
            return False, f"digest mismatch at line {e.line}"
        prev = e.digest
    return True, "chain intact"


def verify_token_against_ledger(token: str, ledger_path: Path | None = None) -> dict[str, Any]:
    """Verify a minted runner token against the attested-history chain."""
    path = ledger_path or Path(os.environ.get(LEDGER_ENV, DEFAULT_LEDGER))
    try:
        entries = load_ledger(path)
    except FileNotFoundError:
        return {"ok": False, "state": "HOLD_UNMEASURED", "reason": f"ledger missing: {path}"}
    except ValueError as exc:
        return {"ok": False, "state": "HOLD_POISONED", "reason": str(exc)}
    ok, reason = verify_chain(entries)
    if not ok:
        return {"ok": False, "state": "HOLD_POISONED", "reason": reason}
    if not token or not token.strip():
        return {"ok": False, "state": "HOLD_UNLINEAGED", "reason": "no token supplied"}
    token_hash = _sha256(token.strip())
    for e in reversed(entries):
        if e.payload_hash == token_hash:
            return {
                "ok": True,
                "state": "PASS",
                "chain_depth": e.line,
                "matched_line": e.line,
                "kind": e.kind,
            }
    return {
        "ok": False,
        "state": "HOLD_UNLINEAGED",
        "reason": "token not present in ledger chain",
        "chain_depth": len(entries),
    }


def register_runner(token: str, repo: str, name: str, labels: list[str] | None = None) -> dict[str, Any]:
    """Verify token, then register via gh. Fails closed on every path."""
    labels = labels or ["self-hosted", "avrone", "emergent-compile"]
    verdict = verify_token_against_ledger(token)
    if not verdict["ok"]:
        return verdict
    if shutil.which("gh") is None:
        return {"ok": False, "state": "HOLD_UNREGISTERED", "reason": "gh CLI not installed"}
    cmd = [
        "gh", "runner", "add",
        "--repo", repo,
        "--name", name,
        "--token", token.strip(),
    ]
    for lab in labels:
        cmd.extend(["--label", lab])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "state": "HOLD_UNREGISTERED", "reason": "gh registration timed out"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "state": "HOLD_UNREGISTERED",
            "reason": proc.stderr.strip() or proc.stdout.strip() or "gh rejected registration",
        }
    return {
        "ok": True,
        "state": "PASS",
        "registered_name": name,
        "labels": labels,
        "repo": repo,
        "chain_depth": verdict["chain_depth"],
    }


def self_test() -> str:
    """Offline self-test: synthetic ledger, mocked gh."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "index.jsonl"
        root_payload = _sha256("root-anchor")
        root_digest = _sha256(f"|{ 'root' }|{root_payload}")
        token = "minted-runner-token-abc123"
        token_hash = _sha256(token)
        entry2_digest = _sha256(f"{root_digest}|runner-mint|{token_hash}")
        ledger.write_text(
            json.dumps({"digest": root_digest, "parent": "", "kind": "root", "payload_hash": root_payload}) + "\n"
            + json.dumps({"digest": entry2_digest, "parent": root_digest, "kind": "runner-mint", "payload_hash": token_hash}) + "\n",
            encoding="utf-8",
        )
        v = verify_token_against_ledger(token, ledger)
        assert v["ok"] is True and v["chain_depth"] == 2, v
        v_bad = verify_token_against_ledger("wrong-token", ledger)
        assert v_bad["state"] == "HOLD_UNLINEAGED", v_bad
        missing = Path(td) / "nope.jsonl"
        v_miss = verify_token_against_ledger(token, missing)
        assert v_miss["state"] == "HOLD_UNMEASURED", v_miss
        # corrupt middle link
        bad = Path(td) / "bad.jsonl"
        bad.write_text(
            json.dumps({"digest": root_digest, "parent": "", "kind": "root", "payload_hash": root_payload}) + "\n"
            + json.dumps({"digest": "deadbeef", "parent": root_digest, "kind": "runner-mint", "payload_hash": token_hash}) + "\n",
            encoding="utf-8",
        )
        v_corr = verify_token_against_ledger(token, bad)
        assert v_corr["state"] == "HOLD_POISONED", v_corr
        # gh absent
        orig = shutil.which
        shutil.which = lambda cmd: None if cmd == "gh" else orig(cmd)  # type: ignore
        try:
            v_gh = register_runner(token, "doitupsolutions810-crow/Avrone", "avrone-sbx-1", ledger_path=ledger)
            assert v_gh["state"] == "HOLD_UNREGISTERED", v_gh
        finally:
            shutil.which = orig  # type: ignore
    return "VERIFIED"


if __name__ == "__main__":
    print(self_test())
