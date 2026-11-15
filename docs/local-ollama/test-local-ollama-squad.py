#!/usr/bin/env python3
"""Live Chat -> Ollama -> Trinity squad -> Docker isolation smoke test."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path
from typing import Any

from avrone import agentcontainer as container_policy

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

def _required_env_matches(env: Any) -> bool:
    required = {
        "HOME": "/agent", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
        "AVRONE_AGENT_DIR": "/agent",
    }
    if not isinstance(env, list) or any(not isinstance(item, str) for item in env):
        return False
    values: dict[str, list[str]] = {}
    for item in env:
        key, sep, value = item.partition("=")
        if not sep or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            return False
        values.setdefault(key, []).append(value)
    if any(len(items) != 1 for items in values.values()):
        return False
    secret_terms = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "PRIVATE_KEY")
    if any(any(term in key.upper() for term in secret_terms) and value[0].strip()
           for key, value in values.items()):
        return False
    return all(values.get(key) == [value] for key, value in required.items())


def _private_namespace(value: Any) -> bool:
    return value is None or value == "" or value == "private"


def _safe_tmpfs(host: dict[str, Any]) -> bool:
    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, dict) or set(tmpfs) != {"/tmp"}:
        return False
    spec = tmpfs.get("/tmp")
    if not isinstance(spec, str):
        return False
    options = [item.strip().lower() for item in spec.split(",")]
    option_set = set(options)
    required = {"rw", "nosuid", "nodev", "size=128m"}
    return option_set == required and len(options) == len(required)


def _durable_spawn_ids(spawns: list[dict[str, Any]]) -> dict[str, str]:
    identities: dict[str, str] = {}
    seen_ids: set[str] = set()
    for event in spawns:
        if not isinstance(event, dict):
            raise RuntimeError("Spawn event is malformed.")
        agent = event.get("agent")
        data = event.get("data")
        agent_id = data.get("agent_id") if isinstance(data, dict) else None
        if not isinstance(agent, str) or not agent.strip() or agent in identities:
            raise RuntimeError("Spawned workers must have distinct event identities.")
        if not isinstance(agent_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,48}", agent_id):
            raise RuntimeError("Every spawned worker must expose a canonical durable agent_id; role names are not identities.")
        if agent_id in seen_ids:
            raise RuntimeError("Two spawned workers share one durable agent_id.")
        identities[agent] = agent_id
        seen_ids.add(agent_id)
    return identities


def inspect_agent_homes(nonce: str, expected_artifacts: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Inspect exactly one fully hardened Docker home for each shell-calling worker."""
    expected_ids = set(expected_artifacts)
    ids_raw = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc", "--filter", "label=avrone.agent-home=1"],
        check=True, capture_output=True, text=True, timeout=20,
    ).stdout
    if not isinstance(ids_raw, str):
        raise RuntimeError("Docker returned malformed container ID inventory.")
    ids = [value.strip() for value in ids_raw.splitlines() if value.strip()]
    if not ids:
        raise RuntimeError("No persistent AVRONE agent-home containers exist after the squad run.")
    if len(ids) != len(set(ids)):
        raise RuntimeError("Docker returned duplicate container IDs.")
    if any(re.fullmatch(r"[0-9a-fA-F]{64}", value) is None for value in ids):
        raise RuntimeError("Docker returned a malformed full-length container ID; refusing to pass it to docker inspect/exec.")
    raw = subprocess.run(
        ["docker", "inspect", *ids], check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    try:
        containers = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError("Docker inspect returned invalid JSON.") from None
    if not isinstance(containers, list) or len(containers) != len(ids) or any(
        not isinstance(container, dict) for container in containers
    ):
        raise RuntimeError("Docker inspect returned a malformed container inventory.")

    inspected_ids = [container.get("Id") for container in containers]
    if (
        any(not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None
            for value in inspected_ids)
        or len({value.lower() for value in inspected_ids if isinstance(value, str)}) != len(ids)
        or {value.lower() for value in inspected_ids if isinstance(value, str)}
            != {value.lower() for value in ids}
    ):
        raise RuntimeError("Docker inspect container identities do not match the full-length listed inventory.")

    output: list[dict[str, Any]] = []
    found_agent_ids: set[str] = set()
    marker = f"sandbox-smoke-{nonce}.txt"
    container_path = f"/agent/{marker}"
    expected_uid = os.environ.get("AVRONE_AGENT_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    expected_gid = os.environ.get("AVRONE_AGENT_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))
    expected_image_ref = os.environ.get("AVRONE_DOCKER_IMAGE", "python:3.12-slim")
    try:
        image_result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", expected_image_ref],
            check=True, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Unable to resolve the configured Docker image to an immutable image ID.") from exc
    expected_image_id = image_result.stdout.strip().lower() if isinstance(image_result.stdout, str) else ""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id) is None:
        raise RuntimeError("Docker image inspect returned a malformed immutable image ID.")
    expected_memory = container_policy.mem_mb() * 1024 * 1024
    expected_nano_cpus = int(round(container_policy.cpus() * 1_000_000_000))

    for container in containers:
        cfg = container.get("Config")
        labels = cfg.get("Labels") if isinstance(cfg, dict) else None
        if not isinstance(labels, dict):
            continue
        agent_id = labels.get("avrone.agent")
        if labels.get("avrone.agent-home") != "1" or agent_id not in expected_ids:
            continue
        if agent_id in found_agent_ids:
            raise RuntimeError(f"Multiple Docker containers claim worker identity {agent_id}.")
        found_agent_ids.add(agent_id)
        host = container.get("HostConfig")
        if not isinstance(cfg, dict) or not isinstance(host, dict):
            raise RuntimeError(f"Worker container {agent_id} has malformed Config/HostConfig.")
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
            raise RuntimeError(f"Worker container {agent_id} does not have exactly one private mount.")
        mount = mounts[0]
        source = mount.get("Source")
        source_path = Path(source) if isinstance(source, str) else Path("/")
        binds = host.get("Binds")
        bind_ok = False
        if isinstance(binds, list) and len(binds) == 1 and isinstance(binds[0], str):
            pieces = binds[0].split(":")
            bind_ok = (
                len(pieces) in (2, 3) and pieces[1] == "/agent"
                and pieces[2:] in ([], ["rw"])
                and Path(pieces[0]).name == agent_id and Path(pieces[0]).parent.name == "agents"
            )
        mount_ok = (
            mount.get("Type") == "bind" and mount.get("Destination") == "/agent"
            and mount.get("RW") is True and source_path.name == agent_id
            and source_path.parent.name == "agents"
        )
        pid_mode, ipc_mode, user = host.get("PidMode"), host.get("IpcMode"), cfg.get("User")
        checks = {
            "container_running": isinstance(container.get("State"), dict)
                and container["State"].get("Running") is True,
            "expected_image": cfg.get("Image") == expected_image_id,
            "actual_image_id_matches": container.get("Image") == expected_image_id,
            "non_root_user": isinstance(user, str) and len(user.split(":")) == 2
                and all(part.isdigit() for part in user.split(":")) and int(user.split(":")[0]) > 0,
            "user_matches_workspace_owner": user == f"{expected_uid}:{expected_gid}",
            "readonly_rootfs": host.get("ReadonlyRootfs") is True,
            "bounded_tmpfs": _safe_tmpfs(host),
            "no_published_ports": host.get("PortBindings") in (None, {}),
            "capabilities_dropped": isinstance(host.get("CapDrop"), list)
                and all(isinstance(value, str) for value in host.get("CapDrop"))
                and "ALL" in host.get("CapDrop") and host.get("CapAdd") in (None, []),
            "no_new_privileges": isinstance(host.get("SecurityOpt"), list)
                and all(isinstance(value, str) for value in host.get("SecurityOpt"))
                and "no-new-privileges" in host.get("SecurityOpt"),
            "no_security_profile_overrides": host.get("SecurityOpt") == ["no-new-privileges"],
            "network_is_none": host.get("NetworkMode") == "none",
            "not_privileged": host.get("Privileged") is False,
            "no_device_access": host.get("Devices") in (None, [])
                and host.get("DeviceRequests") in (None, [])
                and host.get("DeviceCgroupRules") in (None, []),
            "default_runtime_only": host.get("Runtime") in (None, "", "runc"),
            "no_userns_override": host.get("UsernsMode") in (None, ""),
            "no_sysctl_overrides": host.get("Sysctls") in (None, {}),
            "no_supplemental_groups": host.get("GroupAdd") in (None, []),
            "private_pid_namespace": _private_namespace(pid_mode),
            "private_ipc_namespace": _private_namespace(ipc_mode),
            "private_uts_namespace": _private_namespace(host.get("UTSMode")),
            "private_cgroup_namespace": _private_namespace(host.get("CgroupnsMode")),
            "no_volumes_from": host.get("VolumesFrom") in (None, []),
            "auto_remove_disabled": host.get("AutoRemove") is False,
            "memory_limit_exact": type(host.get("Memory")) is int and host.get("Memory") == expected_memory,
            "swap_limit_exact": type(host.get("MemorySwap")) is int and host.get("MemorySwap") == expected_memory,
            "cpu_limit_exact": type(host.get("NanoCpus")) is int and host.get("NanoCpus") == expected_nano_cpus,
            "pids_limit_exact": type(host.get("PidsLimit")) is int and host.get("PidsLimit") == container_policy.PIDS_LIMIT,
            "one_private_mount": mount_ok and bind_ok,
            "required_environment_exact": _required_env_matches(cfg.get("Env")),
            "working_dir_is_agent_home": cfg.get("WorkingDir") == "/agent",
        }
        failed = [key for key, passed in checks.items() if not passed]
        if failed:
            raise RuntimeError(f"Agent container {container.get('Name')} failed isolation checks: {failed}.")
        cid = container.get("Id")
        if not isinstance(cid, str) or not cid:
            raise RuntimeError(f"Worker container {agent_id} has no Docker container ID.")
        is_file = subprocess.run(
            ["docker", "exec", cid, "test", "-f", container_path],
            capture_output=True, text=True, timeout=20,
        )
        not_link = subprocess.run(
            ["docker", "exec", cid, "test", "!", "-L", container_path],
            capture_output=True, text=True, timeout=20,
        )
        if is_file.returncode != 0 or not_link.returncode != 0:
            raise RuntimeError(f"Expected marker file is absent or a symlink in private agent home {agent_id}.")
        digest_output = subprocess.run(
            ["docker", "exec", cid, "sha256sum", "--", container_path],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        digest_parts = digest_output.split()
        digest = digest_parts[0].lower() if digest_parts else ""
        expected = expected_artifacts[agent_id]
        content = subprocess.run(
            ["docker", "exec", cid, "cat", "--", container_path],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout
        if digest != expected["sha256"]:
            raise RuntimeError(f"Private file hash mismatch in agent {agent_id}: {digest} != {expected['sha256']}.")
        if content != expected["content"]:
            raise RuntimeError(f"Private file content mismatch in agent {agent_id}.")
        checks.update({
            "artifact_is_regular_non_symlink": is_file.returncode == 0 and not_link.returncode == 0,
            "artifact_hash_matches_that_worker_shell": digest == expected["sha256"],
            "artifact_content_matches_that_worker_shell": content == expected["content"],
        })
        output.append({
            "name": str(container.get("Name") or "").lstrip("/"), "agent_id": agent_id,
            "user": user, "network": host.get("NetworkMode"), "artifact": marker,
            "image_ref": expected_image_ref, "image_id": expected_image_id,
            "artifact_sha256": digest, "memory_bytes": host.get("Memory"),
            "memory_swap_bytes": host.get("MemorySwap"), "nano_cpus": host.get("NanoCpus"),
            "pids_limit": host.get("PidsLimit"), "mount_destinations": [mount.get("Destination")],
            "checks": checks,
        })
    if found_agent_ids != expected_ids:
        raise RuntimeError(
            f"Private agent homes do not match the two shell callers; expected {sorted(expected_ids)}, "
            f"found {sorted(found_agent_ids)}."
        )
    return output

def _validate_worker_tool_events(
    spawns: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
    expected_command: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Require exactly one real, exact shell call and result from each spawned worker.

    This deliberately receives ALL tool.call/tool.result events, not shell-filtered subsets:
    a worker may not hide an extra skill/task-board/self-scan call behind a passing shell call.
    """
    identities = _durable_spawn_ids(spawns)
    if len(spawns) != 2:
        raise RuntimeError(f"Expected exactly two spawned workers, got {len(spawns)}.")
    expected_agents = set(identities)
    if len(calls) != 2:
        raise RuntimeError(f"Expected exactly two total tool.call events, got {len(calls)}.")
    if len(results) != 2:
        raise RuntimeError(f"Expected exactly two total tool.result events, got {len(results)}.")

    calls_by_agent: dict[str, dict[str, Any]] = {}
    for event in calls:
        if not isinstance(event, dict):
            raise RuntimeError("A tool.call event is malformed.")
        agent = event.get("agent")
        data = event.get("data")
        if agent not in expected_agents or not isinstance(data, dict):
            raise RuntimeError("A tool.call event does not belong to one of the two spawned workers.")
        if agent in calls_by_agent:
            raise RuntimeError(f"Worker {agent} issued more than one tool call.")
        if data.get("tool") != "shell":
            raise RuntimeError(f"Worker {agent} used {data.get('tool')!r}; only shell is permitted.")
        args = data.get("args")
        if not isinstance(args, dict) or args != {"cmd": expected_command}:
            raise RuntimeError(f"Worker {agent} did not submit the exact approved shell command.")
        calls_by_agent[agent] = event

    results_by_agent: dict[str, str] = {}
    for event in results:
        if not isinstance(event, dict):
            raise RuntimeError("A tool.result event is malformed.")
        agent = event.get("agent")
        data = event.get("data")
        if agent not in expected_agents or not isinstance(data, dict):
            raise RuntimeError("A tool.result event does not belong to one of the two spawned workers.")
        if agent in results_by_agent:
            raise RuntimeError(f"Worker {agent} produced more than one tool result.")
        if data.get("tool") != "shell":
            raise RuntimeError(f"Worker {agent} returned a result for {data.get('tool')!r}, not shell.")
        if agent not in calls_by_agent:
            raise RuntimeError(f"Worker {agent} returned a tool result without a recorded call.")
        output = data.get("output")
        if not isinstance(output, str):
            raise RuntimeError(f"Worker {agent} shell result is not text.")
        results_by_agent[agent] = output

    if set(calls_by_agent) != expected_agents or set(results_by_agent) != expected_agents:
        raise RuntimeError("The two spawned workers do not each have exactly one shell call and result.")
    return identities, results_by_agent


def build_smoke_task(shell_command: str) -> str:
    """Build an unambiguous request for one real shell action per isolated worker."""
    return (
        "This is a deterministic one-command sandbox verification, not a research or planning task. "
        "The Lead must spawn exactly two independent workers. "
        "Each worker's first and only tool call must be shell. "
        "Do not call think or any preparatory/non-shell tool. "
        "Do not send a prose preamble or a plan. "
        "Use the shell tool with args.cmd set to this exact command:\\n"
        f"{shell_command}\\n"
        "After the tool returns, report its actual TOOL RESULT only. Do not claim the command ran or the file "
        "exists unless the shell tool result proves it. Both workers use the same command but run it in their "
        "own private /agent directories."
    )


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
    task = build_smoke_task(shell_command)
    result = request_json(base + "/api/chat",
        {"message": task, "mode": "squad", "squad_size": 2, "dispatch": "local"}, timeout=args.timeout)
    run_id = result.get("run_id") if isinstance(result, dict) else None
    if not run_id:
        raise RuntimeError("Chat returned no run_id; this is not a verified run.")
    events = request_json(base + "/api/runs/" + urllib.parse.quote(str(run_id), safe=""))
    if not isinstance(events, list):
        raise RuntimeError("Run log endpoint did not return an event array.")
    if any(not isinstance(event, dict) for event in events):
        raise RuntimeError("Run log endpoint returned a malformed event envelope.")
    spawns = [e for e in events if e.get("kind") == "agent.spawn"]
    calls = [e for e in events if e.get("kind") == "tool.call"]
    results = [e for e in events if e.get("kind") == "tool.result"]
    spawn_by_agent = {
        e.get("agent"): ((e.get("data") or {}).get("agent_id")
                         if isinstance(e.get("data"), dict) else None)
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

    spawn_by_agent, output_by_agent = _validate_worker_tool_events(
        spawns, calls, results, shell_command
    )
    spawn_agents = list(spawn_by_agent)
    expected_line = f"{expected_sha}  {marker}"
    for agent, output in output_by_agent.items():
        if expected_line not in output or marker not in output:
            raise RuntimeError(f"Worker {agent} shell output did not prove sha256sum of its unique run marker.")
    shell_agents = set(spawn_by_agent)
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
