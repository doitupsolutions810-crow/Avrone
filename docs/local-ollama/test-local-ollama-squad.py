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

def inspect_agent_homes(nonce: str, expected_artifacts: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Inspect exactly the two worker homes associated with this run's shell events."""
    ids_raw = subprocess.run(["docker", "ps", "-aq", "--filter", "label=avrone.agent-home=1"],
        check=True, capture_output=True, text=True, timeout=20).stdout
    ids = [x.strip() for x in ids_raw.splitlines() if x.strip()]
    if not ids:
        raise RuntimeError("No persistent AVRONE agent-home containers exist after the squad run.")
    raw = subprocess.run(["docker", "inspect", *ids], check=True, capture_output=True,
        text=True, timeout=30).stdout
    containers = json.loads(raw)
    output = []
    found_agent_ids: set[str] = set()
    marker = f"sandbox-smoke-{nonce}.txt"
    container_path = f"/agent/{marker}"
    expected_uid = os.environ.get("AVRONE_AGENT_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    expected_gid = os.environ.get("AVRONE_AGENT_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))
    for c in containers:
        cfg, host = c.get("Config") or {}, c.get("HostConfig") or {}
        labels = cfg.get("Labels") or {}
        agent_id = labels.get("avrone.agent")
        if labels.get("avrone.agent-home") != "1" or agent_id not in expected_artifacts:
            continue
        mounts = c.get("Mounts") or []
        if len(mounts) != 1 or mounts[0].get("Destination") != "/agent":
            continue
        if (c.get("State") or {}).get("Running") is not True:
            continue
        cid = str(c.get("Id") or "")
        if not cid:
            continue
        is_file = subprocess.run(["docker", "exec", cid, "test", "-f", container_path],
            capture_output=True, text=True, timeout=20)
        not_link = subprocess.run(["docker", "exec", cid, "test", "!", "-L", container_path],
            capture_output=True, text=True, timeout=20)
        if is_file.returncode != 0 or not_link.returncode != 0:
            raise RuntimeError(f"Expected marker file is absent or a symlink in private agent home {agent_id}.")
        digest_output = subprocess.run(["docker", "exec", cid, "sha256sum", "--", container_path],
            check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        digest = digest_output.split()[0].lower() if digest_output.split() else ""
        expected = expected_artifacts[agent_id]
        content = subprocess.run(["docker", "exec", cid, "cat", "--", container_path],
            check=True, capture_output=True, text=True, timeout=20).stdout
        expected_hash = expected["sha256"]
        if digest != expected_hash:
            raise RuntimeError(f"Private file hash mismatch in agent {agent_id}: {digest} != {expected_hash}.")
        if content != expected["content"]:
            raise RuntimeError(f"Private file content mismatch in agent {agent_id}.")
        user = str(cfg.get("User") or "")
        checks = {
            "container_running": True,
            "non_root_user": bool(user.split(":", 1)[0].isdigit() and int(user.split(":", 1)[0]) > 0),
            "user_matches_workspace_owner": user == f"{expected_uid}:{expected_gid}",
            "readonly_rootfs": host.get("ReadonlyRootfs") is True,
            "bounded_tmpfs": (
                "/tmp" in (host.get("Tmpfs") or {})
                and all(token in str((host.get("Tmpfs") or {}).get("/tmp", ""))
                        for token in ("nosuid", "nodev", "size=128m"))
            ),
            "no_published_ports": not bool(host.get("PortBindings") or {}),
            "capabilities_dropped": "ALL" in (host.get("CapDrop") or []) and not (host.get("CapAdd") or []),
            "no_new_privileges": any("no-new-privileges" in str(x) for x in (host.get("SecurityOpt") or [])),
            "network_is_none": host.get("NetworkMode") == "none",
            "not_privileged": host.get("Privileged") is False,
            "no_device_access": not bool(host.get("Devices") or host.get("DeviceRequests") or []),
            "no_host_pid_namespace": not str(host.get("PidMode") or "").startswith(("host", "container")),
            "no_host_ipc_namespace": not str(host.get("IpcMode") or "").startswith(("host", "container")),
            "memory_bounded": int(host.get("Memory") or 0) > 0,
            "swap_bounded_to_memory": int(host.get("MemorySwap") or 0) == int(host.get("Memory") or 0),
            "cpu_bounded": int(host.get("NanoCpus") or 0) > 0,
            "pids_bounded": int(host.get("PidsLimit") or 0) > 0,
            "one_private_mount": len(mounts) == 1 and mounts[0].get("Destination") == "/agent",
            "private_agent_home_source": "/agents/" in str(mounts[0].get("Source", "")),
            "working_dir_is_agent_home": cfg.get("WorkingDir") == "/agent",
            "home_is_agent_home": "HOME=/agent" in (cfg.get("Env") or []),
            "artifact_is_regular_non_symlink": is_file.returncode == 0 and not_link.returncode == 0,
            "artifact_hash_matches_that_worker_shell": digest == expected_hash,
            "artifact_content_matches_that_worker_shell": content == expected["content"],
        }
        output.append({"name": c.get("Name", "").lstrip("/"), "agent_id": agent_id, "user": user,
            "network": host.get("NetworkMode"), "artifact": marker, "artifact_sha256": digest,
            "memory_bytes": host.get("Memory"), "memory_swap_bytes": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"), "pids_limit": host.get("PidsLimit"),
            "mount_destinations": [m.get("Destination") for m in mounts], "checks": checks})
        found_agent_ids.add(agent_id)
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Agent container {c.get('Name')} failed isolation/artifact checks: {failed}")
    expected_ids = set(expected_artifacts)
    if found_agent_ids != expected_ids:
        raise RuntimeError(
            f"Private agent homes do not match the two shell callers; expected {sorted(expected_ids)}, "
            f"found {sorted(found_agent_ids)}."
        )
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
    marker = f"sandbox-smoke-{nonce}.txt"
    marker_content = f"AVRONE_SANDBOX_PROOF:{nonce}\n"
    shell_command = (
        f"printf '%s\\n' 'AVRONE_SANDBOX_PROOF:{nonce}' > {marker} "
        f"&& pwd && sha256sum -- {marker}"
    )
    expected_sha = hashlib.sha256(marker_content.encode("utf-8")).hexdigest()
    task = (
        "This is a deterministic one-command sandbox verification, not a research or planning task. "
        "The Lead must spawn exactly two independent workers. Each worker must make exactly ONE tool call, "
        "and that first response must be a shell tool call. Do not call skill_list, skill_load, task_board, "
        "self_scan, read_file, or any other tool. Do not send a prose preamble or a plan. "
        "Use the shell tool with args.cmd set to this exact command:\n"
        f"{shell_command}\n"
        "After the tool returns, report its actual TOOL RESULT only. Do not claim the command ran or the file "
        "exists unless the shell tool result proves it. Both workers use the same command but run it in their "
        "own private /agent directories."
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
    spawn_by_agent = {
        e.get("agent"): str((e.get("data") or {}).get("agent_id") or safe_agent_id((e.get("data") or {}).get("role", "")))
        for e in spawns
    }
    diagnostic = {
        "status": "DIAGNOSTIC",
        "run_id": run_id,
        "provider": status.get("provider"),
        "model": status.get("model"),
        "runner": status.get("runner"),
        "event_counts": {kind: sum(1 for e in events if e.get("kind") == kind)
                         for kind in sorted({str(e.get("kind")) for e in events})},
        "spawns": [
            {"agent": e.get("agent"), "agent_id": spawn_by_agent.get(e.get("agent")),
             "role": (e.get("data") or {}).get("role"),
             "focus": str((e.get("data") or {}).get("focus", ""))[:350]}
            for e in spawns
        ],
        "tool_calls": [
            {"agent": e.get("agent"), "tool": (e.get("data") or {}).get("tool"),
             "step": (e.get("data") or {}).get("step"),
             "args_keys": sorted((e.get("data") or {}).get("args", {}).keys())
                          if isinstance((e.get("data") or {}).get("args"), dict) else []}
            for e in calls
        ],
        "tool_results": [
            {"agent": e.get("agent"), "tool": (e.get("data") or {}).get("tool"),
             "output_preview": str((e.get("data") or {}).get("output", ""))[:350]}
            for e in results
        ],
        "worker_summaries": [
            {"role": str(a.get("role", "")),
             "result_preview": str(a.get("result", ""))[:700]}
            for a in (result.get("agents", []) if isinstance(result, dict) else [])
            if isinstance(a, dict)
        ],
        "lead_preview": str(result.get("answer", ""))[:1200] if isinstance(result, dict) else "",
    }
    outdir = ROOT / "data" / "local-ollama-smoke"
    outdir.mkdir(parents=True, exist_ok=True)
    diagnostic_path = outdir / f"{run_id}-diagnostic.json"
    diagnostic_path.write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
    print(json.dumps({**diagnostic, "diagnostic_path": str(diagnostic_path)}, indent=2))

    worker_calls = {
        agent: [e for e in calls if e.get("agent") == agent]
        for agent in {e.get("agent") for e in spawns if e.get("agent")}
    }
    worker_results = {
        agent: [e for e in results if e.get("agent") == agent]
        for agent in worker_calls
    }
    if len(spawns) != 2 or set(worker_calls) != set(e.get("agent") for e in spawns):
        raise RuntimeError(f"Expected exactly 2 spawned workers with unique identities; got {len(spawns)} and {sorted(worker_calls)}.")
    if len({spawn_by_agent.get(agent) for agent in worker_calls}) != 2:
        raise RuntimeError("The two workers did not receive distinct durable agent identities.")
    if any(len(worker_calls[a]) != 1 for a in worker_calls):
        raise RuntimeError("Each worker must issue exactly one tool call in this one-action smoke test.")
    if any(len(worker_results[a]) != 1 for a in worker_results):
        raise RuntimeError("Each worker must have exactly one recorded tool result.")
    if any((worker_calls[a][0].get("data") or {}).get("tool") != "shell" for a in worker_calls):
        raise RuntimeError("Both workers must use the shell tool; another tool was called.")
    output_by_agent = {
        agent: str((worker_results[agent][0].get("data") or {}).get("output", ""))
        for agent in worker_results
    }
    expected_line = f"{expected_sha}  {marker}"
    for agent, output in output_by_agent.items():
        if expected_line not in output or marker not in output:
            raise RuntimeError(f"Worker {agent} shell output did not prove sha256sum of its unique run marker.")
    shell_agents = set(worker_calls)
    expected_artifacts = {}
    for agent, output in output_by_agent.items():
        agent_id = spawn_by_agent.get(agent)
        if not agent_id:
            raise RuntimeError(f"Worker {agent} has no stable agent_id in its spawn event.")
        expected_artifacts[agent_id] = {"sha256": expected_sha, "content": marker_content}
    errors = [e for e in events if e.get("kind") == "error" and (not e.get("agent") or e.get("agent") in shell_agents)]
    if errors:
        raise RuntimeError(f"Worker errors were recorded: {errors[:3]}")
    containers = inspect_agent_homes(nonce, expected_artifacts)
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
