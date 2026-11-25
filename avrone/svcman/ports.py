"""Detect a listener on a TCP port before a service tries to bind it."""

from __future__ import annotations

import errno
import socket


def first_conflict(ports: list[int]) -> int | None:
    """Return the first port that already has a listener, or None."""
    for port in ports:
        if port_in_use(port):
            return port
    return None


def port_in_use(port: int) -> bool:
    """True when something is listening on `port`.

    TIME_WAIT from a previous connection is not a conflict: Ollama and uvicorn
    bind with SO_REUSEADDR and can restart while those entries linger. A bind
    without SO_REUSEADDR would report that leftover as a conflict and refuse
    the restart.
    """
    listeners = _listening_ports()
    if listeners is not None:
        return port in listeners
    return _connect_taken(port) or _bind_taken(socket.AF_INET, "127.0.0.1", port)


def _listening_ports() -> set[int] | None:
    found = False
    ports: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()[1:]
        except OSError:
            continue
        found = True
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            try:
                ports.add(int(parts[1].split(":")[1], 16))
            except (IndexError, ValueError):
                continue
    return ports if found else None


def _connect_taken(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(("127.0.0.1", port))
    except ConnectionRefusedError:
        return False
    except OSError:
        return False
    else:
        return True
    finally:
        sock.close()


def _bind_taken(family: int, address: str, port: int, v6only: bool = False) -> bool:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if v6only:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((address, port))
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, errno.EACCES}:
            return True
        return False
    else:
        return False
    finally:
        sock.close()
