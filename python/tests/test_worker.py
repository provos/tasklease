"""Worker capacity, lease fencing and guaranteed bounded error completion."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast

import httpx
import pytest

from tasklease.client import Client
from tasklease.errors import Forbidden, Invalid, NotFound, TooLarge, TransportError
from tasklease.generated import protocol as p
from tasklease.worker import (
    EchoHandler,
    Rejection,
    UnlimitedCapacity,
    WorkerLoop,
    bounded_error,
    validate_work_topic,
)

DOMAIN = "example/project"
TOPIC = "work"
REPLY = "replies/agent"
TASK = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def task(data: p.Data | None = None, *, owned_count: int = 1) -> p.Task:
    return {
        "id": TASK,
        "domain": DOMAIN,
        "topic": TOPIC,
        "creator": "requester",
        "created_at": 1000,
        "lease_until": 601000,
        "owned_count": owned_count,
        "owner": {"identity": "worker", "worker": "test"},
        "owned_at": 1000,
        "data": (
            data
            if data is not None
            else {"request_id": "req", "reply_topic": REPLY, "message": "hello"}
        ),
    }


class Backend:
    def __init__(self) -> None:
        self.task = task()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.on_own: Callable[[], None] = lambda: None
        self.on_renew: Callable[[], None] = lambda: None
        self.on_consume: Callable[[dict[str, object]], None] = lambda request: None
        self.empty = False
        self.renew_errors: list[Exception] = []
        self.consume_errors: list[Exception] = []
        self.timeouts: list[dict[str, float]] = []

    def transport(self, request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        operation = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((operation, body))
        self.timeouts.append(cast(dict[str, float], request.extensions["timeout"]))
        if operation == "own":
            self.on_own()
            if self.empty:
                return httpx.Response(
                    404,
                    json={"ok": False, "error": {"code": "empty", "message": "empty"}},
                )
            response: object = {"ok": True, "task": self.task}
        elif operation == "renew":
            self.on_renew()
            if self.renew_errors:
                raise self.renew_errors.pop(0)
            response = {"ok": True, "task": self.task}
        else:
            assert operation == "consume"
            self.on_consume(body)
            if self.consume_errors:
                raise self.consume_errors.pop(0)
            response = {"ok": True, "ids": []}
        return httpx.Response(
            200, content=json.dumps(response, ensure_ascii=True).encode()
        )

    def client(self) -> Client:
        return Client(
            "https://tasks.example",
            "secret",
            transport=httpx.MockTransport(self.transport),
        )

    @property
    def consumed(self) -> list[dict[str, object]]:
        return [body for operation, body in self.calls if operation == "consume"]


class CustomHandler:
    def __init__(self) -> None:
        self.on_validate: Callable[[p.Task], object | Rejection] = lambda task: task
        self.on_run: Callable[[object, threading.Event], object] = lambda r, lost: {}
        self.on_reply: Callable[[p.Task, object], list[p.NewTask]] = lambda t, r: [
            {"topic": REPLY, "data": {"result": cast(p.JsonValue, r)}}
        ]

    def validate(self, task: p.Task) -> object | Rejection:
        return self.on_validate(task)

    def run(self, request: object, lost: threading.Event) -> object:
        return self.on_run(request, lost)

    def reply(self, task: p.Task, outcome: object) -> list[p.NewTask]:
        return self.on_reply(task, outcome)


def loop(
    backend: Backend, handler: CustomHandler | None = None, *, wait_seconds: int = 30
) -> WorkerLoop[object, object]:
    return WorkerLoop(
        backend.client(),
        DOMAIN,
        TOPIC,
        UnlimitedCapacity(),
        handler or CustomHandler(),
        wait_seconds=wait_seconds,
    )


def new_tasks(request: dict[str, object]) -> list[p.NewTask]:
    return cast(list[p.NewTask], request["tasks"])


def test_echo_only_identifying_fields_and_small_message() -> None:
    backend = Backend()
    backend.task["data"]["unrelated"] = "x" * 200_000
    worker = WorkerLoop(
        backend.client(), DOMAIN, TOPIC, UnlimitedCapacity(), EchoHandler()
    )
    assert worker.step() == "worked"
    assert new_tasks(backend.consumed[0]) == [
        {
            "topic": REPLY,
            "data": {"request_id": "req", "status": "ok", "message": "hello"},
        }
    ]
    assert backend.calls[0][0] == "own"
    assert backend.calls[0][1]["hold_seconds"] == 600


def test_capacity_acquired_before_own_and_released_after_finish() -> None:
    backend = Backend()
    events: list[str] = []
    stop = threading.Event()

    class Capacity:
        @contextmanager
        def acquire(self) -> Iterator[None]:
            events.append("acquire")
            try:
                yield
            finally:
                events.append("release")

    backend.on_own = lambda: events.append("own")

    def consume(request: dict[str, object]) -> None:
        events.append("consume")
        stop.set()

    backend.on_consume = consume
    WorkerLoop(
        backend.client(), DOMAIN, TOPIC, Capacity(), EchoHandler(), stop=stop
    ).run()
    assert events == ["acquire", "own", "consume", "release"]


def test_stop_while_capacity_pending_does_not_own() -> None:
    backend = Backend()
    stop = threading.Event()

    class Capacity:
        @contextmanager
        def acquire(self) -> Iterator[None]:
            stop.set()
            yield

    WorkerLoop(
        backend.client(), DOMAIN, TOPIC, Capacity(), EchoHandler(), stop=stop
    ).run()
    assert backend.calls == []


def test_step_uses_external_capacity_and_empty_is_bounded() -> None:
    backend = Backend()
    backend.empty = True

    class NoAcquire:
        def acquire(self) -> None:
            pytest.fail("step must not acquire capacity")

    worker = WorkerLoop(
        backend.client(),
        DOMAIN,
        TOPIC,
        cast(UnlimitedCapacity, NoAcquire()),
        EchoHandler(),
    )
    assert worker.step() == "empty"
    assert [op for op, _ in backend.calls] == ["own"]


@pytest.mark.parametrize("topic", ["work/error", "work/error/error", "x y", "x/", "X"])
def test_bad_work_topics_rejected_before_network(topic: str) -> None:
    with pytest.raises(Invalid):
        validate_work_topic(topic)
    backend = Backend()
    with pytest.raises(Invalid):
        WorkerLoop(backend.client(), DOMAIN, topic, UnlimitedCapacity(), EchoHandler())
    assert not backend.calls


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("lease", 0),
        ("lease", 86401),
        ("lease", True),
        ("renew_every", 0),
        ("renew_every", 600),
        ("renew_every", float("nan")),
        ("renew_every", 10**1000),
        ("max_owned", 0),
        ("max_owned", True),
        ("poll_interval", float("inf")),
        ("poll_interval", 0),
        ("poll_interval", 10**1000),
    ],
)
def test_invalid_settings(setting: str, value: int | float) -> None:
    kwargs = {setting: value}
    with pytest.raises(Invalid):
        WorkerLoop(
            Backend().client(),
            DOMAIN,
            TOPIC,
            UnlimitedCapacity(),
            EchoHandler(),
            lease=cast(int, kwargs.get("lease", 600)),
            renew_every=kwargs.get("renew_every", 180),
            max_owned=cast(int, kwargs.get("max_owned", 3)),
            poll_interval=kwargs.get("poll_interval", 30),
        )


@pytest.mark.parametrize("stage", ["validate", "run", "reply"])
def test_handler_exception_ends_in_error_or_small_failure_reply(stage: str) -> None:
    backend = Backend()
    handler = CustomHandler()

    def failure(*args: object) -> object:
        raise RuntimeError("handler failed")

    if stage == "validate":
        handler.on_validate = failure
    elif stage == "run":
        handler.on_run = failure
    else:
        handler.on_reply = cast(Callable[[p.Task, object], list[p.NewTask]], failure)
    loop(backend, handler).step()
    reply = new_tasks(backend.consumed[0])[0]
    assert reply["topic"] == (TOPIC + "/error" if stage == "reply" else REPLY)
    assert "handler failed" in json.dumps(reply)


@pytest.mark.parametrize("status", ["rejected", "abandoned"])
def test_rejected_and_max_owned_never_run(status: str) -> None:
    backend = Backend()
    handler = CustomHandler()
    handler.on_run = lambda r, lost: pytest.fail("must not run")
    if status == "abandoned":
        backend.task["owned_count"] = 4
    else:
        handler.on_validate = lambda task: Rejection("no")
    loop(backend, handler).step()
    assert new_tasks(backend.consumed[0])[0]["data"]["status"] == status


@pytest.mark.parametrize("reply_topic", [None, "bad topic", "UPPER", "/", 42])
def test_echo_without_valid_reply_topic_routes_error(reply_topic: p.JsonValue) -> None:
    backend = Backend()
    backend.task["data"]["reply_topic"] = reply_topic
    WorkerLoop(
        backend.client(), DOMAIN, TOPIC, UnlimitedCapacity(), EchoHandler()
    ).step()
    reply = new_tasks(backend.consumed[0])[0]
    assert reply["topic"] == TOPIC + "/error"
    assert reply["data"]["original"] == backend.task["data"]
    assert "Missing or invalid reply_topic" in cast(str, reply["data"]["error"])


@pytest.mark.parametrize("count", [0, 1, 2, 16])
def test_pipeline_stage_without_reply_topic_posts_its_successors(count: int) -> None:
    backend = Backend()
    backend.task["data"] = {"url": "https://a.example"}
    handler = CustomHandler()
    successors: list[p.NewTask] = [
        {"topic": f"classify/stage-{index}", "data": {"url": "https://a.example"}}
        for index in range(count)
    ]
    handler.on_reply = lambda task, outcome: successors
    loop(backend, handler).step()
    assert len(backend.consumed) == 1
    assert new_tasks(backend.consumed[0]) == successors


@pytest.mark.parametrize("stage", ["validate", "run", "rejected", "abandoned"])
def test_failure_without_reply_topic_goes_to_the_error_topic(stage: str) -> None:
    backend = Backend()
    backend.task["data"] = {"url": 1}
    handler = CustomHandler()

    def failure(*args: object) -> object:
        raise RuntimeError("handler failed")

    if stage == "validate":
        handler.on_validate = failure
    elif stage == "run":
        handler.on_run = failure
    elif stage == "rejected":
        handler.on_validate = lambda task: Rejection("url must be text")
    else:
        backend.task["owned_count"] = 4
    handler.on_reply = lambda task, outcome: pytest.fail("failures skip reply")
    loop(backend, handler).step()
    assert len(backend.consumed) == 1
    reply = new_tasks(backend.consumed[0])[0]
    assert reply["topic"] == TOPIC + "/error"
    assert reply["data"]["original"] == backend.task["data"]
    assert reply["data"]["error"] == "Failed, and the task has no reply_topic"
    status = {"validate": "failed", "run": "failed"}.get(stage, stage)
    assert cast(p.Data, reply["data"]["outcome"])["status"] == status


@pytest.mark.parametrize(
    "error", [Forbidden("denied"), Invalid("invalid"), TooLarge("big")]
)
def test_refused_reply_falls_back(error: Exception) -> None:
    backend = Backend()
    backend.consume_errors = [error]
    loop(backend).step()
    assert len(backend.consumed) == 2
    assert new_tasks(backend.consumed[1])[0]["topic"] == TOPIC + "/error"
    assert backend.consumed[0]["id"] == backend.consumed[1]["id"] == TASK


def test_lost_consume_reply_retries_same_id_without_error_fallback() -> None:
    backend = Backend()
    backend.consume_errors = [TransportError("lost reply"), NotFound("committed")]
    loop(backend).step()
    assert len(backend.consumed) == 2
    assert backend.consumed[0] == backend.consumed[1]


def test_too_large_error_uses_escaping_aware_bounded_payload() -> None:
    backend = Backend()
    backend.task["data"]["payload"] = '\x00\\"' * 90_000
    backend.consume_errors = [TooLarge("reply"), TooLarge("error wrapper")]
    loop(backend).step()
    data = new_tasks(backend.consumed[2])[0]["data"]
    assert data["truncated"] is True
    assert (
        len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
        <= 128 * 1024
    )
    assert cast(str, data["original"]).startswith('{"request_id":"req"')


@pytest.mark.parametrize("kind", ["exception", "rejection", "request_id", "outcome"])
def test_unencodable_values_still_finish_with_bounded_error(kind: str) -> None:
    backend = Backend()
    handler = CustomHandler()
    if kind == "exception":

        def fail(request: object, lost: threading.Event) -> object:
            raise ValueError("\ud800")

        handler.on_run = fail
    elif kind == "rejection":
        handler.on_validate = lambda task: Rejection("\ud800")
    elif kind == "request_id":
        backend.task["data"]["request_id"] = "\ud800"
        handler.on_reply = lambda task, outcome: EchoHandler().reply(task, {})
    else:
        handler.on_run = lambda r, lost: {"invalid": "\ud800"}
    loop(backend, handler).step()
    data = new_tasks(backend.consumed[-1])[0]["data"]
    assert new_tasks(backend.consumed[-1])[0]["topic"] == TOPIC + "/error"
    assert data["truncated"] is True
    json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")


@pytest.mark.parametrize("value", [float("nan"), b"bytes", object()])
def test_non_json_outcome_falls_back_without_wedging(value: object) -> None:
    backend = Backend()
    handler = CustomHandler()
    handler.on_run = lambda request, lost: value
    loop(backend, handler).step()
    assert new_tasks(backend.consumed[-1])[0]["data"]["truncated"] is True


def test_bounded_error_limits_error_and_handles_json_escaping() -> None:
    value = bounded_error(
        {"v": '\x00\\"' * 100_000}, {"v": "😃" * 100_000}, "\ud800" * 9999
    )
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= 128 * 1024
    assert cast(str, value["original"]).startswith('{"v":')


def test_renewal_keeps_id_and_transport_error_retries() -> None:
    backend = Backend()
    handler = CustomHandler()
    renewals = threading.Event()
    backend.renew_errors = [TransportError("temporary")]
    count = 0

    def renew() -> None:
        nonlocal count
        count += 1
        if count == 2:
            renewals.set()

    backend.on_renew = renew

    def run(request: object, lost: threading.Event) -> object:
        assert renewals.wait(3)
        assert not lost.is_set()
        return {}

    handler.on_run = run
    worker = loop(backend, handler)
    worker.renew_every = 0.02
    worker.step()
    calls = [body for op, body in backend.calls if op == "renew"]
    assert len(calls) >= 2
    assert {body["id"] for body in calls} == {TASK}
    assert backend.consumed


def test_short_lease_retries_failed_renew_before_deadline() -> None:
    backend = Backend()
    backend.renew_errors = [TransportError("lost first renewal response")]
    handler = CustomHandler()
    renewals = threading.Event()
    count = 0

    def renew() -> None:
        nonlocal count
        count += 1
        if count >= 2:
            renewals.set()

    backend.on_renew = renew

    def run(request: object, lost: threading.Event) -> object:
        assert renewals.wait(0.8)
        assert not lost.is_set()
        return {}

    handler.on_run = run
    worker = loop(backend, handler)
    worker.lease = 1
    worker.renew_every = 0.1
    worker.step()
    assert count >= 2
    assert len(backend.consumed) == 1


@pytest.mark.parametrize("error", [NotFound("reowned"), Forbidden("not holder")])
def test_fencing_marks_handler_lost_without_completion(error: Exception) -> None:
    backend = Backend()
    backend.renew_errors = [error]
    handler = CustomHandler()

    def run(request: object, lost: threading.Event) -> object:
        assert lost.wait(1)
        return {}

    handler.on_run = run
    worker = loop(backend, handler)
    worker.renew_every = 0.02
    worker.step()
    assert not backend.consumed


def test_blocked_renewal_cannot_extend_deadline_or_release_capacity_early() -> None:
    backend = Backend()
    handler = CustomHandler()
    renewal_started = threading.Event()
    release_renewal = threading.Event()
    lost_seen = threading.Event()
    finished = threading.Event()

    def renew() -> None:
        renewal_started.set()
        assert release_renewal.wait(3)

    backend.on_renew = renew

    def run(request: object, lost: threading.Event) -> object:
        assert renewal_started.wait(1)
        assert lost.wait(1.5)
        lost_seen.set()
        return {}

    handler.on_run = run
    worker = loop(backend, handler)
    worker.lease = 1
    worker.renew_every = 0.02

    def execute() -> None:
        try:
            worker.step()
        finally:
            finished.set()

    thread = threading.Thread(target=execute)
    started = time.monotonic()
    thread.start()
    try:
        assert lost_seen.wait(1.6)
        assert time.monotonic() - started < 1.6
        assert not finished.is_set()
    finally:
        release_renewal.set()
        thread.join(3)
    assert finished.is_set()
    assert not backend.consumed
    assert not any(t.name.startswith("tasklease-") for t in threading.enumerate())


def test_own_response_after_deadline_does_not_execute() -> None:
    backend = Backend()
    backend.on_own = lambda: time.sleep(1.05)
    handler = CustomHandler()
    handler.on_validate = lambda task: pytest.fail("late Own must not execute")
    worker = loop(backend, handler, wait_seconds=0)
    worker.lease = 1
    worker.step()
    assert not backend.consumed


def test_stalled_consume_retries_only_until_deadline() -> None:
    backend = Backend()

    def consume(request: dict[str, object]) -> None:
        raise TransportError("outage")

    backend.on_consume = consume
    worker = loop(backend)
    worker.lease = 1
    worker.renew_every = 0.02
    started = time.monotonic()
    worker.step()
    assert 0.8 <= time.monotonic() - started < 2
    assert 2 <= len(backend.consumed) <= 12
    assert not any(op == "renew" for op, _ in backend.calls)


def test_graceful_stop_finishes_active_task_then_exits() -> None:
    backend = Backend()
    stop = threading.Event()
    handler = CustomHandler()

    def run(request: object, lost: threading.Event) -> object:
        stop.set()
        assert not lost.is_set()
        return {}

    handler.on_run = run
    WorkerLoop(
        backend.client(), DOMAIN, TOPIC, UnlimitedCapacity(), handler, stop=stop
    ).run()
    assert len(backend.consumed) == 1
    assert len([op for op, _ in backend.calls if op == "own"]) == 1


def test_idle_stop_wakes_poll_sleep_promptly() -> None:
    backend = Backend()
    backend.empty = True
    stop = threading.Event()
    backend.on_own = stop.set
    started = time.monotonic()
    WorkerLoop(
        backend.client(), DOMAIN, TOPIC, UnlimitedCapacity(), EchoHandler(), stop=stop
    ).run()
    assert time.monotonic() - started < 0.5


def test_handler_can_intentionally_fan_out() -> None:
    backend = Backend()
    handler = CustomHandler()
    handler.on_reply = lambda task, outcome: [
        {"topic": "next/work", "data": {}},
        {"topic": REPLY, "data": {}},
    ]
    loop(backend, handler).step()
    assert len(new_tasks(backend.consumed[0])) == 2


def test_own_long_polls_by_default_with_a_read_grace() -> None:
    backend = Backend()
    backend.empty = True
    worker = loop(backend)
    assert worker.wait_seconds == 30
    worker.step()
    own = next(body for op, body in backend.calls if op == "own")
    assert own == {
        "domain": DOMAIN,
        "topic": TOPIC,
        "hold_seconds": 600,
        "wait_seconds": 30,
    }
    # The read phase covers the server wait and a response margin. Connect
    # keeps the client's configured cap (30s), not the lease plus wait.
    assert backend.timeouts[0]["read"] == 45.0
    assert backend.timeouts[0]["connect"] == 30.0


def test_own_omits_wait_when_disabled_for_older_services() -> None:
    backend = Backend()
    backend.empty = True
    loop(backend, wait_seconds=0).step()
    own = next(body for op, body in backend.calls if op == "own")
    assert "wait_seconds" not in own


def test_wait_seconds_is_validated() -> None:
    backend = Backend()
    for value in (-1, 31, 1.5, True):
        with pytest.raises(Invalid):
            WorkerLoop(
                backend.client(),
                DOMAIN,
                TOPIC,
                UnlimitedCapacity(),
                EchoHandler(),
                wait_seconds=value,  # type: ignore[arg-type]
            )


def test_full_server_wait_continues_without_an_extra_idle_sleep() -> None:
    backend = Backend()
    backend.empty = True
    stop = threading.Event()
    started: list[float] = []

    def on_own() -> None:
        started.append(time.monotonic())
        if len(started) == 1:
            time.sleep(0.3)
        else:
            stop.set()

    backend.on_own = on_own
    loop = WorkerLoop(
        backend.client(),
        DOMAIN,
        TOPIC,
        UnlimitedCapacity(),
        EchoHandler(),
        wait_seconds=0,
        poll_interval=10,
        stop=stop,
    )
    # With long polling disabled, an immediate empty keeps the poll interval.
    assert loop.step() == "empty"
    assert loop.wait_seconds == 0


def test_immediate_empty_keeps_the_poll_interval() -> None:
    backend = Backend()
    backend.empty = True
    stop = threading.Event()
    slept: list[float] = []
    original = stop.wait

    def wait(timeout: float | None = None) -> bool:
        slept.append(timeout or 0)
        stop.set()
        return original(0)

    stop.wait = wait  # type: ignore[method-assign]
    WorkerLoop(
        backend.client(),
        DOMAIN,
        TOPIC,
        UnlimitedCapacity(),
        EchoHandler(),
        wait_seconds=30,
        poll_interval=4,
        stop=stop,
    ).run()
    # A server that answers empty at once did not provide the idle delay.
    assert len(slept) == 1 and 3.2 <= slept[0] <= 4.8


def test_completed_wait_does_not_sleep_again(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Backend()
    backend.empty = True
    stop = threading.Event()
    slept: list[float] = []
    original = stop.wait
    now = [100.0]

    def wait(timeout: float | None = None) -> bool:
        slept.append(timeout or 0)
        return original(0)

    calls = 0

    def on_own() -> None:
        nonlocal calls
        calls += 1
        now[0] += 29.5
        if calls == 2:
            stop.set()

    monkeypatch.setattr("tasklease.worker.time.monotonic", lambda: now[0])
    backend.on_own = on_own
    stop.wait = wait  # type: ignore[method-assign]
    WorkerLoop(
        backend.client(),
        DOMAIN,
        TOPIC,
        UnlimitedCapacity(),
        EchoHandler(),
        wait_seconds=30,
        poll_interval=4,
        stop=stop,
    ).run()
    # The server's own wait was the idle delay, so no extra sleep is needed.
    assert calls == 2
    assert not slept


def test_long_poll_task_gets_a_full_local_lease() -> None:
    """The wait is not lease time: a task claimed at the end is not stale."""
    backend = Backend()
    backend.on_own = lambda: time.sleep(0.2)
    worker = loop(backend, wait_seconds=30)
    worker.lease = 1
    worker.step()
    assert len(backend.consumed) == 1
