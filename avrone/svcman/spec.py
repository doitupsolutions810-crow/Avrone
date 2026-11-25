"""Load and validate declarative service definitions (TOML or YAML)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .yamlmini import YAMLError, loads as yaml_loads

RESTARTS = {"always", "on-failure", "never"}
HEALTH_TYPES = {"http", "tcp", "command", "process"}
SERVICE_KEYS = {
    "description", "command", "command_env", "cwd", "user", "env", "depends_on",
    "optional", "enabled", "restart", "oneshot", "stop_grace_seconds",
    "start_timeout_seconds", "backoff_initial_seconds", "backoff_max_seconds",
    "backoff_multiplier", "jitter", "max_restarts", "restart_window_seconds",
    "circuit_reset_seconds", "stable_seconds", "log_max_bytes", "log_backups",
    "ports", "runtime_limit_seconds", "health", "limits",
}
HEALTH_KEYS = {
    "type", "url", "host", "port", "expect_status", "command", "interval_seconds",
    "timeout_seconds", "unhealthy_threshold", "healthy_threshold",
}
LIMIT_KEYS = {
    "nofile", "nproc", "as_bytes", "cpu_seconds", "fsize", "core",
    "memory_max", "cpu_max", "pids_max", "cgroup_required",
}


class SpecError(ValueError):
    pass


@dataclass
class HealthSpec:
    type: str
    url: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    expect_status: int = 200
    command: list[str] = field(default_factory=list)
    interval_seconds: float = 2.0
    timeout_seconds: float = 2.0
    unhealthy_threshold: int = 3
    healthy_threshold: int = 1


@dataclass
class LimitSpec:
    nofile: int | None = None
    nproc: int | None = None
    as_bytes: int | None = None
    cpu_seconds: int | None = None
    fsize: int | None = None
    core: int | None = None
    memory_max: str | None = None
    cpu_max: str | None = None
    pids_max: str | None = None
    cgroup_required: bool = False

    def has_rlimits(self) -> bool:
        return any(
            value is not None
            for value in (self.nofile, self.nproc, self.as_bytes, self.cpu_seconds, self.fsize, self.core)
        )

    def rlimit_triples(self) -> list[list]:
        pairs = []
        for attr, name in (
            ("nofile", "RLIMIT_NOFILE"),
            ("nproc", "RLIMIT_NPROC"),
            ("as_bytes", "RLIMIT_AS"),
            ("cpu_seconds", "RLIMIT_CPU"),
            ("fsize", "RLIMIT_FSIZE"),
            ("core", "RLIMIT_CORE"),
        ):
            value = getattr(self, attr)
            if value is not None:
                pairs.append([name, int(value), int(value)])
        return pairs

    def has_cgroup(self) -> bool:
        return any(value for value in (self.memory_max, self.cpu_max, self.pids_max))


@dataclass
class ServiceSpec:
    name: str
    command: list[str]
    description: str = ""
    command_env: str = ""
    cwd: str | None = None
    user: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    optional: bool = False
    enabled: bool = True
    restart: str = "on-failure"
    oneshot: bool = False
    stop_grace_seconds: float = 10.0
    start_timeout_seconds: float = 30.0
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    jitter: float = 0.2
    max_restarts: int = 5
    restart_window_seconds: float = 60.0
    circuit_reset_seconds: float = 60.0
    stable_seconds: float = 10.0
    log_max_bytes: int = 10_485_760
    log_backups: int = 3
    ports: list[int] = field(default_factory=list)
    runtime_limit_seconds: float | None = None
    health: HealthSpec | None = None
    limits: LimitSpec | None = None
    source: str = ""

    def delay(self, attempt: int, rand: float | None = None) -> float:
        """Exponential backoff. `rand` in [-1, 1] replaces the random jitter draw."""
        import random
        base = self.backoff_initial_seconds * (self.backoff_multiplier ** max(0, attempt))
        base = min(self.backoff_max_seconds, base)
        if self.jitter:
            draw = rand if rand is not None else random.uniform(-1.0, 1.0)
            base = base * (1.0 + self.jitter * draw)
        return max(0.0, base)

    def substitute(self, mapping: dict[str, str]) -> ServiceSpec:
        def sub(value):
            if not isinstance(value, str):
                return value
            for key, repl in mapping.items():
                value = value.replace("{" + key + "}", repl)
            return value

        health = self.health
        if health is not None:
            health = replace(health, url=sub(health.url), command=[sub(part) for part in health.command])
        return replace(
            self,
            command=[sub(part) for part in self.command],
            cwd=sub(self.cwd) if self.cwd else None,
            env={key: sub(value) for key, value in self.env.items()},
            health=health,
        )


def load_config(path: Path) -> list[ServiceSpec]:
    path = Path(path)
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(
            item for item in path.iterdir()
            if item.is_file() and item.suffix in {".toml", ".yaml", ".yml"}
        )
    else:
        raise SpecError(f"config path not found: {path}")
    if not files:
        raise SpecError(f"no service definitions in {path}")
    specs: list[ServiceSpec] = []
    seen: set[str] = set()
    for file in files:
        for spec in load_file(file):
            if spec.name in seen:
                raise SpecError(f"duplicate service {spec.name!r} ({file})")
            seen.add(spec.name)
            specs.append(spec)
    _reject_unknown_deps(specs)
    return specs


def load_file(path: Path) -> list[ServiceSpec]:
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(text)
        elif path.suffix in {".yaml", ".yml"}:
            data = yaml_loads(text)
        else:
            raise SpecError(f"{path}: expected .toml, .yaml, or .yml")
    except (tomllib.TOMLDecodeError, YAMLError) as exc:
        raise SpecError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise SpecError(f"{path}: expected a services table")
    return [parse_service(str(name), body, path) for name, body in data["services"].items()]


def parse_service(name: str, body, source: Path) -> ServiceSpec:
    where = f"{source}:{name}"
    if not isinstance(body, dict):
        raise SpecError(f"{where}: service must be a table")
    unknown = set(body) - SERVICE_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown keys {sorted(unknown)}")
    if not _valid_name(name):
        raise SpecError(f"{where}: name must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}")
    command = _str_list(body.get("command"), f"{where}: command")
    if not command:
        raise SpecError(f"{where}: command must be a non-empty list")
    restart = str(body.get("restart", "on-failure"))
    if restart not in RESTARTS:
        raise SpecError(f"{where}: restart must be one of {sorted(RESTARTS)}")
    env = body.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise SpecError(f"{where}: env must be a table of strings")
    depends = _str_list(body.get("depends_on") or [], f"{where}: depends_on")
    if name in depends:
        raise SpecError(f"{where}: a service cannot depend on itself")
    ports = []
    for port in body.get("ports") or []:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise SpecError(f"{where}: ports must be integers from 1 to 65535")
        ports.append(port)
    jitter = _float(body.get("jitter", 0.2), f"{where}: jitter")
    if not 0 <= jitter <= 1:
        raise SpecError(f"{where}: jitter must be between 0 and 1")
    return ServiceSpec(
        name=name,
        command=command,
        description=str(body.get("description") or ""),
        command_env=str(body.get("command_env") or ""),
        cwd=_optional_str(body.get("cwd"), f"{where}: cwd"),
        user=_optional_str(body.get("user"), f"{where}: user"),
        env=dict(env),
        depends_on=depends,
        optional=_bool(body.get("optional", False), f"{where}: optional"),
        enabled=_bool(body.get("enabled", True), f"{where}: enabled"),
        restart=restart,
        oneshot=_bool(body.get("oneshot", False), f"{where}: oneshot"),
        stop_grace_seconds=_positive(body.get("stop_grace_seconds", 10), f"{where}: stop_grace_seconds"),
        start_timeout_seconds=_positive(body.get("start_timeout_seconds", 30), f"{where}: start_timeout_seconds"),
        backoff_initial_seconds=_nonneg(body.get("backoff_initial_seconds", 1), f"{where}: backoff_initial_seconds"),
        backoff_max_seconds=_nonneg(body.get("backoff_max_seconds", 30), f"{where}: backoff_max_seconds"),
        backoff_multiplier=_positive(body.get("backoff_multiplier", 2), f"{where}: backoff_multiplier"),
        jitter=jitter,
        max_restarts=_int_min(body.get("max_restarts", 5), 0, f"{where}: max_restarts"),
        restart_window_seconds=_positive(body.get("restart_window_seconds", 60), f"{where}: restart_window_seconds"),
        circuit_reset_seconds=_positive(body.get("circuit_reset_seconds", 60), f"{where}: circuit_reset_seconds"),
        stable_seconds=_nonneg(body.get("stable_seconds", 10), f"{where}: stable_seconds"),
        log_max_bytes=_int_min(body.get("log_max_bytes", 10_485_760), 1024, f"{where}: log_max_bytes"),
        log_backups=_int_min(body.get("log_backups", 3), 1, f"{where}: log_backups"),
        ports=ports,
        runtime_limit_seconds=_optional_positive(body.get("runtime_limit_seconds"), f"{where}: runtime_limit_seconds"),
        health=_parse_health(body.get("health"), where),
        limits=_parse_limits(body.get("limits"), where),
        source=str(source),
    )


def _parse_health(body, where: str) -> HealthSpec | None:
    if body is None:
        return None
    if not isinstance(body, dict):
        raise SpecError(f"{where}: health must be a table")
    unknown = set(body) - HEALTH_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown health keys {sorted(unknown)}")
    kind = str(body.get("type") or "")
    if kind not in HEALTH_TYPES:
        raise SpecError(f"{where}: health.type must be one of {sorted(HEALTH_TYPES)}")
    command = _str_list(body.get("command") or [], f"{where}: health.command")
    if kind == "command" and not command:
        raise SpecError(f"{where}: command health needs a command")
    if kind == "http" and not body.get("url"):
        raise SpecError(f"{where}: http health needs a url")
    port = int(body.get("port") or 0)
    if kind == "tcp" and not 1 <= port <= 65535:
        raise SpecError(f"{where}: tcp health needs a port")
    return HealthSpec(
        type=kind,
        url=str(body.get("url") or ""),
        host=str(body.get("host") or "127.0.0.1"),
        port=port,
        expect_status=_int_min(body.get("expect_status", 200), 100, f"{where}: expect_status"),
        command=command,
        interval_seconds=_positive(body.get("interval_seconds", 2), f"{where}: interval_seconds"),
        timeout_seconds=_positive(body.get("timeout_seconds", 2), f"{where}: timeout_seconds"),
        unhealthy_threshold=_int_min(body.get("unhealthy_threshold", 3), 1, f"{where}: unhealthy_threshold"),
        healthy_threshold=_int_min(body.get("healthy_threshold", 1), 1, f"{where}: healthy_threshold"),
    )


def _parse_limits(body, where: str) -> LimitSpec | None:
    if body is None:
        return None
    if not isinstance(body, dict):
        raise SpecError(f"{where}: limits must be a table")
    unknown = set(body) - LIMIT_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown limit keys {sorted(unknown)}")
    def optional_int(key):
        if body.get(key) is None:
            return None
        return _int_min(body[key], 0, f"{where}: {key}")
    def optional_text(key):
        if body.get(key) is None:
            return None
        return str(body[key])
    return LimitSpec(
        nofile=optional_int("nofile"),
        nproc=optional_int("nproc"),
        as_bytes=optional_int("as_bytes"),
        cpu_seconds=optional_int("cpu_seconds"),
        fsize=optional_int("fsize"),
        core=optional_int("core"),
        memory_max=optional_text("memory_max"),
        cpu_max=optional_text("cpu_max"),
        pids_max=optional_text("pids_max"),
        cgroup_required=_bool(body.get("cgroup_required", False), f"{where}: cgroup_required"),
    )


def find_cycles(specs: list[ServiceSpec]) -> list[list[str]]:
    deps = {spec.name: list(spec.depends_on) for spec in specs}
    color = {name: 0 for name in deps}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(name: str) -> None:
        color[name] = 1
        stack.append(name)
        for dep in deps.get(name, []):
            if dep not in color:
                continue
            if color[dep] == 1:
                cycles.append(stack[stack.index(dep):] + [dep])
            elif color[dep] == 0:
                visit(dep)
        stack.pop()
        color[name] = 2

    for name in list(deps):
        if color[name] == 0:
            visit(name)
    return cycles


def _reject_unknown_deps(specs: list[ServiceSpec]) -> None:
    names = {spec.name for spec in specs}
    for spec in specs:
        missing = [dep for dep in spec.depends_on if dep not in names]
        if missing:
            raise SpecError(f"{spec.name}: unknown dependencies {missing}")


def _valid_name(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    first, rest = name[0], name[1:]
    if not (first.isalnum()):
        return False
    return all(ch.isalnum() or ch in "_.-" for ch in rest)


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SpecError(f"{where} must be a list of strings")
    return list(value)


def _optional_str(value, where: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SpecError(f"{where} must be a string")
    return value


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"{where} must be a boolean")
    return value


def _float(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{where} must be a number")
    return float(value)


def _positive(value, where: str) -> float:
    number = _float(value, where)
    if number <= 0:
        raise SpecError(f"{where} must be > 0")
    return number


def _nonneg(value, where: str) -> float:
    number = _float(value, where)
    if number < 0:
        raise SpecError(f"{where} must be >= 0")
    return number


def _optional_positive(value, where: str) -> float | None:
    if value is None:
        return None
    return _positive(value, where)


def _int_min(value, minimum: int, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{where} must be an integer")
    if value < minimum:
        raise SpecError(f"{where} must be >= {minimum}")
    return value
