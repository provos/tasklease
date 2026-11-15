"""Requester crash recovery, immutable publication and local retention."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Literal, cast

import httpx
import pytest

from tasklease import fsio
from tasklease import store as stores
from tasklease.client import Client
from tasklease.errors import (
    Invalid,
    NotFound,
    StoreError,
    SubmissionError,
    TransportError,
)
from tasklease.generated import protocol as p
from tasklease.store import DrainResult, RequestRecord, Store, hash_id
from tasklease.testing import FakeBoard

DOMAIN = "example/project"
TOPIC = "work/linux"
REPLY = "replied/agent-1"
TASK = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TASK2 = "t_01ARZ3NDEKTSV4RRFFQ69G5FAW"
TASK3 = "t_01ARZ3NDEKTSV4RRFFQ69G5FAX"


def task(
    data: dict[str, object], task_id: str = TASK, *, origin_id: str = TASK
) -> dict[str, object]:
    return {
        "id": task_id,
        "origin_id": origin_id,
        "domain": DOMAIN,
        "topic": REPLY,
        "creator": "worker-1",
        "created_at": 1000,
        "lease_until": 61000,
        "owned_count": 1,
        "owner": {"identity": "requester", "worker": "cli"},
        "owned_at": 1000,
        "data": data,
    }


class Backend:
    def __init__(self) -> None:
        self.creates: list[dict[str, object]] = []
        self.owns: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self.cancels: list[dict[str, object]] = []
        self.created_ids: list[str] = []
        self.replies: list[dict[str, object]] = []
        self.on_create: Callable[[], None] = lambda: None
        self.on_own: Callable[[], None] = lambda: None
        self.on_delete: Callable[[], None] = lambda: None
        self.on_cancel: Callable[[], None] = lambda: None
        self.cancel_not_found = False
        self.delete_not_found = False
        self.wait_refused = False
        self.timeouts: list[object] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        operation = request.url.path.rsplit("/", 1)[-1]
        self.timeouts.append(request.extensions.get("timeout"))
        if operation == "create":
            self.creates.append(body)
            task_id = (TASK, TASK2, TASK3)[len(self.creates) - 1]
            self.created_ids.append(task_id)
            self.on_create()
            return httpx.Response(200, json={"ok": True, "id": task_id})
        if operation == "own":
            self.owns.append(body)
            self.on_own()
            if self.replies:
                return httpx.Response(
                    200,
                    content=json.dumps(
                        {"ok": True, "task": self.replies.pop(0)}
                    ).encode(),
                )
            error: dict[str, object] = {"code": "empty", "message": "empty"}
            if self.wait_refused:
                error["wait_refused"] = True
            return httpx.Response(404, json={"ok": False, "error": error})
        if operation == "cancel":
            self.cancels.append(body)
            self.on_cancel()
            if self.cancel_not_found:
                return httpx.Response(
                    404,
                    json={
                        "ok": False,
                        "error": {"code": "not_found", "message": "gone"},
                    },
                )
            return httpx.Response(200, json={"ok": True})
        assert operation == "delete"
        self.deletes.append(body)
        self.on_delete()
        if self.delete_not_found:
            return httpx.Response(
                404,
                json={"ok": False, "error": {"code": "not_found", "message": "gone"}},
            )
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def backend() -> Backend:
    return Backend()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(
        "https://tasks.example",
        "secret",
        transport=httpx.MockTransport(backend.handler),
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store("agent-1", tmp_path)


def submit(store: Store, client: Client) -> RequestRecord:
    return store.submit(client, DOMAIN, TOPIC, REPLY, {"input": "hello"})


def lost_response() -> None:
    raise httpx.ReadError("lost response")


def test_create_first_submission_saves_returned_origin_and_detached_payload(
    store: Store, client: Client, backend: Backend
) -> None:
    data: dict[str, object] = {"input": {"items": [1]}, "request_id": "opaque"}

    def inspect() -> None:
        assert store.requests() == []
        data["input"] = "mutated"

    backend.on_create = inspect
    record = store.submit(client, DOMAIN, TOPIC, REPLY, data)
    assert record.origin_id == TASK and record.state == "pending"
    assert record.data == {
        "input": {"items": [1]},
        "request_id": "opaque",
        "reply_topic": REPLY,
    }
    assert backend.creates[0]["data"] == record.data
    assert record.as_dict() == record.to_dict()
    assert "task_id" not in record.as_dict()
    assert (store.request_directory / f"{TASK}.json").is_file()
    assert store.request_directory.name == hash_id(store.agent_id)
    assert not list(store.request_directory.glob("*.task.json"))


def test_uncertain_create_leaves_no_record_and_retry_uses_a_new_origin(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.on_create = lost_response
    with pytest.raises(TransportError):
        submit(store, client)
    assert store.requests() == []
    backend.on_create = lambda: None
    retried = submit(store, client)
    assert retried.origin_id == TASK2
    assert backend.created_ids == [TASK, TASK2]
    backend.replies = [
        task({"result": "first"}, TASK3, origin_id=TASK),
        task({"result": "retried"}, origin_id=TASK2),
    ]
    reply = store.wait(client, retried.origin_id, 1, wait_seconds=0)
    assert reply is not None and reply.data["result"] == "retried"
    assert reply.matched
    first = store.reply(TASK)
    assert first is not None and not first.matched


def test_failed_local_publication_exposes_the_created_origin(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsio.publish_new

    def publish(path: Path, data: bytes) -> Literal["created", "exists"]:
        if path == store._request_path(TASK):
            raise OSError("disk full with secret payload")
        return original(path, data)

    monkeypatch.setattr(fsio, "publish_new", publish)
    with pytest.raises(SubmissionError) as error:
        submit(store, client)
    assert error.value.origin_id == TASK
    assert TASK in str(error.value) and "secret" not in str(error.value)
    assert len(backend.creates) == 1
    assert store.requests() == []


def test_a_reply_can_arrive_before_local_submission_is_saved(
    store: Store, client: Client, backend: Backend
) -> None:
    def deliver() -> None:
        backend.replies = [task({"result": "fast"})]
        store.drain(client, DOMAIN, REPLY)
        reply = store.reply(TASK)
        assert reply is not None and not reply.matched

    backend.on_create = deliver
    record = submit(store, client)
    assert record.state == "answered"
    reply = store.wait(client, record.origin_id, 0)
    assert reply is not None and reply.matched


def test_concurrent_submissions_are_distinct_even_with_identical_payloads(
    tmp_path: Path,
) -> None:
    board = FakeBoard(DOMAIN)
    board.add_identity("requester", "request-token", create=[TOPIC])
    stores = [Store("agent-1", tmp_path), Store("agent-1", tmp_path)]
    barrier = Barrier(2)

    def post(store: Store) -> RequestRecord:
        with board.client("request-token") as api:
            barrier.wait()
            return submit(store, api)

    with ThreadPoolExecutor(2) as pool:
        records = list(pool.map(post, stores))
    assert len({record.origin_id for record in records}) == 2
    assert len(board.tasks) == 2
    assert len(stores[0].requests()) == 2


@pytest.mark.parametrize("origin", ["", "../x", "request-1", "t_bad", "\ud800", None])
def test_lookup_rejects_invalid_origins(store: Store, origin: object) -> None:
    for read in (store.reply, store.request):
        with pytest.raises(StoreError):
            read(cast(str, origin))


@pytest.mark.parametrize("data", [{"reply_topic": "other"}, {"value": float("nan")}])
def test_invalid_payload_never_creates(
    store: Store, client: Client, backend: Backend, data: dict[str, object]
) -> None:
    with pytest.raises(StoreError):
        store.submit(client, DOMAIN, TOPIC, REPLY, data)
    assert backend.creates == []


def test_payload_correlation_cannot_take_another_origins_reply_slot(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [
        task(
            {"request_id": TASK, "origin_id": TASK, "result": "forged"},
            TASK2,
            origin_id=TASK2,
        ),
        task({"result": "genuine"}, origin_id=TASK),
        task({"result": "duplicate"}, TASK3, origin_id=TASK),
    ]
    store.drain(client, DOMAIN, REPLY)
    reply = store.wait(client, TASK, 0)
    assert reply is not None and reply.data["result"] == "genuine" and reply.matched
    forged = store.reply(TASK2)
    assert forged is not None and not forged.matched
    assert len(store.inbox()) == 2
    assert len(backend.deletes) == 3


@pytest.mark.parametrize("outcome", ["success", "not_found", "lost"])
def test_cancel_uses_origin_and_keeps_local_state_until_confirmed(
    store: Store, client: Client, backend: Backend, outcome: str
) -> None:
    submit(store, client)
    backend.cancel_not_found = outcome == "not_found"
    if outcome == "lost":
        backend.on_cancel = lost_response
        with pytest.raises(TransportError):
            store.cancel(client, TASK)
    else:
        assert store.cancel(client, TASK) == (
            "cancelled" if outcome == "success" else "not_found"
        )
    assert backend.cancels == [{"domain": DOMAIN, "topic": TOPIC, "origin_id": TASK}]
    assert backend.deletes == []
    assert (store.request(TASK) is None) == (outcome == "success")


def test_cancel_initial_task_after_own_rotates_its_id(tmp_path: Path) -> None:
    board = FakeBoard(DOMAIN)
    board.add_identity("requester", "request-token", create=[TOPIC])
    board.add_identity("worker", "worker-token", own=[TOPIC])
    store = Store("agent-1", tmp_path)
    with board.client("request-token") as api, board.client("worker-token") as worker:
        record = submit(store, api)
        held = worker.own({"domain": DOMAIN, "topic": TOPIC, "hold_seconds": 60})[
            "task"
        ]
        assert held["id"] != record.origin_id
        assert store.cancel(api, record.origin_id) == "cancelled"
        with pytest.raises(NotFound):
            worker.renew(
                {"domain": DOMAIN, "topic": TOPIC, "id": held["id"], "hold_seconds": 60}
            )


def test_prune_keeps_request_while_its_later_reply_is_retained(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    submit(store, client)
    now += 6 * 86400
    backend.replies = [task({"result": "done"})]
    store.drain(client, DOMAIN, REPLY)
    now += 2 * 86400
    store.prune()
    restarted = Store(store.agent_id, store.root, initialize=False)
    record = restarted.request(TASK)
    assert record is not None and record.state == "answered"
    reply = restarted.wait(client, TASK, 0)
    assert reply is not None and reply.matched
    now += 6 * 86400
    restarted.prune()
    assert restarted.requests() == [] and restarted.inbox() == []


def test_pruned_request_cannot_be_answered_by_a_fresh_forged_origin(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    submit(store, client)
    temporary = store.request_directory / ".abandoned.random.tmp"
    temporary.write_bytes(b"partial")
    os.utime(temporary, (now, now))
    now += 8 * 86400
    store.prune()
    assert store.request(TASK) is None and not temporary.exists()
    backend.replies = [
        task({"request_id": TASK, "origin_id": TASK}, TASK2, origin_id=TASK2)
    ]
    store.drain(client, DOMAIN, REPLY)
    assert store.reply(TASK) is None
    with pytest.raises(StoreError, match="No saved request"):
        store.wait(client, TASK, 0)


@pytest.mark.parametrize("folder", ["requests", "replies"])
@pytest.mark.parametrize("name", ["a" * 32 + ".json", TASK + ".task.json"])
def test_legacy_state_files_fail_closed(store: Store, folder: str, name: str) -> None:
    directory = (
        store.request_directory if folder == "requests" else store.reply_directory
    )
    (directory / name).write_text("{}")
    with pytest.raises(StoreError, match="storage name"):
        store.requests() if folder == "requests" else store.inbox()


def test_opaque_surrogate_data_is_saved_before_delete(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [task({"request_id": "\ud800", "value": "\ud800"})]

    def inspect() -> None:
        record = store.reply(TASK)
        assert record is not None and not record.matched
        assert record.data == {"request_id": "\ud800", "value": "\ud800"}

    backend.on_delete = inspect
    assert store.drain(client, DOMAIN, REPLY) == 1
    assert (store.reply_directory / f"{TASK}.json").read_bytes().isascii()


def test_poll_on_a_fake_board_receives_a_new_server_origin(tmp_path: Path) -> None:
    board = FakeBoard(DOMAIN)
    board.add_identity("listener", "listen-token", own=[REPLY])
    board.add_identity("sender", "send-token", create=[REPLY])
    store = Store("agent-1", tmp_path)

    def send() -> str:
        time.sleep(0.2)
        with board.client("send-token") as client:
            return client.create(
                {"domain": DOMAIN, "topic": REPLY, "data": {"text": "hello"}}
            )["id"]

    with ThreadPoolExecutor(1) as pool, board.client("listen-token") as client:
        sent = pool.submit(send)
        assert store.poll(client, DOMAIN, REPLY) == DrainResult(1, True)
        origin = sent.result()
    reply = store.reply(origin)
    assert reply is not None and reply.data == {"text": "hello"}


@pytest.mark.parametrize("agent_id", ["Foo", "CON", "agent/a", "", "a" * 129])
def test_agent_id_must_be_topic_segment(agent_id: str, tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        Store(agent_id, tmp_path)


def test_saved_reply_precedes_delete_and_wait_reads_local_first(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [task({"result": 42}, origin_id=TASK)]

    def inspect() -> None:
        reply = store.reply(TASK)
        assert reply is not None and reply.data["result"] == 42

    backend.on_delete = inspect
    assert store.drain(client, DOMAIN, REPLY) == 1
    reply = store.wait(client, TASK, 0)
    assert reply is not None and reply.creator == "worker-1"
    assert len(backend.owns) == 2
    assert backend.owns[0]["hold_seconds"] == 60
    assert backend.deletes[0]["id"] == TASK
    assert reply.to_dict() == reply.as_dict()


def test_lost_delete_response_or_crash_after_save_recovers_first_reply(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [task({"result": "first"}, origin_id=TASK)]
    backend.on_delete = lost_response
    with pytest.raises(TransportError):
        store.drain(client, DOMAIN, REPLY)
    assert store.wait(client, TASK, 0) is not None
    backend.on_delete = lambda: None
    backend.replies = [task({"result": "duplicate"}, TASK2, origin_id=TASK)]
    store.drain(client, DOMAIN, REPLY)
    reply = store.reply(TASK)
    assert reply is not None and reply.data["result"] == "first"
    assert len(store.inbox()) == 1
    record = store.request(TASK)
    assert record is not None and record.state == "answered"
    assert len(backend.creates) == 1
    assert len(backend.deletes) == 2


@pytest.mark.parametrize(
    "failure", ["publication", "corrupt_existing", "unreadable_existing"]
)
def test_local_reply_failure_never_deletes_server_copy(
    failure: str,
    store: Store,
    client: Client,
    backend: Backend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = store.reply_directory / f"{TASK}.json"
    # Claim automatic prune before injecting corruption; exercise the drain itself.
    store.drain(client, DOMAIN, REPLY)
    if failure == "corrupt_existing":
        path.write_bytes(b"not-json")
    elif failure == "unreadable_existing":
        backend.replies = [task({}, origin_id=TASK)]
        store.drain(client, DOMAIN, REPLY)
        backend.deletes.clear()
        original_read = fsio.read_json

        def read(target: Path) -> object:
            if target == path:
                raise PermissionError("secret")
            return original_read(target)

        monkeypatch.setattr(fsio, "read_json", read)
    else:
        original_publish = fsio.publish_new

        def publish(target: Path, data: bytes) -> Literal["created", "exists"]:
            if target == path:
                raise OSError("secret")
            return original_publish(target, data)

        monkeypatch.setattr(fsio, "publish_new", publish)
    backend.replies = [task({}, origin_id=TASK)]
    with pytest.raises(StoreError):
        store.drain(client, DOMAIN, REPLY)
    assert backend.deletes == []


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_saved_reply_never_deletes_valid_duplicate(
    number: str, store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [
        task(
            {"result": {"nested": ["replace-me"]}},
            origin_id=TASK,
        )
    ]
    store.drain(client, DOMAIN, REPLY)
    backend.deletes.clear()
    path = store.reply_directory / f"{TASK}.json"
    contents = path.read_text(encoding="utf-8")
    assert '"replace-me"' in contents
    corrupted = contents.replace('"replace-me"', number)
    path.write_text(corrupted, encoding="utf-8")
    backend.replies = [task({"result": "valid"}, TASK2, origin_id=TASK)]
    with pytest.raises(StoreError):
        store.drain(client, DOMAIN, REPLY)
    assert backend.deletes == []
    assert path.read_text(encoding="utf-8") == corrupted


def test_concurrent_duplicate_drain_keeps_one_whole_first_reply(tmp_path: Path) -> None:
    barrier = Barrier(2)
    first, second = Store("agent-1", tmp_path), Store("agent-1", tmp_path)
    backends = [Backend(), Backend()]
    clients: list[Client] = []
    for index, backend in enumerate(backends):
        backend.replies = [
            task(
                {"winner": index},
                (TASK, TASK2)[index],
                origin_id=TASK,
            )
        ]
        backend.on_own = lambda: None
        clients.append(
            Client(
                "https://tasks.example",
                "secret",
                transport=httpx.MockTransport(backend.handler),
            )
        )

    def drain(store: Store, client: Client) -> int:
        barrier.wait()
        return store.drain(client, DOMAIN, REPLY)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(drain, first, clients[0]),
            pool.submit(drain, second, clients[1]),
        ]
        assert [future.result() for future in futures] == [1, 1]
    assert len(first.inbox()) == 1
    assert first.inbox()[0].data["winner"] in {0, 1}
    assert all(len(backend.deletes) == 1 for backend in backends)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


def test_wait_timeout_preserves_intent_and_bounds_network_timeout(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    assert store.wait(client, TASK, 2.5, 1, max_poll_interval=1) is None
    assert clock.sleeps == [1, 1, 0.5]
    assert len(backend.owns) == 3
    assert backend.timeouts[1:] == [
        {"connect": remaining, "read": remaining, "write": remaining, "pool": remaining}
        for remaining in (2.5, 1.5, 0.5)
    ]
    assert store.request(TASK) is not None
    assert len(backend.creates) == 1


def test_wait_backs_off_to_the_maximum_poll_interval(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    assert store.wait(client, TASK, 60, 1, max_poll_interval=4) is None
    assert clock.sleeps[:6] == [1, 1.5, 2.25, 3.375, 4, 4]
    # A one-minute wait makes 17 requests instead of about 60.
    assert len(backend.owns) == len(clock.sleeps) == 17


def test_wait_checks_deadline_even_with_endless_incoming_replies(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)

    def refill() -> None:
        clock.now += 0.5
        backend.replies.append(task({}, origin_id=TASK2))

    backend.on_own = refill
    assert store.wait(client, TASK, 2) is None
    assert len(backend.owns) == 4
    # At the deadline the last reply is safely local, without another network call.
    assert len(backend.deletes) == 3
    assert store.reply(TASK2) is not None


def test_automatic_prune_claim_is_once_daily_across_processes(
    tmp_path: Path, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    first, second = Store("agent-1", tmp_path), Store("agent-1", tmp_path)
    calls: list[str] = []

    def prune(self: Store) -> int:
        calls.append(self.agent_id)
        return 0

    monkeypatch.setattr(Store, "prune", prune)
    first.drain(client, DOMAIN, REPLY)
    second.drain(client, DOMAIN, REPLY)
    assert calls == ["agent-1"]
    now += 86400
    second.drain(client, DOMAIN, REPLY)
    assert calls == ["agent-1", "agent-1"]


def test_agents_and_prune_all_work_without_config_or_client(tmp_path: Path) -> None:
    for agent in ("z-agent", "a-agent", "con"):
        Store(agent, tmp_path)
    assert Store.agents(tmp_path) == ["a-agent", "con", "z-agent"]
    assert Store.prune_all(tmp_path) == 0


def test_wait_returns_desired_reply_without_draining_unrelated_backlog(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [
        task({}, origin_id=TASK),
        task({}, TASK2, origin_id=TASK2),
    ]
    assert store.wait(client, TASK, 100) is not None
    assert len(backend.owns) == 1 and len(backend.deletes) == 1
    assert len(backend.replies) == 1


def test_network_timeout_override_is_per_call_and_not_client_global(
    client: Client, backend: Backend
) -> None:
    backend.replies = [task({}), task({}, TASK2)]
    client.own({"domain": DOMAIN, "topic": REPLY, "hold_seconds": 60}, timeout=1.25)
    client.delete({"domain": DOMAIN, "topic": REPLY, "id": TASK}, timeout=0.5)
    client.own({"domain": DOMAIN, "topic": REPLY, "hold_seconds": 60})
    assert backend.timeouts == [
        {"connect": value, "read": value, "write": value, "pool": value}
        for value in (1.25, 0.5, 30.0)
    ]
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(Invalid):
            client.own(
                {"domain": DOMAIN, "topic": REPLY, "hold_seconds": 60}, timeout=timeout
            )
    assert len(backend.owns) == 2


def test_network_timeout_at_wait_deadline_is_a_wait_timeout(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    def timeout() -> None:
        clock.now += 2
        raise httpx.ReadTimeout("deadline")

    backend.on_own = timeout
    assert store.wait(client, TASK, 2) is None
    assert store.request(TASK) is not None


def test_wait_retries_a_transport_failure_until_the_reply(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that drops a held poll must not end a long wait."""
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    failures = [httpx.ReadError("deploy"), httpx.ConnectError("restarting")]

    def flaky() -> None:
        if failures:
            raise failures.pop(0)
        backend.replies.append(task({"result": "ok"}, origin_id=TASK))

    backend.on_own = flaky
    reply = store.wait(client, TASK, 6 * 3600)
    assert reply is not None and reply.data["result"] == "ok"
    # Each failure backs off like an immediate empty.
    assert clock.sleeps == [1, 1.5]


