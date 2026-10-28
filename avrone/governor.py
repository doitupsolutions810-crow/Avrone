"""Health / thermal governor: adapt squad parallelism to the node's condition.

An edge node abroad runs hot, shares its CPU, fills its disk and swaps. Before a
squad (or deep wave) spawns its workers, Trinity asks the governor how many may
run at once. The governor reads four sensors, each through an injectable reader
so tests (and odd platforms) can supply their own:

  load      1-minute load average divided by CPU count   (os.getloadavg)
  mem_pct   memory in use, percent                        (psutil or /proc/meminfo)
  disk_free disk free on the data dir, percent            (shutil.disk_usage)
  temp_c    hottest CPU/thermal zone, Celsius             (psutil or /sys/class/thermal)

Each reading maps to a pressure factor in [0, 1] (1 = healthy, 0 = critical):

  temp:  1 at <= THERMAL_MAX-15C, falling linearly to 0 at >= THERMAL_MAX
  load:  1 at <= 1.0 per CPU,     falling linearly to 0 at >= 2.0 per CPU
  mem:   1 at <= 80% used,        falling linearly to 0 at >= 95% used
  disk:  1 at >= 10% free,        falling linearly to 0 at <= 2% free

  target = floor + round((max_parallel - floor) * min(factors))

clamped to [floor, max_parallel], where floor = AVRONE_MIN_PARALLEL (default 1,
itself clamped to 1..max_parallel). A sensor that cannot be read (or a reader that
raises) is ignored; with nothing readable the target is max_parallel. The governor
is advisory: it never raises and never returns less than 1.

Env knobs: AVRONE_THERMAL_MAX_C (default 85), AVRONE_MIN_PARALLEL (default 1),
AVRONE_GOVERNOR (on/off, default on).
"""
from __future__ import annotations

import glob
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

Reader = Callable[[], "float | None"]

DEFAULT_THERMAL_MAX_C = 85.0
THERMAL_SOFT_BAND_C = 15.0
LOAD_SOFT, LOAD_HARD = 1.0, 2.0
MEM_SOFT, MEM_HARD = 80.0, 95.0
DISK_SOFT, DISK_HARD = 10.0, 2.0


# ------------------------------------------------------------------ default readers
def _psutil() -> Any:
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:
        return None


def read_load() -> float | None:
    """1-minute load average per CPU."""
    one = os.getloadavg()[0]
    return one / max(1, os.cpu_count() or 1)


def read_mem_pct() -> float | None:
    ps = _psutil()
    if ps is not None:
        return float(ps.virtual_memory().percent)
    info: dict[str, float] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k.strip()] = float(v.split()[0])
    total = info.get("MemTotal") or 0
    avail = info.get("MemAvailable", info.get("MemFree"))
    if not total or avail is None:
        return None
    return 100.0 * (1 - avail / total)


def make_disk_reader(path: str = ".") -> Reader:
    def read_disk_free_pct() -> float | None:
        p = path
        while p and not os.path.exists(p):  # the data dir may not exist yet
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        u = shutil.disk_usage(p or ".")
        return 100.0 * u.free / u.total if u.total else None
    return read_disk_free_pct


def read_temp_c(thermal_glob: str = "/sys/class/thermal/thermal_zone*/temp") -> float | None:
    """Hottest temperature sensor, Celsius; None when the platform exposes none."""
    temps: list[float] = []
    ps = _psutil()
    if ps is not None and hasattr(ps, "sensors_temperatures"):
        try:
            for entries in (ps.sensors_temperatures() or {}).values():
                temps += [float(e.current) for e in entries if getattr(e, "current", None) is not None]
        except Exception:
            pass
    if not temps:
        for p in glob.glob(thermal_glob):
            try:
                with open(p) as f:
                    raw = float(f.read().strip())
            except (OSError, ValueError):
                continue
            temps.append(raw / 1000.0 if raw > 1000 else raw)  # sysfs reports millidegrees
    temps = [t for t in temps if -40 < t < 150]  # drop disconnected-sensor junk
    return max(temps) if temps else None


# ------------------------------------------------------------------ model
@dataclass
class NodeHealth:
    load: float | None = None
    mem_pct: float | None = None
    disk_free_pct: float | None = None
    temp_c: float | None = None
    errors: dict[str, str] = field(default_factory=dict)
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("load", "mem_pct", "disk_free_pct", "temp_c"):
            if d[k] is not None:
                d[k] = round(d[k], 2)
        return d


