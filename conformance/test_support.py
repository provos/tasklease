"""Resource lifetime checks for the conformance harness itself."""

import inspect
import json
from collections.abc import Callable, Generator
from typing import cast

import httpx
import pytest

from conformance import conftest
from conformance.support import ADMIN_TOKEN, ServiceTarget
from tasklease.client import Client


class Registry(httpx.MockTransport):
    def __init__(self) -> None:
        super().__init__(self.handle)
        self.identities: set[str] = set()
        self.revoked: list[str] = []
        self.fail_revoke: set[str] = set()
        self.closed = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        value: object = json.loads(request.content)
        assert isinstance(value, dict)
        name = value.get("name")
        assert isinstance(name, str)
        if request.url.path == "/v1/identity.add":
            self.identities.add(name)
            return httpx.Response(
                200, json={"ok": True, "name": name, "token": "token-" + name}
            )
        assert request.url.path == "/v1/identity.revoke"
        self.revoked.append(name)
        if name in self.fail_revoke:
            return httpx.Response(500)
        if name not in self.identities:
            return httpx.Response(
                404,
                json={"ok": False, "error": {"code": "not_found", "message": "gone"}},
            )
        self.identities.remove(name)
        return httpx.Response(200, json={"ok": True})

    def close(self) -> None:
        self.closed = True


class Request:
    def __init__(self, admin: Client | None = None) -> None:
        self.admin = admin

    def getfixturevalue(self, name: str) -> object:
        assert self.admin is not None, (
            "Fake-only tests must not resolve Worker fixtures"
        )
        if name == "worker_url":
            return "https://worker.example"
        assert name == "worker_admin"
        return self.admin


def factory_fixture(
    backend: str, request: Request
) -> Generator[Callable[[str], ServiceTarget], None, None]:
    fixture = cast(
        Callable[
            [str, pytest.FixtureRequest],
            Generator[Callable[[str], ServiceTarget], None, None],
        ],
        inspect.unwrap(conftest.target_factory),
    )
    return fixture(backend, cast(pytest.FixtureRequest, request))


def test_targets_reuse_admin_and_close_only_their_own_identities() -> None:
    registry = Registry()
    with Client("https://worker.example", ADMIN_TOKEN, transport=registry) as admin:
        first = ServiceTarget("first", "https://worker.example", admin=admin)
        second = ServiceTarget("second", "https://worker.example", admin=admin)
        first.identity("one")
        second.identity("two")
        first.close()
        first.close()
        assert registry.identities == {"two"}
        second.identity("three")
        assert not registry.closed
        second.close()
        assert not registry.identities
        assert registry.revoked == ["one", "two", "three"]
    assert registry.closed


def test_cleanup_tolerates_explicit_admin_revocation() -> None:
    registry = Registry()
    with Client("https://worker.example", ADMIN_TOKEN, transport=registry) as admin:
        target = ServiceTarget("demo", "https://worker.example", admin=admin)
        target.identity("already-revoked")
        target.identity("remaining")
        admin.identity_revoke({"name": "already-revoked"})
        target.close()
        assert not registry.identities
        assert registry.revoked == ["already-revoked", "already-revoked", "remaining"]


def test_factory_revokes_all_targets_after_test_failure() -> None:
    registry = Registry()
    with Client("https://worker.example", ADMIN_TOKEN, transport=registry) as admin:
        fixture = factory_fixture("worker", Request(admin))
        create = next(fixture)
        create("demo").identity("ordinary")
        create("acme/pipeline").identity("corpus")
        with pytest.raises(RuntimeError, match="test failed"):
            try:
                raise RuntimeError("test failed")
            finally:
                fixture.close()
        assert not registry.identities
        assert not registry.closed
    assert registry.closed


def test_cleanup_attempts_other_identities_and_targets_after_failure() -> None:
    registry = Registry()
    with Client("https://worker.example", ADMIN_TOKEN, transport=registry) as admin:
        fixture = factory_fixture("worker", Request(admin))
        create = next(fixture)
        create("first").identity("other-target")
        last = create("last")
        last.identity("broken")
        last.identity("same-target")
        registry.fail_revoke.add("broken")
        with pytest.raises(ExceptionGroup, match="identity cleanup failed"):
            fixture.close()
        assert registry.revoked == ["broken", "same-target", "other-target"]
        assert registry.identities == {"broken"}
    assert registry.closed


def test_session_admin_fixture_closes_its_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry()
    admin = Client("https://worker.example", ADMIN_TOKEN, transport=registry)

    def client(url: str, token: str, *, transport: httpx.BaseTransport) -> Client:
        assert url == "https://worker.example" and token == ADMIN_TOKEN
        transport.close()
        return admin

    monkeypatch.setattr(conftest, "Client", client)
    fixture = cast(
        Callable[[str], Generator[Client, None, None]],
        inspect.unwrap(conftest.worker_admin),
    )("https://worker.example")
    assert next(fixture) is admin
    assert not registry.closed
    fixture.close()
    assert registry.closed


def test_session_admin_bypasses_ambient_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inspect HTTPX's URL routing without opening sockets or contacting a proxy.
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(variable, "http://proxy.invalid:3128")
        monkeypatch.setenv(variable.lower(), "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    url = "http://127.0.0.1:8787"
    fixture = cast(
        Callable[[str], Generator[Client, None, None]],
        inspect.unwrap(conftest.worker_admin),
    )(url)
    try:
        admin = next(fixture)
        assert admin._http._transport_for_url(httpx.URL(url)) is admin._http._transport
    finally:
        fixture.close()


def test_fake_factory_never_resolves_admin_or_worker() -> None:
    fixture = factory_fixture("fake", Request())
    create = next(fixture)
    target = create("demo")
    _, token = target.identity("offline")
    with target.client(token) as client:
        assert client.identity_whoami({})["name"] == "offline"
    fixture.close()
