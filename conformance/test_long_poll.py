"""Long-poll Own semantics, shared by the Worker and the in-process board."""

import threading
import time
from typing import cast

import pytest

from conformance.support import ServiceTarget
from tasklease.errors import Empty, Invalid
from tasklease.generated import protocol as p


@pytest.fixture
def poll_identity(target: ServiceTarget) -> tuple[str, str]:
    _, token = target.identity(create=("work/**",), own=("work/**",), read=("work/**",))
    return target.domain, token


def test_long_poll_wakes_promptly_on_create(
    target: ServiceTarget, poll_identity: tuple[str, str]
) -> None:
    domain, token = poll_identity
    with (
        target.client(token, "waiter") as waiter,
        target.client(token, "creator") as creator,
    ):
        waiting: list[p.Task] = []

        def own() -> None:
            request: p.OwnRequest = {
                "domain": domain,
                "topic": "work/wait",
                "hold_seconds": 60,
                "wait_seconds": 30,
            }
            waiting.append(waiter.own(request)["task"])

        thread = threading.Thread(target=own)
        thread.start()
        # The poll is recorded before it parks. Once it is listed, give it
        # time to park, so the Create below must reach it through a wakeup.
        listed = time.monotonic() + 10
        while not creator.topic_pollers({"domain": domain, "topic": "work/wait"})[
            "pollers"
        ]:
            assert time.monotonic() < listed
            time.sleep(0.05)
        time.sleep(0.5)
        assert thread.is_alive()
        created_at = time.monotonic()
        created = creator.create(
            {"domain": domain, "topic": "work/wait", "data": {"n": 1}}
        )["id"]
        thread.join(10)
        assert not thread.is_alive()
        # Well inside the 30-second wait: the Create woke the held poll.
        assert time.monotonic() - created_at < 5
        task = waiting[0]
        assert task["id"] != created
        assert task["data"] == {"n": 1}


def test_long_poll_returns_empty_at_its_deadline(
    target: ServiceTarget, poll_identity: tuple[str, str]
) -> None:
    domain, token = poll_identity
    with target.client(token) as client:
        started = time.monotonic()
        request: p.OwnRequest = {
            "domain": domain,
            "topic": "work/idle",
            "hold_seconds": 60,
            "wait_seconds": 1,
        }
        with pytest.raises(Empty):
            client.own(request)
        assert time.monotonic() - started < 10


def test_zero_wait_remains_immediate(
    target: ServiceTarget, poll_identity: tuple[str, str]
) -> None:
    domain, token = poll_identity
    with target.client(token) as client:
        started = time.monotonic()
        immediate: list[p.OwnRequest] = [
            {"domain": domain, "topic": "work/zero", "hold_seconds": 60},
            {
                "domain": domain,
                "topic": "work/zero",
                "hold_seconds": 60,
                "wait_seconds": 0,
            },
        ]
        for request in immediate:
            with pytest.raises(Empty):
                client.own(request)
        assert time.monotonic() - started < 2


def test_wait_seconds_bounds_are_rejected(
    target: ServiceTarget, poll_identity: tuple[str, str]
) -> None:
    domain, token = poll_identity
    with target.client(token) as client:
        for wait in (-1, 31):
            request = {
                "domain": domain,
                "topic": "work/bounds",
                "hold_seconds": 60,
                "wait_seconds": wait,
            }
            with pytest.raises(Invalid):
                client.own(cast(p.OwnRequest, request))


def test_competing_long_polls_claim_each_task_once(
    target: ServiceTarget, poll_identity: tuple[str, str]
) -> None:
    domain, token = poll_identity
    claims: list[p.Task] = []
    lock = threading.Lock()

    def waiter(index: int) -> None:
        with target.client(token, f"waiter-{index}") as client:
            request: p.OwnRequest = {
                "domain": domain,
                "topic": "work/compete",
                "hold_seconds": 60,
                "wait_seconds": 5,
            }
            task = client.own(request)["task"]
            with lock:
                claims.append(task)

    threads = [threading.Thread(target=waiter, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    # Four tasks for three waiters: every waiter claims one, and exactly one
    # task remains for a later short poll.
    with target.client(token, "creator") as creator:
        for value in range(4):
            creator.create(
                {"domain": domain, "topic": "work/compete", "data": {"n": value}}
            )
    for thread in threads:
        thread.join(15)
    assert len({task["id"] for task in claims}) == len(claims) == 3
    assert {cast(dict[str, object], task["data"])["n"] for task in claims} <= {
        0,
        1,
        2,
        3,
    }
    with target.client(token, "drain") as drain:
        remaining = drain.own(
            {"domain": domain, "topic": "work/compete", "hold_seconds": 60}
        )["task"]
    remaining_value = cast(dict[str, object], remaining["data"])["n"]
    assert remaining_value not in {
        cast(dict[str, object], task["data"])["n"] for task in claims
    }
