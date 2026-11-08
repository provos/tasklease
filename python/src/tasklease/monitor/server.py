"""A loopback-only, read-only monitor with server-side identity credentials."""

from __future__ import annotations

import json
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from threading import Lock
from typing import cast
from urllib.parse import parse_qsl, unquote_to_bytes

from tasklease.client import Client
from tasklease.errors import APIError, StoreError, TaskleaseError, TransportError
from tasklease.store import Store

_CACHE_SECONDS = 25.0
_MISSING_DASHBOARD = b"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tasklease dashboard unavailable</title></head>
<body><main><h1>Dashboard was not built</h1>
<p>From a source checkout, run <code>make monitor</code> to build the dashboard.</p>
<p>For a packaged installation, reinstall tasklease to restore the dashboard assets.</p>
</main></body></html>
"""
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class SnapshotCache:
    """One serialized entry; a lock coalesces concurrent refreshes.

    Changing the hour range replaces the entry, so arbitrary choices cannot
    retain hundreds of large snapshots. Failed refreshes never replace it.
    """

    def __init__(self, client: Client, state_root: Path | None = None) -> None:
        self.client = client
        self.state_root = state_root
        self._lock = Lock()
        self._hours: int | None = None
        self._expires = 0.0
        self._content: bytes | None = None

    def _build(self, hours: int) -> dict[str, object]:
        domains = []
        errors: list[str] = []
        identity = self.client.identity_whoami({})
        for domain in dict.fromkeys(identity["domains"]):
            try:
                domains.append(
                    self.client.monitor_snapshot({"domain": domain, "hours": hours})
                )
            except APIError:
                # The exception can contain arbitrary upstream text. The browser
                # only needs to know which configured domain could not refresh.
                errors.append(f"Snapshot unavailable for domain {domain}")
        requests: list[dict[str, object]] = []
        replies: list[dict[str, object]] = []
        try:
            for agent_id in Store.agents(self.state_root):
                store = Store(agent_id, self.state_root, initialize=False)
                requests.extend(
                    record.as_dict()
                    for record in store.requests()
                    if record.state != "answered"
                )
                replies.extend(record.as_dict() for record in store.inbox())
        except StoreError:
            errors.append("Local requester state could not be read")
        result: dict[str, object] = {
            "generated_at": time.time_ns() // 1_000_000,
            "domains": domains,
            "local": {"requests": requests, "replies": replies},
        }
        if errors:
            result["errors"] = errors
        return result

    def get(self, hours: int) -> bytes:
        if type(hours) is not int or not 1 <= hours <= 720:
            raise ValueError("hours must be an integer from 1 to 720")
        with self._lock:
            if (
                hours == self._hours
                and self._content is not None
                and time.monotonic() < self._expires
            ):
                return self._content
            content = json.dumps(
                self._build(hours),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
            self._hours, self._content = hours, content
            self._expires = time.monotonic() + _CACHE_SECONDS
            return content


class MonitorServer(ThreadingHTTPServer):
    """Bind only to IPv4 loopback; keep the provided Client outside the browser.

    The caller owns the Client. Closing the server waits for its request
    threads before the caller closes that client's HTTP connection pool.
    """

    daemon_threads = False
    block_on_close = True
    # Several tabs can connect before the accept loop is scheduled. macOS
    # resets connections beyond TCPServer's default backlog of five.
    request_queue_size = 32

    def __init__(
        self, client: Client, *, port: int = 0, state_root: Path | None = None
    ) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        self.cache = SnapshotCache(client, state_root)
        self.static_root: Traversable = files("tasklease.monitor").joinpath("static")
        super().__init__(("127.0.0.1", port), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server_version = "tasklease-monitor"
    sys_version = ""

    @property
    def monitor(self) -> MonitorServer:
        return cast(MonitorServer, self.server)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: object) -> None:
        # Request targets and upstream payloads have no place in access logs.
        pass

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._reply(
            status, (message + "\n").encode("ascii"), "text/plain; charset=utf-8"
        )

    def _allowed_host(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        return len(hosts) == 1 and hosts[0] in {
            f"127.0.0.1:{self.monitor.server_port}",
            f"localhost:{self.monitor.server_port}",
        }

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if not self._allowed_host():
            self._error(HTTPStatus.FORBIDDEN, "Host is not allowed")
            return False
        if self.command != "GET":
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Only GET is supported")
            return False
        return True

    def _target(self) -> tuple[str, str]:
        target = self.path
        if (
            not target.startswith("/")
            or target.startswith("//")
            or "#" in target
            or re.search(r"%(?![0-9a-fA-F]{2})", target)
        ):
            raise ValueError("Invalid request target")
        path, _, query = target.partition("?")
        decoded = unquote_to_bytes(path).decode("utf-8", errors="strict")
        if (
            re.fullmatch(r"/[A-Za-z0-9._/-]*", decoded) is None
            or any(part in {".", ".."} for part in decoded.split("/"))
            or "//" in decoded
        ):
            raise ValueError("Invalid static path")
        return decoded, query

    def do_GET(self) -> None:
        try:
            path, query = self._target()
            if path == "/api/snapshot":
                pairs = parse_qsl(
                    query, keep_blank_values=True, strict_parsing=True, max_num_fields=1
                )
                if pairs and pairs[0][0] != "hours":
                    raise ValueError("Unexpected query")
                value = pairs[0][1] if pairs else "24"
                if re.fullmatch(r"[0-9]{1,3}", value) is None:
                    raise ValueError("Invalid hours")
                hours = int(value)
                if not 1 <= hours <= 720:
                    raise ValueError("Invalid hours")
            elif query:
                raise ValueError("Unexpected query")
        except (ValueError, UnicodeError):
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request")
            return
        if path == "/api/snapshot":
            try:
                content = self.monitor.cache.get(hours)
            except TransportError:
                self._error(HTTPStatus.BAD_GATEWAY, "The service could not be reached")
                return
            except (TaskleaseError, OSError, ValueError, TypeError, RecursionError):
                self._error(
                    HTTPStatus.BAD_GATEWAY, "The snapshot could not be refreshed"
                )
                return
            self._reply(HTTPStatus.OK, content, "application/json; charset=utf-8")
            return
        if path == "/":
            path = "/index.html"
        if path.startswith("/api/") or path.endswith("/"):
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        resource = self.monitor.static_root.joinpath(*path.lstrip("/").split("/"))
        try:
            if not resource.is_file():
                raise FileNotFoundError
            body = resource.read_bytes()
        except (OSError, ValueError) as error:
            if path == "/index.html" and isinstance(error, FileNotFoundError):
                self._reply(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    _MISSING_DASHBOARD,
                    "text/html; charset=utf-8",
                )
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = _CONTENT_TYPES.get(Path(path).suffix, "application/octet-stream")
        self._reply(HTTPStatus.OK, body, content_type)
