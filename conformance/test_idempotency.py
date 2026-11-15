"""The same Create retry semantics on SQLite and the offline board."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conformance.support import LostResponse, ServiceTarget
from tasklease.errors import Empty, TransportError
from tasklease.generated import protocol as p
from tasklease.names import new_idempotency_key
from tasklease.store import Store


@pytest.mark.parametrize("transition", ["queued", "own", "consume", "delete", "cancel"])
def test_replay_recovers_origin_across_task_lifecycle(
    target: ServiceTarget, transition: str
) -> None:
    _, token = target.identity()
    request: p.CreateRequest = {
        "domain": target.domain,
        "topic": "work",
        "data": {},
        "idempotency_key": new_idempotency_key(),
    }
    with target.client(token) as api:
        origin = api.create(request)["id"]
        live = origin
        if transition in {"own", "consume", "delete"}:
            live = api.own(
                {"domain": target.domain, "topic": "work", "hold_seconds": 60}
            )["task"]["id"]
            assert live != origin
        if transition == "consume":
            api.consume(
                {
                    "domain": target.domain,
                    "topic": "work",
                    "id": live,
                    "tasks": [{"topic": "reply", "data": {"answer": True}}],
                }
            )
        elif transition == "delete":
            api.delete({"domain": target.domain, "topic": "work", "id": live})
        elif transition == "cancel":
            api.cancel({"domain": target.domain, "topic": "work", "origin_id": origin})
        assert api.create(request)["id"] == origin
        if transition == "queued":
            assert (
                api.own({"domain": target.domain, "topic": "work", "hold_seconds": 60})[
                    "task"
                ]["origin_id"]
                == origin
            )
        with pytest.raises(Empty):
            api.own({"domain": target.domain, "topic": "work", "hold_seconds": 60})
        if transition == "consume":
            assert (
                api.own(
                    {"domain": target.domain, "topic": "reply", "hold_seconds": 60}
                )["task"]["origin_id"]
                == origin
            )


def test_concurrent_keys_keep_first_request_and_isolate_creators(
    target: ServiceTarget,
) -> None:
    first_name, token = target.identity()
    _, other_token = target.identity()
    request: p.CreateRequest = {
        "domain": target.domain,
        "topic": "work",
        "data": {"first": True},
        "idempotency_key": new_idempotency_key(),
    }
    with target.client(token) as api, target.client(other_token) as other:
        with ThreadPoolExecutor(max_workers=8) as pool:
            origins = list(pool.map(lambda _: api.create(request)["id"], range(20)))
        assert len(set(origins)) == 1
        origin = origins[0]
        assert (
            api.create({**request, "topic": "different", "data": {"changed": True}})[
                "id"
            ]
            == origin
        )
        task = api.read({"domain": target.domain, "topic": "work", "id": origin})[
            "task"
        ]
        assert task["data"] == {"first": True} and task["creator"] == first_name
        assert (
            api.tasks_list({"domain": target.domain, "topic": "different"})["tasks"]
            == []
        )
        assert other.create(request)["id"] != origin
        assert (
            len(api.tasks_list({"domain": target.domain, "topic": "work"})["tasks"])
            == 2
        )


def test_lost_create_replay_recovers_a_reply_drained_before_intent(
    target: ServiceTarget, tmp_path: Path
) -> None:
    _, token = target.identity()
    store = Store("keyed-requester", tmp_path)
    key = new_idempotency_key()
    with target.client(
        token, transport=LostResponse(target.transport(), "create")
    ) as flaky:
        with pytest.raises(TransportError):
            store.submit(flaky, target.domain, "work", "reply", {}, idempotency_key=key)
    assert store.requests() == []
    with target.client(token) as api:
        held = api.own({"domain": target.domain, "topic": "work", "hold_seconds": 60})[
            "task"
        ]
        api.consume(
            {
                "domain": target.domain,
                "topic": "work",
                "id": held["id"],
                "tasks": [{"topic": "reply", "data": {"answer": "done"}}],
            }
        )
        assert store.drain(api, target.domain, "reply") == 1
        unmatched = store.reply(held["origin_id"])
        assert unmatched is not None and not unmatched.matched
        recovered = store.submit(
            api, target.domain, "work", "reply", {}, idempotency_key=key
        )
        assert (
            recovered.origin_id == held["origin_id"] and recovered.state == "answered"
        )
        reply = store.wait(api, recovered.origin_id, 0)
        assert reply is not None and reply.data == {"answer": "done"}
        with pytest.raises(Empty):
            api.own({"domain": target.domain, "topic": "work", "hold_seconds": 60})
