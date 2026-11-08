"""Requester recovery across real HTTP, SQLite and the durable local store."""

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tasklease.client import Client
from tasklease.errors import Empty, TransportError
from tasklease.store import Store


class DropCommittedResponse(httpx.BaseTransport):
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.dropped = False
        self.actual = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.actual.handle_request(request)
        if request.url.path == f"/v1/{self.operation}" and not self.dropped:
            self.dropped = True
            response.read()
            response.close()
            raise httpx.ReadError("Lost committed response", request=request)
        return response

    def close(self) -> None:
        self.actual.close()


class NoRequests(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("A durable local reply must need no HTTP request")


@pytest.fixture
def requester_identity(admin: Client) -> Iterator[tuple[str, str, str]]:
    name = "requester-" + uuid4().hex
    domain = "conformance/requester/" + uuid4().hex
    issued = admin.identity_add(
        {
            "name": name,
            "domains": [domain],
            "scopes": {"create": ["**"], "own": ["**"], "read": ["**"]},
            "operator": True,
        }
    )
    try:
        yield name, domain, issued["token"]
    finally:
        admin.identity_revoke({"name": name})


def test_lost_create_recovers_after_restart_and_deduplicates_replies(
    service_url: str,
    requester_identity: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    name, domain, token = requester_identity
    store = Store("conformance-agent", tmp_path)
    reply_topic = "reply/conformance-agent"
    with Client(service_url, token, transport=DropCommittedResponse("create")) as flaky:
        with pytest.raises(TransportError):
            store.submit(
                flaky, domain, "work", reply_topic, {"input": "hello"}, "lost-create"
            )
    record = store.request("lost-create")
    assert record is not None and record.state == "submission_unknown"
    assert record.task_id is None
    with Client(service_url, token) as api:
        task = api.own({"domain": domain, "topic": "work", "hold_seconds": 60})["task"]
        assert task["data"] == record.data
        api.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": task["id"],
                "tasks": [
                    {
                        "topic": reply_topic,
                        "data": {"request_id": "lost-create", "result": "first"},
                    }
                ],
            }
        )
        restarted = Store("conformance-agent", tmp_path)
        reply = restarted.wait(api, "lost-create", 2, poll_interval=0.01)
        assert reply is not None and reply.creator == name
        assert reply.data["result"] == "first"
        answered = restarted.request("lost-create")
        assert answered is not None and answered.state == "answered"
        api.create(
            {
                "domain": domain,
                "topic": reply_topic,
                "data": {
                    "request_id": "lost-create",
                    "result": "duplicate",
                },
            }
        )
        assert restarted.drain(api, domain, reply_topic) == 1
        saved = restarted.reply("lost-create")
        assert saved is not None and saved.data["result"] == "first"
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": reply_topic, "hold_seconds": 60})
    with Client(service_url, token, transport=NoRequests()) as offline:
        assert (
            Store("conformance-agent", tmp_path).wait(offline, "lost-create", 0)
            == reply
        )


def test_lost_delete_response_keeps_reply_durable(
    service_url: str,
    requester_identity: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    _, domain, token = requester_identity
    store = Store("conformance-agent", tmp_path)
    reply_topic = "reply/conformance-agent"
    with Client(service_url, token) as api:
        store.submit(api, domain, "work", reply_topic, {}, "lost-delete")
        task = api.own({"domain": domain, "topic": "work", "hold_seconds": 60})["task"]
        api.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": task["id"],
                "tasks": [
                    {
                        "topic": reply_topic,
                        "data": {"request_id": "lost-delete", "result": "ok"},
                    }
                ],
            }
        )
    with Client(service_url, token, transport=DropCommittedResponse("delete")) as flaky:
        with pytest.raises(TransportError):
            store.drain(flaky, domain, reply_topic)
    with Client(service_url, token, transport=NoRequests()) as offline:
        reply = Store("conformance-agent", tmp_path).wait(offline, "lost-delete", 0)
        assert reply is not None and reply.data["result"] == "ok"
    with Client(service_url, token) as api:
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": reply_topic, "hold_seconds": 60})
