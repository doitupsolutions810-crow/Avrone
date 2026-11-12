#!/usr/bin/env python3
"""Live Chat -> Ollama -> Trinity squad -> Docker isolation smoke test."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "docs" / "local-ollama" / "local-ollama-sandbox.env"

def load_profile(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            os.environ.setdefault(key, value.strip("\"'"))

def request_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(3000).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not reach {url}: {exc}") from exc

def safe_agent_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    return value.strip("-_")[:48].strip("-_") or "agent"

def inspect_agent_homes(nonce: str, expected_hashes: set[str]) -> list[dict[str, Any]]:
    """Inspect live agent homes that contain this run's marker artifact."""
    ids_raw = subprocess.run(["docker", "ps", "-aq", "--filter", "label=avrone.agent-home=1"],
        check=True, capture_output=True, text=True, timeout=20).stdout
    ids = [x.strip() for x in ids_raw.splitlines() if x.strip()]
    if not ids:
        raise RuntimeError("No persistent AVRONE agent-home containers exist after the squad run.")
    raw = subprocess.run(["docker", "inspect", *ids], check=True, capture_output=True,
        text=True, timeout=30).stdout
    containers = json.loads(raw)
    output = []
    actual_hashes: set[str] = set()
    result_values: set[str] = set()
    expected_uid = os.environ.get("AVRONE_AGENT_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    expected_gid = os.environ.get("AVRONE_AGENT_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))
    marker = f"sandbox-smoke-{nonce}.txt"
    for c in containers:
        cfg, host = c.get("Config") or {}, c.get("HostConfig") or {}
        labels = cfg.get("Labels") or {}
        aid = labels.get("avrone.agent")
        mounts = c.get("Mounts") or []
        if len(mounts) != 1 or mounts[0].get("Destination") != "/agent":
            continue
        source = Path(str(mounts[0].get("Source", "")))
        artifact = source / marker
        try:
            if artifact.is_symlink() or not artifact.is_file():
                continue
            content = artifact.read_text(encoding="utf-8")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except (OSError, UnicodeError):
            continue
        if digest not in expected_hashes:
            raise RuntimeError(f"Agent {aid} artifact hash does not match the sha256sum output recorded by shell.")
        actual_hashes.add(digest)
        present_values = {v for v in ("687871", "660886") if v in content}
        result_values.update(present_values)
        user = str(cfg.get("User") or "")
        checks = {
            "container_running": (c.get("State") or {}).get("Running") is True,
            "non_root_user": bool(user.split(":", 1)[0].isdigit() and int(user.split(":", 1)[0]) > 0),
            "user_matches_workspace_owner": user == f"{expected_uid}:{expected_gid}",
            "readonly_rootfs": host.get("ReadonlyRootfs") is True,
            "capabilities_dropped": "ALL" in (host.get("CapDrop") or []),
            "no_new_privileges": any("no-new-privileges" in str(x) for x in (host.get("SecurityOpt") or [])),
            "network_is_none": host.get("NetworkMode") == "none",
            "memory_bounded": int(host.get("Memory") or 0) > 0,
            "swap_bounded_to_memory": int(host.get("MemorySwap") or 0) == int(host.get("Memory") or 0),
            "cpu_bounded": int(host.get("NanoCpus") or 0) > 0,
            "pids_bounded": int(host.get("PidsLimit") or 0) > 0,
            "one_private_mount": len(mounts) == 1 and mounts[0].get("Destination") == "/agent",
            "private_agent_home_source": "/agents/" in str(mounts[0].get("Source", "")),
            "working_dir_is_agent_home": cfg.get("WorkingDir") == "/agent",
            "home_is_agent_home": "HOME=/agent" in (cfg.get("Env") or []),
            "artifact_is_regular_file": artifact.is_file() and not artifact.is_symlink(),
            "artifact_hash_matches_shell": digest in expected_hashes,
            "artifact_contains_expected_result": bool(present_values),
        }
        output.append({"name": c.get("Name", "").lstrip("/"), "agent_id": aid,
            "user": user, "network": host.get("NetworkMode"), "artifact": marker,
            "artifact_sha256": digest, "artifact_results": sorted(present_values),
            "memory_bytes": host.get("Memory"), "memory_swap_bytes": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"), "pids_limit": host.get("PidsLimit"),
            "mounts": [{"destination": m.get("Destination")} for m in mounts], "checks": checks})
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Agent container {c.get('Name')} failed isolation/artifact checks: {failed}")
    if len(output) < 2:
        raise RuntimeError(f"Expected two different private agent homes to contain this run's artifact; found {len(output)}.")
    if result_values != {"687871", "660886"}:
        raise RuntimeError(f"Private agent files did not independently contain both expected results: {sorted(result_values)}")
    if actual_hashes != expected_hashes:
        raise RuntimeError("The file hashes observed in private homes do not match the distinct hashes emitted by shell.")
    return output

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=os.environ.get("AVRONE_LOCAL_BASE_URL", "http://127.0.0.1:8710"))
    ap.add_argument("--profile", type=Path, default=PROFILE)
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()
    load_profile(args.profile)
    parsed = urllib.parse.urlparse(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("BLOCKED: smoke test only permits a loopback AVRONE API URL.")
    base = args.base_url.rstrip("/")
    status = request_json(base + "/api/status")
    if status.get("provider") != "ollama" or status.get("runner") != "docker":
        raise SystemExit(f"BLOCKED: expected provider=ollama and runner=docker; observed {status.get('provider')} / {status.get('runner')}.")
    if os.environ.get("AVRONE_ALLOW_NETWORK", "false").lower() not in {"false", "0", "no", "off"}:
        raise SystemExit("BLOCKED: profile must set AVRONE_ALLOW_NETWORK=false.")
    if os.environ.get("AVRONE_AGENT_CONTAINER", "off").lower() != "persistent":
        raise SystemExit("BLOCKED: profile must set AVRONE_AGENT_CONTAINER=persistent.")
    nonce = uuid.uuid4().hex[:10]
    task = (
        "Run a live two-worker tool-use test. Split into exactly two independent workers. "
        "Each worker MUST call the shell tool in its own private working directory; do not use the python tool "
        "instead of shell, and never use shared/ or the workspace root. Worker A must calculate 731*941 "
        "(expected 687871). Worker B must calculate 982*673 (expected 660886). "
        f"Each worker must create sandbox-smoke-{nonce}.txt inside its own current directory, write the assigned "
        "calculation and result with a Python one-liner invoked through shell, then run pwd and sha256sum on that file. "
        "Return the actual shell output and hash. Lead must compare both results."
    )
    result = request_json(base + "/api/chat",
        {"message": task, "mode": "squad", "squad_size": 2, "dispatch": "local"}, timeout=args.timeout)
    run_id = result.get("run_id") if isinstance(result, dict) else None
    if not run_id:
        raise RuntimeError("Chat returned no run_id; this is not a verified run.")
    events = request_json(base + "/api/runs/" + urllib.parse.quote(str(run_id), safe=""))
    if not isinstance(events, list):
        raise RuntimeError("Run log endpoint did not return an event array.")
    spawns = [e for e in events if e.get("kind") == "agent.spawn"]
    calls = [e for e in events if e.get("kind") == "tool.call" and (e.get("data") or {}).get("tool") == "shell"]
    results = [e for e in events if e.get("kind") == "tool.result" and (e.get("data") or {}).get("tool") == "shell"]
    spawn_roles = {e.get("agent"): (e.get("data") or {}).get("role", "") for e in spawns}
    shell_agents = {e.get("agent") for e in calls if e.get("agent")}
    if len(spawns) < 2 or len(shell_agents) < 2:
        raise RuntimeError(f"Expected 2 spawned workers and 2 distinct shell callers; got {len(spawns)} and {sorted(shell_agents)}.")
    matched = [e for e in results if e.get("agent") in shell_agents]
    if len({e.get("agent") for e in matched}) < 2:
        raise RuntimeError("Two distinct shell tool results were not recorded in Eternal CodeX.")
    outputs_by_agent = {
        agent: "\\n".join(str((e.get("data") or {}).get("output", "")) for e in matched if e.get("agent") == agent)
        for agent in shell_agents
    }
    joined = "\\n".join(outputs_by_agent.values())
    if "687871" not in joined or "660886" not in joined:
        raise RuntimeError("Shell outputs did not prove both expected arithmetic results.")
    hash_pattern = re.compile(r"(?m)^([0-9a-fA-F]{64})\\s+.*sandbox-smoke-" + re.escape(nonce) + r"\\.txt(?:\\s|$)")
    reported_hashes = {
        match.group(1).lower()
        for output in outputs_by_agent.values()
        for match in hash_pattern.finditer(output)
    }
    if len(reported_hashes) < 2:
        raise RuntimeError("Two distinct sha256sum results for the run marker files were not recorded by shell.")
    errors = [e for e in events if e.get("kind") == "error" and (not e.get("agent") or e.get("agent") in shell_agents)]
    if errors:
        raise RuntimeError(f"Worker errors were recorded: {errors[:3]}")
    containers = inspect_agent_homes(nonce, reported_hashes)
    required = {"run.start", "lead.merge", "run.done"}
    present = {e.get("kind") for e in events}
    if not required.issubset(present):
        raise RuntimeError(f"Missing Trinity lifecycle events: {sorted(required - present)}")
    report = {
        "status": "PASS", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": base, "run_id": run_id, "provider": status.get("provider"),
        "model": status.get("model"), "runner": status.get("runner"),
        "events": {"agent_spawn": len(spawns), "shell_tool_calls": len(calls),
            "shell_tool_results": len(results), "agents_using_shell": sorted(shell_agents),
            "lifecycle": sorted(present & required)},
        "agent_containers": containers, "lead_answer": str(result.get("answer", ""))[-4000:],
    }
    outdir = ROOT / "data" / "local-ollama-smoke"
    outdir.mkdir(parents=True, exist_ok=True)
    report_path = outdir / f"{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report_path": str(report_path)}, indent=2))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
