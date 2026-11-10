"""Requester recovery against both backends and the durable local store."""

from pathlib import Path

import httpx
import pytest

from conformance.support import LostResponse, ServiceTarget
from tasklease.errors import Empty, TransportError
from tasklease.store import Store


class NoRequests(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("A durable local reply must need no HTTP request")


@pytest.fixture
def requester_identity(target: ServiceTarget) -> tuple[str, str, str]:
    name, token = target.identity()
    return name, target.domain, token


def test_lost_create_recovers_after_restart_and_deduplicates_replies(
    target: ServiceTarget,
    requester_identity: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    name, domain, token = requester_identity
    store = Store("conformance-agent", tmp_path)
    reply_topic = "reply/conformance-agent"
    with target.client(
        token, transport=LostResponse(target.transport(), "create")
    ) as flaky:
        with pytest.raises(TransportError):
            store.submit(
                flaky, domain, "work", reply_topic, {"input": "hello"}, "lost-create"
            )
    record = store.request("lost-create")
    assert record is not None and record.state == "submission_unknown"
    assert record.task_id is None
    with target.client(token) as api:
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
    with target.client(token, transport=NoRequests()) as offline:
        assert (
            Store("conformance-agent", tmp_path).wait(offline, "lost-create", 0)
            == reply
        )


def test_lost_delete_response_keeps_reply_durable(
    target: ServiceTarget,
    requester_identity: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    _, domain, token = requester_identity
    store = Store("conformance-agent", tmp_path)
    reply_topic = "reply/conformance-agent"
    with target.client(token) as api:
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
    with target.client(
        token, transport=LostResponse(target.transport(), "delete")
    ) as flaky:
        with pytest.raises(TransportError):
            store.drain(flaky, domain, reply_topic)
    with target.client(token, transport=NoRequests()) as offline:
        reply = Store("conformance-agent", tmp_path).wait(offline, "lost-delete", 0)
        assert reply is not None and reply.data["result"] == "ok"
    with target.client(token) as api:
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": reply_topic, "hold_seconds": 60})