def _ramp_down(x: float, soft: float, hard: float) -> float:
    """1 at/below soft, 0 at/above hard, linear between (works for hard < soft too)."""
    if hard == soft:
        return 1.0 if x < soft else 0.0
    f = (hard - x) / (hard - soft)
    return max(0.0, min(1.0, f))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Governor:
    """Computes a target concurrency in [floor, max_parallel] from node health."""

    def __init__(self, max_parallel: int, min_parallel: int | None = None,
                 thermal_max_c: float | None = None, enabled: bool = True,
                 readers: dict[str, Reader] | None = None, data_dir: str = ".",
                 ttl_s: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.max_parallel = max(1, int(max_parallel or 1))
        mp = _env_int("AVRONE_MIN_PARALLEL", 1) if min_parallel is None else int(min_parallel)
        self.floor = max(1, min(mp, self.max_parallel))
        self.thermal_max_c = (_env_float("AVRONE_THERMAL_MAX_C", DEFAULT_THERMAL_MAX_C)
                              if thermal_max_c is None else float(thermal_max_c))
        self.enabled = enabled
        self.readers: dict[str, Reader] = {
            "load": read_load, "mem_pct": read_mem_pct,
            "disk_free_pct": make_disk_reader(str(data_dir)), "temp_c": read_temp_c,
        }
        self.readers.update(readers or {})
        self.ttl_s = ttl_s
        self.clock = clock
        self._cache: tuple[float, NodeHealth] | None = None
        self.last_target = self.max_parallel
        self.last_reason = "not consulted yet"
        self.throttle_count = 0

    @classmethod
    def from_settings(cls, settings: Any, **kw: Any) -> "Governor":
        """Build from a Settings-like object; missing attributes fall back to env/defaults."""
        enabled = getattr(settings, "governor", None)
        if enabled is None:
            enabled = os.environ.get("AVRONE_GOVERNOR", "on").lower() not in {"0", "off", "false", "no"}
        return cls(max_parallel=getattr(settings, "max_parallel", 1) or 1,
                   min_parallel=getattr(settings, "min_parallel", None),
                   thermal_max_c=getattr(settings, "thermal_max_c", None),
                   enabled=bool(enabled), data_dir=str(getattr(settings, "data_dir", ".") or "."), **kw)

    # -------------------------------------------------------------- sensing
    def read(self, fresh: bool = False) -> NodeHealth:
        """Current readings (cached for ttl_s). Never raises."""
        now = self.clock()
        if not fresh and self._cache and now - self._cache[0] < self.ttl_s:
            return self._cache[1]
        h = NodeHealth(ts=time.time())
        for key, reader in self.readers.items():
            try:
                v = reader()
                if v is not None:
                    v = float(v)
                    if v != v:  # NaN
                        raise ValueError("NaN reading")
                setattr(h, key, v)
            except Exception as e:  # unreadable sensor: ignore it, remember why
                h.errors[key] = f"{type(e).__name__}: {e}"[:200]
        self._cache = (now, h)
        return h

    def factors(self, h: NodeHealth) -> dict[str, float]:
        f: dict[str, float] = {}
        if h.temp_c is not None:
            f["temp_c"] = _ramp_down(h.temp_c, self.thermal_max_c - THERMAL_SOFT_BAND_C, self.thermal_max_c)
        if h.load is not None:
            f["load"] = _ramp_down(h.load, LOAD_SOFT, LOAD_HARD)
        if h.mem_pct is not None:
            f["mem_pct"] = _ramp_down(h.mem_pct, MEM_SOFT, MEM_HARD)
        if h.disk_free_pct is not None:
            f["disk_free_pct"] = _ramp_down(h.disk_free_pct, DISK_SOFT, DISK_HARD)
        return f

    def target(self, want: int | None = None) -> int:
        """How many workers may run at once (1 <= result <= max_parallel; <= want if given)."""
        cap = self.max_parallel
        try:
            if not self.enabled:
                t, reason = cap, "governor off"
            else:
                fs = self.factors(self.read())
                if not fs:
                    t, reason = cap, "no readable sensors"
                else:
                    worst = min(fs, key=lambda k: fs[k])
                    t = self.floor + round((cap - self.floor) * fs[worst])
                    t = max(self.floor, min(cap, t))
                    reason = "healthy" if t >= cap else f"throttled by {worst} (factor {fs[worst]:.2f})"
        except Exception as e:  # advisory: never break a run over a sensor bug
            t, reason = cap, f"governor error: {type(e).__name__}"
        if want is not None:
            t = min(t, max(1, int(want)))
        t = max(1, t)
        if t < min(cap, want or cap):
            self.throttle_count += 1
        self.last_target, self.last_reason = t, reason
        return t

    def state(self) -> dict[str, Any]:
        """Governor + node health snapshot for telemetry. Never raises."""
        try:
            h = self.read()
            fs = self.factors(h)
            health = h.as_dict()
        except Exception as e:  # pragma: no cover - read() already swallows reader errors
            fs, health = {}, {"errors": {"governor": str(e)}}
        return {"enabled": self.enabled, "max_parallel": self.max_parallel, "min_parallel": self.floor,
                "thermal_max_c": self.thermal_max_c,
                "factors": {k: round(v, 3) for k, v in fs.items()},
                "last_target": self.last_target, "last_reason": self.last_reason,
                "throttle_count": self.throttle_count, "health": health}
