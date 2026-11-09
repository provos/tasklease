"""Backend-neutral clients and fault injection for conformance tests."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import httpx
import pytest

from tasklease.client import Client
from tasklease.testing import FakeBoard

ADMIN_TOKEN = "conformance-admin-fake-secret"
_FAKE_UNSUPPORTED = {
    "identity.add",
    "identity.revoke",
    "identity.rotate",
    "identity.list",
    "monitor.snapshot",
    "topic.declare",
}


class ServiceTarget:
    """A fresh domain, with identities provisioned outside the tested protocol."""

    def __init__(
        self, domain: str, url: str = "", *, board: FakeBoard | None = None
    ) -> None:
        self.domain = domain
        self.board = board
        self.url = board.base_url if board is not None else url

    def transport(self) -> httpx.BaseTransport:
        return (
            self.board.transport() if self.board is not None else httpx.HTTPTransport()
        )

    def client(
        self,
        token: str,
        worker: str = "cli",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Client:
        return Client(
            self.url,
            token,
            worker,
            transport=self.transport() if transport is None else transport,
        )

    def http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), trust_env=False)

    def identity(
        self,
        name: str | None = None,
        *,
        create: Sequence[str] = ("**",),
        own: Sequence[str] = ("**",),
        read: Sequence[str] = ("**",),
        operator: bool = False,
    ) -> tuple[str, str]:
        name = name or "test-" + uuid4().hex
        if self.board is not None:
            assert not operator, "The fake deliberately does not model operators"
            token = "fake-" + uuid4().hex
            self.board.add_identity(name, token, create=create, own=own, read=read)
        else:
            with self.client(ADMIN_TOKEN) as admin:
                token = admin.identity_add(
                    {
                        "name": name,
                        "domains": [self.domain],
                        "scopes": {
                            "create": list(create),
                            "own": list(own),
                            "read": list(read),
                        },
                        "operator": operator,
                    }
                )["token"]
        return name, token

    def require(self, operation: str) -> None:
        if self.board is not None and operation in _FAKE_UNSUPPORTED:
            pytest.skip(f"In-process board does not model {operation}")


class LostResponse(httpx.BaseTransport):
    """Commit an operation through either transport, then lose its first reply."""

    def __init__(self, inner: httpx.BaseTransport, operation: str) -> None:
        self.inner = inner
        self.operation = operation
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if request.url.path == "/v1/" + self.operation:
            self.calls += 1
            if self.calls == 1:
                response.read()
                response.close()
                raise httpx.ReadError("Lost committed response", request=request)
        return response

    def close(self) -> None:
        self.inner.close()
