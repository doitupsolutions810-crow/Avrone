#!/usr/bin/env python3
"""Read-only harness client. Fetches the pinned invariant suite and runs it
as gate stage-one. The evolve loop never writes here."""
import hashlib, os, subprocess, sys, textwrap

PINNED_SHA_PATH = os.environ.get("HARNESS_PINNED_SHA", "avrone-squad/hermetic/PINNED_SHA")
HARNESS_URL = os.environ.get("HARNESS_URL", "https://github.com/doitupsolutions810-crow/avrone-harness")

def read_pinned_sha():
    with open(PINNED_SHA_PATH) as f:
        return f.read().strip()

def fetch_harness(sha):
    """Clone harness repo at exact SHA into a temp dir. Read-only."""
    dest = f"/tmp/avrone-harness-{sha[:8]}"
    if os.path.exists(dest):
        return dest
    r = subprocess.run(["git", "clone", "--depth", "1", HARNESS_URL, dest],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"harness fetch failed: {r.stderr}")
    r = subprocess.run(["git", "-C", dest, "checkout", sha],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"harness SHA pin failed: {r.stderr}")
    return dest

def verify_sha(dest, expected):
    """Content-hash the harness tree and compare to the pinned value."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(dest):
        dirs.sort(); files.sort()
        for fn in files:
            p = os.path.join(root, fn)
            with open(p, "rb") as f:
                h.update(fn.encode()); h.update(f.read())
    actual = h.hexdigest()
    if actual != expected:
        raise RuntimeError(f"harness SHA mismatch: expected {expected[:16]}… got {actual[:16]}…")
    return actual

def run_invariants(dest):
    """Execute the invariant suite. Returns (passed: bool, output: str)."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "invariants/"],
                       cwd=dest, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr

def gate_stage_one(evolve_payload):
    """Full stage-one: pin check, fetch, hash verify, run invariants.
    Aborts before any application test if the harness is stale or tampered."""
    expected = read_pinned_sha()
    dest = fetch_harness(expected)
    verify_sha(dest, expected)
    ok, out = run_invariants(dest)
    if not ok:
        return False, f"INVARIANT FAILURE\n{out}"
    return True, "harness green, proceeding to stage-two"

if __name__ == "__main__":
    ok, msg = gate_stage_one(None)
    print(msg)
    sys.exit(0 if ok else 1)
