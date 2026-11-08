"""Actual loopback HTTP checks for monitor isolation, caching and local reads."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import pytest

from tasklease.client import Client
from tasklease.errors import TransportError
from tasklease.monitor.server import MonitorServer
from tasklease.store import Store

TOKEN = "tl_test-secret-must-stay-in-python"
TASK_ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV"
REPLY_ID = "t_01ARZ3NDEKTSV4RRFFQ69G5FAW"


@dataclass
class Service:
    calls: Counter[str] = field(default_factory=Counter)
    domains: list[str] = field(default_factory=lambda: ["acme/a", "acme/b"])
    hours: list[int] = field(default_factory=list)
    unavailable: bool = False
    denied: str | None = None
    delay: float = 0
    reply: dict[str, object] | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        operation = request.url.path.rsplit("/", 1)[-1]
        self.calls[operation] += 1
        if self.unavailable:
            raise httpx.ConnectError(TOKEN, request=request)
        value: object = json.loads(request.content)
        assert isinstance(value, dict)
        data = cast(dict[str, object], value)
        if operation == "identity.whoami":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "name": "monitor",
                    "domains": self.domains,
                    "scopes": {"create": [], "own": [], "read": ["**"]},
                    "operator": False,
                },
            )
        if operation == "monitor.snapshot":
            assert type(data["hours"]) is int
            self.hours.append(data["hours"])
            time.sleep(self.delay)
            if data["domain"] == self.denied:
                return httpx.Response(
                    403,
                    json={
                        "ok": False,
                        "error": {"code": "forbidden", "message": TOKEN},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "domain": data["domain"],
                    "generated_at": 2_000_000_000_000,
                    "topics": [],
                    "stats": [],
                },
            )
        if operation == "create":
            return httpx.Response(200, json={"ok": True, "id": TASK_ID})
        if operation == "own":
            reply, self.reply = self.reply, None
            if reply is not None:
                return httpx.Response(200, json={"ok": True, "task": reply})
            return httpx.Response(
                404, json={"ok": False, "error": {"code": "empty", "message": "Empty"}}
            )
        if operation == "delete":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"Unexpected operation {operation}")


@dataclass
class Running:
    server: MonitorServer
    service: Service
    api: Client
    state_root: Path
    static_root: Path

    def request(
        self,
        target: str = "/api/snapshot",
        *,
        method: str = "GET",
        hosts: list[str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.putrequest(method, target, skip_host=True)
            if hosts is None:
                hosts = [f"127.0.0.1:{self.server.server_port}"]
            for host in hosts:
                connection.putheader("Host", host)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


@pytest.fixture
def running(tmp_path: Path) -> Iterator[Running]:
    service = Service()
    with Client(
        "https://service.example", TOKEN, transport=httpx.MockTransport(service.respond)
    ) as api:
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("<!doctype html><h1>Monitor</h1>")
        (static_root / "assets").mkdir()
        (static_root / "assets" / "app.js").write_text("console.log('monitor');")
        state_root = tmp_path / "state"
        with MonitorServer(api, state_root=state_root) as server:
            server.static_root = static_root
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}
            )
            thread.start()
            try:
                yield Running(server, service, api, state_root, static_root)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                assert not thread.is_alive()


def decoded(body: bytes) -> dict[str, object]:
    value: object = json.loads(body)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_snapshot_aggregates_configured_domains_without_exposing_credentials(
    running: Running,
) -> None:
    status, headers, body = running.request()
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert TOKEN.encode() not in body
    value = decoded(body)
    assert type(value["generated_at"]) is int
    assert value["local"] == {"requests": [], "replies": []}
    domains = value["domains"]
    assert isinstance(domains, list)
    assert [item["domain"] for item in domains] == ["acme/a", "acme/b"]
    assert running.service.calls == {"identity.whoami": 1, "monitor.snapshot": 2}
    assert running.service.hours == [24, 24]
    assert not running.state_root.exists()


def test_browser_headers_cannot_replace_server_identity(running: Running) -> None:
    with httpx.Client(trust_env=False) as browser:
        response = browser.get(
            running.server.url + "/api/snapshot",
            headers={
                "Authorization": "Bearer browser-supplied-token",
                "X-Worker": "browser",
            },
        )
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert "browser-supplied-token" not in response.text
    assert running.service.calls == {"identity.whoami": 1, "monitor.snapshot": 2}


def test_duplicate_identity_domains_are_fetched_once_in_original_order(
    running: Running,
) -> None:
    running.service.domains = ["acme/b", "acme/b", "acme/a", "acme/b"]
    status, _, body = running.request()
    assert status == 200
    domains = decoded(body)["domains"]
    assert isinstance(domains, list)
    assert [item["domain"] for item in domains] == ["acme/b", "acme/a"]
    assert running.service.calls == {"identity.whoami": 1, "monitor.snapshot": 2}


def test_concurrent_refreshes_share_one_serialized_cache_entry(
    running: Running,
) -> None:
    running.service.delay = 0.04
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda _: running.request(), range(8)))
    assert {status for status, _, _ in replies} == {200}
    assert len({body for _, _, body in replies}) == 1
    assert running.service.calls == {"identity.whoami": 1, "monitor.snapshot": 2}
    assert 24 < running.server.cache._expires - time.monotonic() <= 25


def test_refresh_burst_queues_before_accept_loop_starts(tmp_path: Path) -> None:
    service = Service(delay=0.04)
    with (
        Client(
            "https://service.example",
            TOKEN,
            transport=httpx.MockTransport(service.respond),
        ) as api,
        MonitorServer(api, state_root=tmp_path / "absent") as server,
    ):
        connections: list[http.client.HTTPConnection] = []
        serving = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        started = False
        try:
            # Queue complete requests before starting accept. A backlog of five
            # cannot reliably retain eight callers; no scheduler race is needed.
            for _ in range(8):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5
                )
                connections.append(connection)
                connection.connect()
                connection.request("GET", "/api/snapshot")
            serving.start()
            started = True
            bodies: list[bytes] = []
            for connection in connections:
                response = connection.getresponse()
                assert response.status == 200
                bodies.append(response.read())
            assert len(set(bodies)) == 1
            assert service.calls == {"identity.whoami": 1, "monitor.snapshot": 2}
        finally:
            for connection in connections:
                connection.close()
            if started:
                server.shutdown()
                serving.join(timeout=5)
                assert not serving.is_alive()


def test_hour_change_replaces_entry_and_expiry_refreshes(running: Running) -> None:
    for hours in (24, 1, 24):
        assert running.request(f"/api/snapshot?hours={hours}")[0] == 200
    assert running.service.hours == [24, 24, 1, 1, 24, 24]
    assert running.server.cache._hours == 24
    running.server.cache._expires = 0
    assert running.request()[0] == 200
    assert running.service.calls["identity.whoami"] == 4


@pytest.mark.parametrize("hours", [1, 720])
def test_hours_boundaries_reach_upstream(running: Running, hours: int) -> None:
    assert running.request(f"/api/snapshot?hours={hours}")[0] == 200
    assert running.service.hours == [hours, hours]


@pytest.mark.parametrize(
    "query",
    [
        "hours=0",
        "hours=721",
        "hours=-1",
        "hours=1.5",
        "hours=NaN",
        "hours=1e2",
        "hours=",
        "hours=1&hours=2",
        "hours=1&url=https://evil.example",
        "url=https://evil.example",
        "hours",
        "hours=%",
    ],
)
def test_invalid_queries_never_reach_service(running: Running, query: str) -> None:
    assert running.request("/api/snapshot?" + query)[0] == 400
    assert not running.service.calls


@pytest.mark.parametrize(
    "hosts",
    [[], ["evil.example"], ["127.0.0.1"], ["localhost"], ["127.0.0.1:1"], ["[::1]:1"]],
)
def test_disallowed_host_never_reaches_service(
    running: Running, hosts: list[str]
) -> None:
    assert running.request(hosts=hosts)[0] == 403
    assert not running.service.calls


def test_duplicate_host_is_rejected_even_when_both_values_are_allowed(
    running: Running,
) -> None:
    host = f"localhost:{running.server.server_port}"
    assert running.request(hosts=[host, host])[0] == 403
    assert running.request(hosts=[host])[0] == 200


@pytest.mark.parametrize(
    "method",
    ["HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BREW"],
)
def test_methods_cannot_forward_writes(running: Running, method: str) -> None:
    status, _, _ = running.request(method=method)
    assert status == 405
    assert not running.service.calls
    assert running.request(method=method, hosts=["evil.example"])[0] == 403


@pytest.mark.parametrize(
    "target",
    [
        "/../config.toml",
        "/assets/../../config.toml",
        "/%2e%2e/config.toml",
        "/assets/%2e%2e/%2e%2e/config.toml",
        "/%252e%252e/config.toml",
        "/assets\\..\\config.toml",
        "/assets%5c..%5cconfig.toml",
        "/%00",
        "/assets/%FF",
        "/assets/%",
        "/assets/app.js#fragment",
        "http://evil.example/api/snapshot",
        "/assets/app.js?hours=24",
    ],
)
def test_static_path_traversal_and_noncanonical_targets_are_rejected(
    running: Running, target: str
) -> None:
    assert running.request(target)[0] == 400
    assert not running.service.calls


def test_only_packaged_static_tree_is_served(running: Running) -> None:
    status, headers, body = running.request("/")
    assert status == 200 and body == b"<!doctype html><h1>Monitor</h1>"
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert TOKEN.encode() not in body
    status, headers, body = running.request("/assets/app.js")
    assert status == 200 and body == b"console.log('monitor');"
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    for path in ("/missing", "/api/unknown", "/assets/"):
        assert running.request(path)[0] == 404
    assert not running.service.calls


@pytest.mark.parametrize("missing_tree", [False, True])
def test_missing_dashboard_explains_build_or_reinstall_without_api_or_state_writes(
    running: Running, missing_tree: bool
) -> None:
    if missing_tree:
        shutil.rmtree(running.static_root)
    else:
        (running.static_root / "index.html").unlink()
    for path in ("/", "/index.html"):
        status, headers, body = running.request(path)
        assert status == 503
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        assert b"Dashboard was not built" in body
        assert b"make monitor" in body
        assert b"reinstall tasklease" in body
        assert TOKEN.encode() not in body
        assert b"<script" not in body
    assert running.request("/assets/missing.js")[0] == 404
    assert running.request("/other.html")[0] == 404
    assert running.request("/", hosts=["evil.example"])[0] == 403
    assert running.request("/%2e%2e/index.html")[0] == 400
    assert not running.service.calls
    assert not running.state_root.exists()


def test_service_failure_is_safe_and_does_not_replace_previous_cache(
    running: Running,
) -> None:
    assert running.request()[0] == 200
    previous = running.server.cache._content
    running.server.cache._expires = 0
    running.service.unavailable = True
    status, _, body = running.request()
    assert status == 502
    assert TOKEN.encode() not in body
    assert running.server.cache._content == previous


def test_domain_error_preserves_other_domains_with_safe_message(
    running: Running,
) -> None:
    running.service.denied = "acme/a"
    status, _, body = running.request()
    assert status == 200 and TOKEN.encode() not in body
    value = decoded(body)
    domains = value["domains"]
    assert isinstance(domains, list) and len(domains) == 1
    assert domains[0]["domain"] == "acme/b"
    assert value["errors"] == ["Snapshot unavailable for domain acme/a"]


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_all_agent_local_views_are_read_only_even_with_one_missing_folder(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Store("agent-a", running.state_root)
    first.submit(running.api, "acme/a", "work", "reply/agent-a", {}, "pending")
    second = Store("agent-b", running.state_root)
    second.submit(running.api, "acme/b", "work", "reply/agent-b", {}, "answered")
    running.service.reply = {
        "id": REPLY_ID,
        "domain": "acme/b",
        "topic": "reply/agent-b",
        "created_at": 2_000_000_000_000,
        "lease_until": 2_000_000_060_000,
        "owned_count": 1,
        "creator": "worker",
        "owner": {"identity": "agent-b", "worker": "cli"},
        "owned_at": 2_000_000_000_000,
        "data": {"request_id": "answered", "answer": "done"},
    }
    second.drain(running.api, "acme/b", "reply/agent-b")
    (first.reply_directory / "agent.json").unlink()
    first.reply_directory.rmdir()
    before = tree(running.state_root)
    directories = {str(path) for path in running.state_root.rglob("*") if path.is_dir()}
    running.service.calls.clear()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("The monitor must not publish local state")

    monkeypatch.setattr("tasklease.fsio.publish_new", forbidden)
    status, _, body = running.request()
    assert status == 200
    value = decoded(body)
    local = value["local"]
    assert isinstance(local, dict)
    assert {record["agent_id"] for record in local["requests"]} == {
        "agent-a",
    }
    assert local["replies"][0]["data"]["answer"] == "done"
    assert tree(running.state_root) == before
    assert {
        str(path) for path in running.state_root.rglob("*") if path.is_dir()
    } == directories
    assert running.service.calls == {"identity.whoami": 1, "monitor.snapshot": 2}


def test_answered_large_input_is_omitted_while_reply_and_pending_states_remain(
    running: Running,
) -> None:
    store = Store("agent-a", running.state_root)
    store.submit(
        running.api, "acme/a", "work", "reply/agent-a", {"message": "pending"}, "known"
    )

    def lost_response(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("Lost Create response", request=request)

    with Client(
        "https://service.example", TOKEN, transport=httpx.MockTransport(lost_response)
    ) as flaky:
        with pytest.raises(TransportError):
            store.submit(
                flaky,
                "acme/a",
                "work",
                "reply/agent-a",
                {"message": "unknown"},
                "unknown",
            )
    store.submit(
        running.api,
        "acme/a",
        "work",
        "reply/agent-a",
        {"answered_large_input": "completed-original-" + "x" * 250_000},
        "answered",
    )
    running.service.reply = {
        "id": REPLY_ID,
        "domain": "acme/a",
        "topic": "reply/agent-a",
        "created_at": 2_000_000_000_000,
        "lease_until": 2_000_000_060_000,
        "owned_count": 1,
        "creator": "worker",
        "owner": {"identity": "agent-a", "worker": "cli"},
        "owned_at": 2_000_000_000_000,
        "data": {"request_id": "answered", "answer": "done"},
    }
    store.drain(running.api, "acme/a", "reply/agent-a")
    answered = store.request("answered")
    assert answered is not None and answered.state == "answered"
    before = tree(running.state_root)
    status, _, body = running.request()
    assert status == 200
    assert b"answered_large_input" not in body and b"completed-original-" not in body
    assert len(body) < 10_000
    local = decoded(body)["local"]
    assert isinstance(local, dict)
    assert {record["request_id"]: record["state"] for record in local["requests"]} == {
        "known": "pending",
        "unknown": "submission_unknown",
    }
    assert local["replies"][0]["data"] == {"request_id": "answered", "answer": "done"}
    assert tree(running.state_root) == before


def test_corrupt_local_state_is_reported_without_mutation(running: Running) -> None:
    store = Store("agent-a", running.state_root)
    path = store.request_directory / "agent.json"
    path.write_text("NaN")
    before = tree(running.state_root)
    status, _, body = running.request()
    assert status == 200
    assert decoded(body)["errors"] == ["Local requester state could not be read"]
    assert tree(running.state_root) == before


@pytest.mark.parametrize("port", [-1, 65536, True])
def test_invalid_port_fails_before_binding(port: int) -> None:
    with Client("https://service.example", TOKEN) as api:
        with pytest.raises(ValueError, match="port"):
            MonitorServer(api, port=port)


def test_server_close_waits_for_inflight_read_before_client_cleanup(
    tmp_path: Path,
) -> None:
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    service = Service()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/monitor.snapshot":
            entered.set()
            assert release.wait(5)
        return service.respond(request)

    with Client(
        "https://service.example", TOKEN, transport=httpx.MockTransport(respond)
    ) as api:
        server = MonitorServer(api, state_root=tmp_path / "absent")
        serving = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        serving.start()
        statuses: list[int] = []

        def fetch() -> None:
            with httpx.Client(trust_env=False, timeout=5) as browser:
                statuses.append(browser.get(server.url + "/api/snapshot").status_code)

        reader = threading.Thread(target=fetch)
        reader.start()
        try:
            assert entered.wait(5)
            server.shutdown()
            serving.join(timeout=5)

            def close() -> None:
                server.server_close()
                closed.set()

            closer = threading.Thread(target=close)
            closer.start()
            assert not closed.wait(0.05)
            assert not api._http.is_closed
            release.set()
            reader.join(timeout=5)
            closer.join(timeout=5)
            assert closed.is_set() and statuses == [200]
            assert not api._http.is_closed
        finally:
            release.set()
            server.server_close()
            reader.join(timeout=5)
            serving.join(timeout=5)
