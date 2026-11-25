"""Loopback HTTP control API. Every route requires the supervisor token."""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .supervisor import SupervisorError

_MAX_BODY = 1_048_576


class ControlServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    supervisor = None
    token = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        if not _loopback(self.client_address[0]):
            self._send(403, {"error": "control API accepts loopback clients only"})
            return
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        if host and host not in {"127.0.0.1", "localhost", "::1"}:
            self._send(403, {"error": "control API accepts loopback Host headers only"})
            return
        if not self._authorized():
            self._send(401, {"error": "token required"})
            return
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            self._dispatch(parts, query)
        except SupervisorError as exc:
            self._send(404, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def _dispatch(self, parts: list[str], query: dict) -> None:
        supervisor = self.server.supervisor
        method = self.command
        if parts == ["v1", "status"] and method == "GET":
            self._send(200, supervisor.status())
            return
        if parts == ["v1", "events"] and method == "GET":
            self._send(200, supervisor.events_tail(_lines(query)))
            return
        if parts == ["v1", "services"] and method == "GET":
            self._send(200, {"services": supervisor.status()["services"]})
            return
        if parts == ["v1", "start"] and method == "POST":
            self._send(200, supervisor.start_enabled())
            return
        if parts == ["v1", "stop"] and method == "POST":
            self._send(200, supervisor.stop_all())
            return
        if parts == ["v1", "reload"] and method == "POST":
            self._send(200, supervisor.reload())
            return
        if parts == ["v1", "shutdown"] and method == "POST":
            body = self._body()
            mark = bool(body.get("mark_stopped", False))
            supervisor.request_shutdown(mark_stopped=mark)
            self._send(200, {"ok": True, "shutting_down": True, "mark_stopped": mark})
            return
        if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "services":
            name = parts[2]
            action = parts[3] if len(parts) > 3 else ""
            if method == "GET" and not action:
                self._send(200, supervisor.service_status(name))
                return
            if method == "GET" and action == "logs":
                self._send(200, supervisor.logs(name, _lines(query)))
                return
            if method == "POST" and action in {"start", "stop", "restart", "enable", "disable"}:
                fn = {
                    "start": supervisor.start_service,
                    "stop": supervisor.stop_service,
                    "restart": supervisor.restart_service,
                    "enable": supervisor.enable_service,
                    "disable": supervisor.disable_service,
                }[action]
                self._send(200, fn(name))
                return
        self._send(404, {"error": "not found"})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        got = header[7:].strip() if header.startswith("Bearer ") else ""
        if not got:
            got = (self.headers.get("X-Avrone-Svc-Token") or "").strip()
        expected = self.server.token or ""
        if not got or not expected or len(got) != len(expected):
            return False
        return hmac.compare_digest(got.encode(), expected.encode())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > _MAX_BODY:
            return {}
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _lines(query: dict) -> int:
    raw = (query.get("lines") or ["200"])[0]
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = 200
    return max(1, min(count, 5000))


def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1"}


def serve(supervisor, host: str, port: int, token: str) -> tuple[ControlServer, int]:
    try:
        httpd = ControlServer((host, port), Handler)
    except OSError as exc:
        raise SupervisorError(f"control API cannot bind {host}:{port}: {exc}") from exc
    httpd.supervisor = supervisor
    httpd.token = token
    thread = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=0.1),
        name="avrone-svc-api",
        daemon=True,
    )
    thread.start()
    bound = httpd.server_address[1]
    return httpd, bound
