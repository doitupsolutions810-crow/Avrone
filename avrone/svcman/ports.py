"""Detect a listener on a TCP port before a service tries to bind it."""

from __future__ import annotations

import errno
import socket


def first_conflict(ports: list[int]) -> int | None:
    """Return the first port that is already taken, or None.

    The check binds without SO_REUSEADDR and then releases the socket. A
    service that starts in the gap can still race; the following bind inside
    the child fails and the restart policy handles it.
    """
    for port in ports:
        if port_in_use(port):
            return port
    return None


def port_in_use(port: int) -> bool:
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET, "127.0.0.1")):
        if _bind_taken(family, address, port):
            return True
    if socket.has_ipv6:
        if _bind_taken(socket.AF_INET6, "::", port, v6only=True):
            return True
    return False


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
