"""Wire behavior, typed operation mapping and controlled client failures."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import httpx
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from tasklease.client import Client
from tasklease.errors import (
    API_ERRORS,
    APIError,
    Invalid,
    ProtocolError,
    TransportError,
)
from tasklease.generated import protocol as p

SCHEMA = Path(__file__).resolve().parents[2] / "schema"
TOKEN = "tl_test-secret"
type Schema = bool | Mapping[str, object]

# The casts adapt corpus objects to the generated request DTOs. Calling every
# public method separately also checks their declared request/response API.
CALLS: dict[str, Callable[[Client, dict[str, object]], object]] = {
    "create": lambda c, r: c.create(cast(p.CreateRequest, r)),
    "own": lambda c, r: c.own(cast(p.OwnRequest, r)),
    "renew": lambda c, r: c.renew(cast(p.RenewRequest, r)),
    "read": lambda c, r: c.read(cast(p.ReadRequest, r)),
    "delete": lambda c, r: c.delete(cast(p.DeleteRequest, r)),
    "consume": lambda c, r: c.consume(cast(p.ConsumeRequest, r)),
    "topics.list": lambda c, r: c.topics_list(cast(p.TopicsListRequest, r)),
    "tasks.list": lambda c, r: c.tasks_list(cast(p.TasksListRequest, r)),
    "topic.pollers": lambda c, r: c.topic_pollers(cast(p.TopicPollersRequest, r)),
    "topic.declare": lambda c, r: c.topic_declare(cast(p.TopicDeclareRequest, r)),
    "blob.put_url": lambda c, r: c.blob_put_url(cast(p.BlobPutUrlRequest, r)),
    "blob.get_url": lambda c, r: c.blob_get_url(cast(p.BlobGetUrlRequest, r)),
    "identity.add": lambda c, r: c.identity_add(cast(p.IdentityAddRequest, r)),
    "identity.revoke": lambda c, r: c.identity_revoke(cast(p.IdentityRevokeRequest, r)),
    "identity.rotate": lambda c, r: c.identity_rotate(cast(p.IdentityRotateRequest, r)),
    "identity.list": lambda c, r: c.identity_list(cast(p.IdentityListRequest, r)),
    "identity.whoami": lambda c, r: c.identity_whoami(cast(p.IdentityWhoamiRequest, r)),
    "monitor.snapshot": lambda c, r: c.monitor_snapshot(
        cast(p.MonitorSnapshotRequest, r)
    ),
}


def read_object(path: Path) -> dict[str, object]:
    result: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return cast(dict[str, object], result)


def example(operation: str, kind: str) -> dict[str, object]:
    body = read_object(SCHEMA / "examples" / operation / "valid" / f"{kind}.json")[
        "body"
    ]
    assert isinstance(body, dict)
    return cast(dict[str, object], body)


@pytest.fixture(scope="module")
def registry() -> Registry[Schema]:
    resources: list[tuple[str, Resource[Schema]]] = []
    for path in [SCHEMA / "common.json", *sorted((SCHEMA / "ops").glob("*.json"))]:
        schema = read_object(path)
        identifier = schema["$id"]
        assert isinstance(identifier, str)
        resources.append((identifier, Resource.from_contents(schema)))
    return Registry[Schema]().with_resources(resources)


@pytest.mark.parametrize("operation", sorted(CALLS))
def test_every_operation_uses_its_wire_name_and_full_typed_response(
    operation: str, registry: Registry[Schema]
) -> None:
    request_body = example(operation, "request")
    response_body = example(operation, "response")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == f"https://tasks.example/v1/{operation}"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-Worker"] == "test-worker"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == request_body
        return httpx.Response(200, json=response_body)

    with Client(
        "https://tasks.example/",
        TOKEN,
        "test-worker",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = CALLS[operation](client, request_body)
    assert result == response_body
    identifier = read_object(SCHEMA / "ops" / f"{operation}.json")["$id"]
    assert isinstance(identifier, str)
    Draft202012Validator(
        {"$ref": f"{identifier}#/$defs/Response"}, registry=registry
    ).validate(result)
    assert len(requests) == 1


def test_version_is_an_unauthenticated_get(registry: Registry[Schema]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/version"
        assert "authorization" not in request.headers
        assert "x-worker" not in request.headers
        assert not request.content
        return httpx.Response(200, json=example("version", "response"))

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        result = c.version()
    identifier = read_object(SCHEMA / "ops" / "version.json")["$id"]
    assert isinstance(identifier, str)
    Draft202012Validator(
        {"$ref": f"{identifier}#/$defs/Response"}, registry=registry
    ).validate(result)


@pytest.mark.parametrize("error_type", list(API_ERRORS.values()), ids=list(API_ERRORS))
def test_error_codes_have_distinct_exceptions(error_type: type[APIError]) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            error_type.status_code,
            json={
                "ok": False,
                "error": {"code": error_type.code, "message": "refused"},
            },
        )

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(error_type, match="refused") as caught:
            c.identity_whoami({})
    assert caught.value.code == error_type.code
    assert caught.value.status_code == error_type.status_code
    assert attempts == 1


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError]
)
def test_network_failures_are_controlled_and_never_retried(
    error_type: type[httpx.RequestError],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_type(f"unsafe diagnostic {TOKEN}", request=request)

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(TransportError) as caught:
            c.create({"domain": "acme", "topic": "work", "data": {}})
    assert TOKEN not in str(caught.value)
    assert caught.value.status_code is None
    assert attempts == 1


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_failures_are_transport_errors_without_a_retry(status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, text=f"unsafe diagnostic {TOKEN}")

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(TransportError) as caught:
            c.identity_whoami({})
    assert caught.value.status_code == status
    assert TOKEN not in str(caught.value)
    assert attempts == 1


@pytest.mark.parametrize(
    ("status", "content"),
    [
        (200, b"not JSON"),
        (200, b"[]"),
        (200, b"null"),
        (200, b"{}"),
        (200, b'{"ok":1,"id":"t_test"}'),
        (200, b'{"ok":true}'),
        (200, b'{"ok":true,"id":[]}'),
        (200, b'{"ok":true,"id":NaN}'),
        (400, b'{"ok":true,"id":"t_test"}'),
        (400, b'{"ok":false}'),
        (400, b'{"ok":false,"error":{"code":"future","message":"bad"}}'),
        (400, b'{"ok":false,"error":{"code":"invalid","message":12}}'),
        (401, b'{"ok":false,"error":{"code":"invalid","message":"bad"}}'),
    ],
)
def test_malformed_responses_raise_protocol_errors(status: int, content: bytes) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, content=content))
    with Client("https://tasks.example", TOKEN, transport=transport) as client:
        with pytest.raises(ProtocolError):
            client.create({"domain": "acme", "topic": "work", "data": {}})


def test_redirects_do_not_send_the_credential_to_another_host() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307, headers={"location": "https://other.example/v1/create"}
        )

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(ProtocolError):
            c.create({"domain": "acme", "topic": "work", "data": {}})
    assert calls == 1


def test_server_error_messages_redact_the_credential() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            401,
            json={"ok": False, "error": {"code": "unauthenticated", "message": TOKEN}},
        )
    )
    with Client("https://tasks.example", TOKEN, transport=transport) as client:
        with pytest.raises(APIError) as caught:
            client.identity_whoami({})
    assert str(caught.value) == "[redacted]"


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf"), b"bytes", "\ud800"]
)
def test_unserializable_json_is_refused_before_sending(value: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid JSON must not be sent")

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(Invalid, match="valid JSON"):
            c.create(
                {
                    "domain": "acme",
                    "topic": "work",
                    "data": {"v": cast(p.JsonValue, value)},
                }
            )


def test_json_uses_compact_utf8_bytes_and_accurate_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.content
            == '{"domain":"acme","topic":"work","data":{"text":"é😃"}}'.encode()
        )
        assert int(request.headers["content-length"]) == len(request.content)
        return httpx.Response(200, json={"ok": True, "id": "t_test"})

    with Client(
        "https://tasks.example", TOKEN, transport=httpx.MockTransport(handler)
    ) as c:
        c.create({"domain": "acme", "topic": "work", "data": {"text": "é😃"}})


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"protocol": "v2", "service_version": "0.1.0"},
        {"protocol": "v1", "service_version": 1},
    ],
)
def test_malformed_version_is_controlled(body: dict[str, object]) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    with Client("https://tasks.example", TOKEN, transport=transport) as client:
        with pytest.raises(ProtocolError):
            client.version()


def test_close_releases_the_owned_transport() -> None:
    class Transport(httpx.BaseTransport):
        closed = False

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        def close(self) -> None:
            self.closed = True

    transport = Transport()
    client = Client("https://tasks.example", TOKEN, transport=transport)
    client.close()
    assert transport.closed


@pytest.mark.parametrize("token", ["", "has spaces", "nonascii-é", "line\nbreak"])
def test_invalid_credentials_are_refused_without_echoing_them(token: str) -> None:
    with pytest.raises(Invalid) as caught:
        Client("https://tasks.example", token)
    if token:
        assert token not in str(caught.value)


@pytest.mark.parametrize("label", ["", "has spaces", "a" * 257, "line\nbreak"])
def test_invalid_worker_labels_are_refused(label: str) -> None:
    with pytest.raises(Invalid):
        Client("https://tasks.example", TOKEN, label)
