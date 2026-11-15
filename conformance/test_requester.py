"""Server-carried requester correlation and recovery against both backends."""

import time
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


def test_lost_create_reply_is_unmatched_and_retry_has_an_independent_origin(
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
            store.submit(flaky, domain, "work", reply_topic, {"input": "hello"})
    assert store.requests() == []
    with target.client(token) as api:
        first = api.own({"domain": domain, "topic": "work", "hold_seconds": 60})["task"]
        api.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": first["id"],
                "tasks": [{"topic": reply_topic, "data": {"result": "first"}}],
            }
        )
        retried = store.submit(api, domain, "work", reply_topic, {"input": "hello"})
        assert retried.origin_id != first["origin_id"]
        second = api.own({"domain": domain, "topic": "work", "hold_seconds": 60})[
            "task"
        ]
        assert second["origin_id"] == retried.origin_id
        api.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": second["id"],
                "tasks": [{"topic": reply_topic, "data": {"result": "retried"}}],
            }
        )
        restarted = Store("conformance-agent", tmp_path)
        reply = restarted.wait(api, retried.origin_id, 2, poll_interval=0.01)
        assert reply is not None and reply.creator == name and reply.matched
        assert reply.data["result"] == "retried"
        orphan = restarted.reply(first["origin_id"])
        assert orphan is not None and not orphan.matched
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": reply_topic, "hold_seconds": 60})
    with target.client(token, transport=NoRequests()) as offline:
        assert (
            Store("conformance-agent", tmp_path).wait(offline, retried.origin_id, 0)
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
        record = store.submit(api, domain, "work", reply_topic, {})
        task = api.own({"domain": domain, "topic": "work", "hold_seconds": 60})["task"]
        api.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": task["id"],
                "tasks": [{"topic": reply_topic, "data": {"result": "ok"}}],
            }
        )
    with target.client(
        token, transport=LostResponse(target.transport(), "delete")
    ) as flaky:
        with pytest.raises(TransportError):
            store.drain(flaky, domain, reply_topic)
    with target.client(token, transport=NoRequests()) as offline:
        reply = Store("conformance-agent", tmp_path).wait(offline, record.origin_id, 0)
        assert reply is not None and reply.data["result"] == "ok"
    with target.client(token) as api:
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": reply_topic, "hold_seconds": 60})


def test_redelivery_rotates_id_but_keeps_one_reply_per_origin(
    target: ServiceTarget, requester_identity: tuple[str, str, str], tmp_path: Path
) -> None:
    _, domain, token = requester_identity
    reply_topic = "reply/conformance-agent"
    store = Store("conformance-agent", tmp_path)

    class BeforeDelete(httpx.BaseTransport):
        def __init__(self, inner: httpx.BaseTransport) -> None:
            self.inner = inner

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/delete":
                raise httpx.ConnectError(
                    "Delete never reached the service", request=request
                )
            return self.inner.handle_request(request)

        def close(self) -> None:
            self.inner.close()

    with target.client(token) as api:
        origin = api.create(
            {"domain": domain, "topic": reply_topic, "data": {"result": "once"}}
        )["id"]
    with target.client(token, transport=BeforeDelete(target.transport())) as flaky:
        with pytest.raises(TransportError):
            store.drain(flaky, domain, reply_topic)
    saved = store.reply(origin)
    assert saved is not None
    with target.client(token) as api:
        api.renew(
            {
                "domain": domain,
                "topic": reply_topic,
                "id": saved.task_id,
                "hold_seconds": 1,
            }
        )
        time.sleep(1.05)
        assert store.drain(api, domain, reply_topic) == 1
    assert store.inbox() == [saved]


def test_fanout_conflicts_are_archived_without_replacing_the_answer(
    target: ServiceTarget, requester_identity: tuple[str, str, str], tmp_path: Path
) -> None:
    name, domain, token = requester_identity
    store = Store("conformance-agent", tmp_path)
    reply_topic = "reply/conformance-agent"
    with target.client(token) as api:
        request = store.submit(api, domain, "work", reply_topic, {})
        owned = api.own({"domain": domain, "topic": "work", "hold_seconds": 60})["task"]
        api.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": owned["id"],
                "tasks": [
                    {"topic": reply_topic, "data": {"answer": "a"}},
                    {"topic": reply_topic, "data": {"answer": "b"}},
                ],
            }
        )
        assert store.drain(api, domain, reply_topic) == 2
        selected = store.wait(api, request.origin_id, 0)
        assert selected is not None and not selected.duplicate
        records = Store("conformance-agent", tmp_path, initialize=False).inbox()
        assert {record.data["answer"] for record in records} == {"a", "b"}
        assert all(
            record.origin_id == request.origin_id and record.creator == name
            for record in records
        )
        assert sum(record.duplicate for record in records) == 1
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": reply_topic, "hold_seconds": 60})


def test_shared_reply_topic_isolates_direct_and_confused_deputy_forgeries(
    target: ServiceTarget, tmp_path: Path
) -> None:
    domain, reply_topic = target.domain, "reply/victim"
    _, requester_token = target.identity(create=("work",), own=(reply_topic,), read=())
    _, attacker_token = target.identity(create=("bounce", reply_topic), own=(), read=())
    worker_name, worker_token = target.identity(
        create=(reply_topic,), own=("work", "bounce"), read=()
    )
    store = Store("victim", tmp_path)
    with (
        target.client(requester_token) as requester,
        target.client(attacker_token) as attacker,
        target.client(worker_token) as worker,
    ):
        record = store.submit(requester, domain, "work", reply_topic, {})
        forged = attacker.create(
            {
                "domain": domain,
                "topic": reply_topic,
                "data": {
                    "request_id": record.origin_id,
                    "origin_id": record.origin_id,
                    "result": "forged",
                },
            }
        )
        bounced = attacker.create(
            {
                "domain": domain,
                "topic": "bounce",
                "data": {"reply_topic": reply_topic, "request_id": record.origin_id},
            }
        )
        held = worker.own({"domain": domain, "topic": "bounce", "hold_seconds": 60})[
            "task"
        ]
        worker.consume(
            {
                "domain": domain,
                "topic": "bounce",
                "id": held["id"],
                "tasks": [
                    {
                        "topic": reply_topic,
                        "data": {"request_id": record.origin_id, "result": "bounced"},
                    }
                ],
            }
        )
        held = worker.own({"domain": domain, "topic": "work", "hold_seconds": 60})[
            "task"
        ]
        worker.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": held["id"],
                "tasks": [{"topic": reply_topic, "data": {"result": "genuine"}}],
            }
        )
        store.drain(requester, domain, reply_topic)
        reply = store.wait(requester, record.origin_id, 0)
        assert reply is not None and reply.creator == worker_name
        assert reply.data == {"result": "genuine"}
        assert {entry.origin_id for entry in store.inbox()} == {
            record.origin_id,
            forged["id"],
            bounced["id"],
        }
