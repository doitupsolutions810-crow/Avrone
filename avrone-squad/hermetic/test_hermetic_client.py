#!/usr/bin/env python3
import os, subprocess, sys, textwrap, hashlib, pytest

CLIENT = "avrone-squad/hermetic/harness_client.py"

def test_client_syntax():
    r = subprocess.run([sys.executable, "-m", "py_compile", CLIENT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

def test_pinned_sha_format():
    p = "avrone-squad/hermetic/PINNED_SHA"
    if os.path.exists(p):
        sha = open(p).read().strip()
        assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)

def test_no_harness_paths_in_evolve_payloads():
    banned = ["avrone-harness", "hermetic/PINNED_SHA", "invariants/"]
    payloads = []
    d = "avrone-squad/evolve-payloads"
    if os.path.isdir(d):
        for fn in os.listdir(d):
            payloads.append(open(os.path.join(d, fn)).read())
    for pl in payloads:
        for b in banned:
            assert b not in pl, f"payload contains banned harness path: {b}"

def test_sha_mismatch_aborts():
    code = open(CLIENT).read()
    env = os.environ.copy()
    env["HARNESS_PINNED_SHA"] = "/dev/null"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode != 0 or "Error" in r.stderr or "Traceback" in r.stderr

def test_invariants_dir_documented():
    readme = open("avrone-squad/hermetic/harness_repo.md").read()
    for inv in ["test_self_metering", "test_es_fixed_point", "test_idle_fraction_floor",
                "test_relaxation", "test_gate_threshold", "test_es_clamp",
                "test_circuit_breaker", "test_no_self_touch"]:
        assert inv in readme, f"invariant {inv} missing from harness docs"
