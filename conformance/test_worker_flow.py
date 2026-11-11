"""Worker/requester completion and faults through both service implementations."""

import json
from pathlib import Path
from threading import Event

import pytest

from conformance.support import LostResponse, ServiceTarget
from tasklease.errors import Empty
from tasklease.generated import protocol as p
from tasklease.store import Store
from tasklease.worker import EchoHandler, UnlimitedCapacity, WorkerLoop


@pytest.fixture
def worker_identity(target: ServiceTarget) -> tuple[str, str]:
    _, token = target.identity(
        create=("work", "reply/**"),
        own=("work", "work/error", "reply/**"),
        read=("**",),
    )
    return target.domain, token


def test_echo_reply_survives_lost_consume_response_without_error_duplicate(
    target: ServiceTarget, worker_identity: tuple[str, str], tmp_path: Path
) -> None:
    domain, token = worker_identity
    store = Store("echo-requester", tmp_path)
    transport = LostResponse(target.transport(), "consume")
    with target.client(token) as requester:
        store.submit(
            requester,
            domain,
            "work",
            "reply/echo-requester",
            {"message": "hello from requester", "large_unused": "x" * 200_000},
            "lost-completion",
        )
        with target.client(token, "echo-worker", transport=transport) as api:
            loop = WorkerLoop(api, domain, "work", UnlimitedCapacity(), EchoHandler())
            assert loop.step() == "worked"
            assert transport.calls == 2
            assert (
                WorkerLoop(
                    api,
                    domain,
                    "work",
                    UnlimitedCapacity(),
                    EchoHandler(),
                    wait_seconds=0,
                ).step()
                == "empty"
            )
        reply = store.wait(requester, "lost-completion", 2, poll_interval=0.01)
        assert reply is not None
        assert reply.data["request_id"] == "lost-completion"
        assert reply.data["message"] == "hello from requester"
        assert "large_unused" not in reply.data
        for topic in ("reply/echo-requester", "work/error"):
            with pytest.raises(Empty):
                requester.own({"domain": domain, "topic": topic, "hold_seconds": 60})


def test_lost_renew_response_retries_before_short_local_deadline(
    target: ServiceTarget, worker_identity: tuple[str, str], tmp_path: Path
) -> None:
    class SlowEcho(EchoHandler):
        def run(self, request: str, lost: Event) -> p.Data:
            assert not lost.wait(1.25), "The worker lost its renewable short lease"
            return super().run(request, lost)

    domain, token = worker_identity
    store = Store("echo-requester", tmp_path)
    transport = LostResponse(target.transport(), "renew")
    with target.client(token) as requester:
        store.submit(
            requester,
            domain,
            "work",
            "reply/echo-requester",
            {"message": "short lease"},
            "lost-renewal",
        )
        with target.client(token, "slow-echo", transport=transport) as api:
            loop = WorkerLoop(
                api,
                domain,
                "work",
                UnlimitedCapacity(),
                SlowEcho(),
                lease=1,
                renew_every=0.1,
            )
            assert loop.step() == "worked"
            assert transport.calls >= 2
        reply = store.wait(requester, "lost-renewal", 2, poll_interval=0.01)
        assert reply is not None and reply.data["status"] == "ok"
        assert reply.data["message"] == "short lease"


def test_forbidden_reply_routes_original_to_automatic_error_grant(
    target: ServiceTarget, worker_identity: tuple[str, str]
) -> None:
    domain, token = worker_identity
    data: p.Data = {"message": "hello", "reply_topic": "denied", "request_id": "denied"}
    with target.client(token, "echo-worker") as api:
        api.create({"domain": domain, "topic": "work", "data": data})
        loop = WorkerLoop(api, domain, "work", UnlimitedCapacity(), EchoHandler())
        assert loop.step() == "worked"
        assert (
            WorkerLoop(
                api,
                domain,
                "work",
                UnlimitedCapacity(),
                EchoHandler(),
                wait_seconds=0,
            ).step()
            == "empty"
        )
        error = api.own({"domain": domain, "topic": "work/error", "hold_seconds": 60})[
            "task"
        ]
        assert error["data"]["original"] == data
        assert "error" in error["data"]
        api.delete({"domain": domain, "topic": "work/error", "id": error["id"]})
        with pytest.raises(Empty):
            api.own({"domain": domain, "topic": "work/error", "hold_seconds": 60})


def test_unencodable_wire_data_drains_through_ascii_bounded_fallback(
    target: ServiceTarget, worker_identity: tuple[str, str]
) -> None:
    domain, token = worker_identity
    # Escaped isolated surrogates are legal wire JSON, even though Python's
    # ordinary UTF-8 request serializer cannot send the decoded string.
    payload = {
        "domain": domain,
        "topic": "work",
        "data": {
            "message": "hello",
            "reply_topic": "reply/echo-requester",
            "request_id": "\ud800",
        },
    }
    with target.http() as http:
        response = http.post(
            target.url + "/v1/create",
            headers={
                "Authorization": "Bearer " + token,
                "X-Worker": "raw-requester",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload, ensure_ascii=True).encode("ascii"),
        )
    assert response.status_code == 200
    with target.client(token, "echo-worker") as api:
        loop = WorkerLoop(api, domain, "work", UnlimitedCapacity(), EchoHandler())
        assert loop.step() == "worked"
        assert (
            WorkerLoop(
                api,
                domain,
                "work",
                UnlimitedCapacity(),
                EchoHandler(),
                wait_seconds=0,
            ).step()
            == "empty"
        )
        error = api.own({"domain": domain, "topic": "work/error", "hold_seconds": 60})[
            "task"
        ]
        assert error["data"]["truncated"] is True
        original = error["data"]["original"]
        assert isinstance(original, str) and "\\ud800" in original
        assert len(json.dumps(error["data"], ensure_ascii=False).encode()) <= 128 * 1024
        api.delete({"domain": domain, "topic": "work/error", "id": error["id"]})