def test_wait_reports_transport_failures_that_persist(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong URL or long outage is reported, not ridden out for hours."""
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    backend.on_own = lost_response
    with pytest.raises(TransportError):
        store.wait(client, TASK, 6 * 3600)
    assert 300 <= clock.now - 100 < 400


def test_wait_transport_failures_end_at_the_timeout(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    backend.on_own = lost_response
    assert store.wait(client, TASK, 100) is None
    assert clock.now >= 200
    assert store.request(TASK) is not None


def test_wait_returns_a_saved_reply_after_persistent_failures(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply saved before a failing Delete is returned, not lost to patience."""
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)

    # The outage ends just inside the five-minute patience; the Own that
    # saves the reply is slow, and its Delete fails after the patience.
    def recovers() -> None:
        if clock.now < 370:
            raise httpx.ConnectError("outage")
        clock.now += 30
        backend.replies.append(task({"result": "ok"}, origin_id=TASK))

    backend.on_own = recovers
    backend.on_delete = lost_response
    reply = store.wait(client, TASK, 6 * 3600)
    assert reply is not None and reply.data["result"] == "ok"


def test_wait_cut_short_by_its_deadline_returns_none(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A last attempt that the wait's deadline cut off is a timeout."""
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    failed = False

    def late_failures() -> None:
        nonlocal failed
        if clock.now < 390:
            return
        if not failed:
            failed = True
            raise httpx.ConnectError("deploy")
        clock.now = 700
        raise httpx.ReadTimeout("deadline")

    backend.on_own = late_failures
    assert store.wait(client, TASK, 600) is None


def test_wait_patience_counts_the_first_failed_attempt(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)

    def blackholed() -> None:
        clock.now += 40
        raise httpx.ConnectTimeout("blackholed")

    backend.on_own = blackholed
    with pytest.raises(TransportError):
        store.wait(client, TASK, 310)


def test_wait_backoff_starts_over_after_a_held_poll(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    rounds = iter(["fail", "fail", "fail", "held", "immediate"])

    def scripted() -> None:
        step = next(rounds, "reply")
        if step == "fail":
            raise httpx.ReadError("deploy")
        if step == "held":
            clock.now += 30
        elif step == "reply":
            backend.replies.append(task({"result": "ok"}, origin_id=TASK))

    backend.on_own = scripted
    assert store.wait(client, TASK, 3600, wait_seconds=30) is not None
    assert clock.sleeps == [1, 1.5, 2.25, 1]


def test_wait_delivers_durable_reply_when_delete_response_is_lost(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [task({"result": "safe"}, origin_id=TASK)]
    backend.on_delete = lost_response
    reply = store.wait(client, TASK, 100)
    assert reply is not None and reply.data["result"] == "safe"
    assert len(backend.deletes) == 1


@pytest.mark.parametrize(
    "configured,expected",
    [
        (2.0, {"connect": 2.0, "read": 2.0, "write": 2.0, "pool": 2.0}),
        (
            httpx.Timeout(connect=0.25, read=2.0, write=None, pool=3.0),
            {"connect": 0.25, "read": 2.0, "write": 10.0, "pool": 3.0},
        ),
        (None, {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}),
    ],
)
def test_per_call_timeout_caps_each_configured_phase(
    configured: float | httpx.Timeout | None,
    expected: dict[str, float],
    backend: Backend,
) -> None:
    backend.replies = [task({}), task({}, TASK2)]
    with Client(
        "https://tasks.example",
        "secret",
        timeout=configured,
        transport=httpx.MockTransport(backend.handler),
    ) as client:
        own_request: p.OwnRequest = {
            "domain": DOMAIN,
            "topic": REPLY,
            "hold_seconds": 60,
        }
        delete_request: p.DeleteRequest = {"domain": DOMAIN, "topic": REPLY, "id": TASK}
        client.own(own_request, timeout=10.0)
        client.delete(delete_request, timeout=10.0)
        client.own(own_request, timeout=0.1)
        client.delete(delete_request, timeout=0.1)
        client.delete(delete_request)
    assert backend.timeouts[:2] == [expected, expected]
    assert (
        backend.timeouts[2:4]
        == [{"connect": 0.1, "read": 0.1, "write": 0.1, "pool": 0.1}] * 2
    )
    original = (
        httpx.Timeout(configured)
        if not isinstance(configured, httpx.Timeout)
        else configured
    )
    assert backend.timeouts[4] == original.as_dict()


def test_locating_existing_store_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Store("agent", tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Read-only initialization must not publish")

    monkeypatch.setattr(stores, "_publish", forbidden)
    existing = Store("agent", tmp_path, initialize=False)
    assert existing.requests() == [] and existing.inbox() == []


@pytest.mark.parametrize("missing", ["requests", "replies"])
def test_locating_partial_store_does_not_create_missing_agent_folder(
    tmp_path: Path, missing: str
) -> None:
    store = Store("agent", tmp_path)
    directory = tmp_path / missing / hash_id("agent")
    (directory / "agent.json").unlink()
    directory.rmdir()
    existing = Store("agent", tmp_path, initialize=False)
    assert existing.request_directory == store.request_directory
    assert existing.requests() == [] and existing.inbox() == []
    assert not directory.exists()


def test_locating_absent_store_fails_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(StoreError, match="No existing metadata"):
        Store("agent", root, initialize=False)
    assert not root.exists()


def test_locating_corrupt_metadata_refuses_it_without_replacement(
    tmp_path: Path,
) -> None:
    store = Store("agent", tmp_path)
    path = store.request_directory / "agent.json"
    path.write_text('{"agent_id":"different"}')
    with pytest.raises(StoreError, match="does not match"):
        Store("agent", tmp_path, initialize=False)
    assert path.read_text() == '{"agent_id":"different"}'


def test_wait_long_polls_without_backoff(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)

    # Each server response takes the full wait and advances the clock, so no
    # client-side sleep is needed between polls.
    def full_wait() -> None:
        clock.now += 30

    backend.on_own = full_wait
    assert store.wait(client, TASK, 70, wait_seconds=30) is None
    assert clock.sleeps == []
    assert len(backend.owns) == 3
    # Every round waits on the server; the result records the initial
    # submission as own[0], so all rounds are long polls capped to the
    # remaining overall timeout, less the answer margin.
    assert [body["wait_seconds"] for body in backend.owns[-3:]] == [30, 30, 5]


def test_wait_caps_a_long_poll_to_the_remaining_timeout(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    assert store.wait(client, TASK, 12, wait_seconds=30) is None
    # The wait leaves its answer five seconds inside the overall deadline.
    assert backend.owns[0]["wait_seconds"] == 7
    timeout = backend.timeouts[1]
    assert isinstance(timeout, dict) and timeout["read"] == 12


def test_wait_zero_restores_bounded_backoff(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    assert store.wait(client, TASK, 3, wait_seconds=0) is None
    assert clock.sleeps == [1, 1.5, 0.5]
    assert all("wait_seconds" not in body for body in backend.owns)


def test_wait_immediate_empty_still_backs_off(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    # A full waiter queue or older service answers at once; back off anyway.
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    assert store.wait(client, TASK, 3, wait_seconds=30) is None
    assert clock.sleeps and clock.sleeps[0] >= 1


def test_wait_refused_hint_backs_off_even_after_a_slow_answer(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)

    # Slow enough to pass for a held one-second wait, but the service says
    # it refused to hold the request.
    def slow_refusal() -> None:
        clock.now += 0.6

    backend.on_own = slow_refusal
    backend.wait_refused = True
    assert store.wait(client, TASK, 5, wait_seconds=1) is None
    assert clock.sleeps[:2] == [1, 1.5]


def test_wait_one_second_immediate_empty_backs_off(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    # An answer that did not hold the request must not re-poll without a
    # pause, even with the shortest wait.
    assert store.wait(client, TASK, 3, wait_seconds=1) is None
    assert clock.sleeps == [1, 1.5, 0.5]


def test_wait_rejects_invalid_wait_seconds(store: Store, client: Client) -> None:
    for value in (-1, 31, 1.5, True):
        with pytest.raises(StoreError):
            store.wait(client, TASK, 10, wait_seconds=value)  # type: ignore[arg-type]


def test_poll_holds_only_its_first_own_and_saves_the_batch(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [
        task({"text": "first"}, origin_id=TASK),
        task({"text": "second"}, TASK2, origin_id=TASK2),
    ]
    assert store.poll(client, DOMAIN, REPLY) == DrainResult(2, True)
    assert backend.owns[0]["wait_seconds"] == 30
    # A task already queued is never delayed behind a held poll.
    assert all("wait_seconds" not in body for body in backend.owns[1:])
    timeout = backend.timeouts[0]
    assert isinstance(timeout, dict) and timeout["read"] > 30
    assert len(backend.deletes) == 2
    first = store.reply(TASK)
    assert first is not None and first.data["text"] == "first"


def test_poll_after_a_held_empty_polls_again_at_once(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    def full_wait() -> None:
        clock.now += 30

    backend.on_own = full_wait
    assert store.poll(client, DOMAIN, REPLY) == DrainResult(0, True)


def test_poll_backs_off_after_an_immediate_or_refused_empty(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    # An empty answered at once was not held, whatever the service says.
    assert store.poll(client, DOMAIN, REPLY) == DrainResult(0, False)

    def full_wait() -> None:
        clock.now += 30

    backend.on_own = full_wait
    backend.wait_refused = True
    assert store.poll(client, DOMAIN, REPLY) == DrainResult(0, False)


def test_poll_zero_does_not_wait(
    store: Store, client: Client, backend: Backend
) -> None:
    assert store.poll(client, DOMAIN, REPLY, wait_seconds=0) == DrainResult(0, False)
    assert "wait_seconds" not in backend.owns[0]


@pytest.mark.parametrize("wait", [-1, 31, 1.5, True])
def test_poll_refuses_an_invalid_wait(
    store: Store, client: Client, backend: Backend, wait: object
) -> None:
    with pytest.raises(StoreError, match="wait_seconds"):
        store.poll(client, DOMAIN, REPLY, wait_seconds=cast(int, wait))
    assert backend.owns == []


def test_poll_on_a_fake_board_reports_a_refused_wait(tmp_path: Path) -> None:
    board = FakeBoard(DOMAIN, max_waiters=0)
    board.add_identity("listener", "listen-token", own=[REPLY])
    store = Store("agent-1", tmp_path)
    result = store.poll(board.client("listen-token"), DOMAIN, REPLY)
    assert result == DrainResult(0, False)
