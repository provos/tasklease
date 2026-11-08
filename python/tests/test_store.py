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
from tasklease.errors import Invalid, StoreError, TransportError
from tasklease.generated import protocol as p
from tasklease.store import Store, hash_id

DOMAIN = "example/project"
TOPIC = "work/linux"
REPLY = "replied/agent-1"
TASK = "t_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TASK2 = "t_01ARZ3NDEKTSV4RRFFQ69G5FAW"


def task(data: dict[str, object], task_id: str = TASK) -> dict[str, object]:
    return {
        "id": task_id,
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
        self.replies: list[dict[str, object]] = []
        self.on_create: Callable[[], None] = lambda: None
        self.on_own: Callable[[], None] = lambda: None
        self.on_delete: Callable[[], None] = lambda: None
        self.delete_not_found = False
        self.timeouts: list[object] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        operation = request.url.path.rsplit("/", 1)[-1]
        self.timeouts.append(request.extensions.get("timeout"))
        if operation == "create":
            self.creates.append(body)
            self.on_create()
            return httpx.Response(200, json={"ok": True, "id": TASK})
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
            return httpx.Response(
                404, json={"ok": False, "error": {"code": "empty", "message": "empty"}}
            )
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


def submit(store: Store, client: Client, request_id: str = "request-1") -> None:
    store.submit(client, DOMAIN, TOPIC, REPLY, {"input": "hello"}, request_id)


def lost_response() -> None:
    raise httpx.ReadError("lost response")


def test_intent_is_complete_and_durable_before_create(
    store: Store, client: Client, backend: Backend
) -> None:
    def inspect() -> None:
        saved = store.request("request-1")
        assert saved is not None and saved.state == "submission_unknown"
        assert saved.task_id is None
        assert saved.data == backend.creates[0]["data"]

    backend.on_create = inspect
    submit(store, client)
    record = store.request("request-1")
    assert record is not None and record.state == "pending" and record.task_id == TASK
    assert record.data == {
        "input": "hello",
        "request_id": "request-1",
        "reply_topic": REPLY,
    }
    assert record.as_dict() == record.to_dict()
    assert record.created_at > 0
    assert fsio.read_json(
        store.request_directory / f"{hash_id('request-1')}.task.json"
    ) == {"task_id": TASK}


def test_lost_create_response_retains_unknown_intent_and_explicit_resend(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.on_create = lost_response
    with pytest.raises(TransportError):
        submit(store, client)
    first = store.request("request-1")
    assert first is not None and first.state == "submission_unknown"
    backend.on_create = lambda: None
    # Ordinary invocation cannot turn an uncertain network result into a retry.
    submit(store, client)
    assert len(backend.creates) == 1
    retried = store.resend(client, "request-1")
    assert len(backend.creates) == 2
    assert backend.creates[0] == backend.creates[1]
    assert retried.created_at == first.created_at
    assert retried.state == "pending"
    with pytest.raises(StoreError, match="known pending"):
        store.resend(client, "request-1")


def test_sidecar_publication_failure_leaves_saved_unknown_request(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsio.publish_new

    def publish(path: Path, data: bytes) -> Literal["created", "exists"]:
        if path.name.endswith(".task.json"):
            raise OSError("disk full with secret payload")
        return original(path, data)

    monkeypatch.setattr(fsio, "publish_new", publish)
    with pytest.raises(StoreError, match="Cannot publish") as error:
        submit(store, client)
    assert "secret" not in str(error.value)
    record = store.request("request-1")
    assert record is not None and record.state == "submission_unknown"
    assert len(backend.creates) == 1


@pytest.mark.parametrize("change", ["data", "domain", "topic", "reply_topic"])
def test_reusing_id_for_different_request_never_posts(
    change: str, store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    arguments: dict[str, object] = {
        "domain": DOMAIN,
        "topic": TOPIC,
        "reply_topic": REPLY,
        "data": {"input": "hello"},
        "request_id": "request-1",
    }
    arguments[change] = {"other": True} if change == "data" else "other"
    with pytest.raises(StoreError):
        store.submit(
            client,
            cast(str, arguments["domain"]),
            cast(str, arguments["topic"]),
            cast(str, arguments["reply_topic"]),
            cast(dict[str, object], arguments["data"]),
            "request-1",
        )
    assert len(backend.creates) == 1


@pytest.mark.parametrize("request_id", ["Foo", "foo", "CON", "../x", "a/b", "é"])
def test_identifiers_are_only_hashed_filenames(
    request_id: str, store: Store, client: Client
) -> None:
    submit(store, client, request_id)
    assert (store.request_directory / f"{hash_id(request_id)}.json").is_file()
    assert store.request_directory.name == hash_id(store.agent_id)
    assert hash_id("Foo") != hash_id("foo")
    assert fsio.read_json(store.request_directory / "agent.json") == {
        "agent_id": "agent-1"
    }


@pytest.mark.parametrize("request_id", ["", "x" * 129, "\ud800"])
def test_invalid_request_identifiers_never_post(
    request_id: str, store: Store, client: Client, backend: Backend
) -> None:
    with pytest.raises(StoreError):
        submit(store, client, request_id)
    assert backend.creates == []


@pytest.mark.parametrize("agent_id", ["Foo", "CON", "agent/a", "", "a" * 129])
def test_agent_id_must_be_topic_segment(agent_id: str, tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        Store(agent_id, tmp_path)


def test_submit_refuses_conflicting_identifying_fields(
    store: Store, client: Client, backend: Backend
) -> None:
    for data in ({"request_id": "other"}, {"reply_topic": "other"}):
        with pytest.raises(StoreError):
            store.submit(client, DOMAIN, TOPIC, REPLY, data, "request-1")
    assert backend.creates == []


def test_saved_reply_precedes_delete_and_wait_reads_local_first(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [task({"request_id": "request-1", "result": 42})]

    def inspect() -> None:
        reply = store.reply("request-1")
        assert reply is not None and reply.data["result"] == 42

    backend.on_delete = inspect
    assert store.drain(client, DOMAIN, REPLY) == 1
    reply = store.wait(client, "request-1", 0)
    assert reply is not None and reply.creator == "worker-1"
    assert len(backend.owns) == 2
    assert backend.owns[0]["hold_seconds"] == 60
    assert backend.deletes[0]["id"] == TASK
    assert reply.to_dict() == reply.as_dict()


def test_lost_delete_response_or_crash_after_save_recovers_first_reply(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [task({"request_id": "request-1", "result": "first"})]
    backend.on_delete = lost_response
    with pytest.raises(TransportError):
        store.drain(client, DOMAIN, REPLY)
    assert store.wait(client, "request-1", 0) is not None
    backend.on_delete = lambda: None
    backend.replies = [task({"request_id": "request-1", "result": "duplicate"}, TASK2)]
    store.drain(client, DOMAIN, REPLY)
    reply = store.reply("request-1")
    assert reply is not None and reply.data["result"] == "first"
    assert len(store.inbox()) == 1
    record = store.request("request-1")
    assert record is not None and record.state == "answered"
    assert store.resend(client, "request-1").state == "answered"
    assert len(backend.creates) == 1
    assert len(backend.deletes) == 2


@pytest.mark.parametrize("request_id", [None, 17, "", "x" * 129, ["id"]])
def test_malformed_correlation_is_durably_unmatched(
    request_id: object, store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [task({"request_id": request_id, "result": "diagnostic"})]
    store.drain(client, DOMAIN, REPLY)
    assert (store.unmatched_directory / f"{TASK}.json").is_file()
    assert store.inbox()[0].request_id is None
    assert len(backend.deletes) == 1


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
    path = store.reply_directory / f"{hash_id('request-1')}.json"
    # Claim automatic prune before injecting corruption; exercise the drain itself.
    store.drain(client, DOMAIN, REPLY)
    if failure == "corrupt_existing":
        path.write_bytes(b"not-json")
    elif failure == "unreadable_existing":
        backend.replies = [task({"request_id": "request-1"})]
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
    backend.replies = [task({"request_id": "request-1"})]
    with pytest.raises(StoreError):
        store.drain(client, DOMAIN, REPLY)
    assert backend.deletes == []


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_saved_reply_never_deletes_valid_duplicate(
    number: str, store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [
        task({"request_id": "request-1", "result": {"nested": ["replace-me"]}})
    ]
    store.drain(client, DOMAIN, REPLY)
    backend.deletes.clear()
    path = store.reply_directory / f"{hash_id('request-1')}.json"
    contents = path.read_text(encoding="utf-8")
    assert '"replace-me"' in contents
    corrupted = contents.replace('"replace-me"', number)
    path.write_text(corrupted, encoding="utf-8")
    backend.replies = [task({"request_id": "request-1", "result": "valid"}, TASK2)]
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
            task({"request_id": "same", "winner": index}, (TASK, TASK2)[index])
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


@pytest.mark.parametrize("outcome", ["success", "not_found", "lost"])
def test_cancel_retains_metadata_until_confirmed_delete(
    outcome: str, store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.delete_not_found = outcome == "not_found"
    if outcome == "lost":
        backend.on_delete = lost_response
        with pytest.raises(TransportError):
            store.cancel(client, "request-1")
    else:
        assert store.cancel(client, "request-1") == (
            "cancelled" if outcome == "success" else "not_found"
        )
    record = store.request("request-1")
    assert (record is None) == (outcome == "success")
    if record is not None:
        assert record.state == "pending"


def test_unknown_submission_cannot_cancel(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.on_create = lost_response
    with pytest.raises(TransportError):
        submit(store, client)
    assert store.cancel(client, "request-1") == "submission_unknown"
    assert backend.deletes == []


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
    assert store.wait(client, "request-1", 2.5, 1, max_poll_interval=1) is None
    assert clock.sleeps == [1, 1, 0.5]
    assert len(backend.owns) == 3
    assert backend.timeouts[1:] == [
        {"connect": remaining, "read": remaining, "write": remaining, "pool": remaining}
        for remaining in (2.5, 1.5, 0.5)
    ]
    assert store.request("request-1") is not None
    assert len(backend.creates) == 1


def test_wait_backs_off_to_the_maximum_poll_interval(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(store, client)
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(stores, "uniform", lambda low, high: 1.0)
    assert store.wait(client, "request-1", 60, 1, max_poll_interval=4) is None
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
        backend.replies.append(task({"request_id": "unrelated"}))

    backend.on_own = refill
    assert store.wait(client, "request-1", 2) is None
    assert len(backend.owns) == 4
    # At the deadline the last reply is safely local, without another network call.
    assert len(backend.deletes) == 3
    assert store.reply("unrelated") is not None


def test_prune_expires_records_and_temporaries_but_keeps_metadata(
    store: Store, client: Client, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    submit(store, client, "old")
    backend.replies = [task({"request_id": "old"}), task({"bad": True}, TASK2)]
    store.drain(client, DOMAIN, REPLY)
    temporary = store.request_directory / ".abandoned.random.tmp"
    temporary.write_bytes(b"partial")
    os.utime(temporary, (now, now))
    now += 8 * 86400
    submit(store, client, "recent")
    count = store.prune()
    assert count >= 5  # intent, sidecar, matched, unmatched, temporary
    assert not temporary.exists()
    assert store.request("old") is None
    assert store.request("recent") is not None
    assert store.inbox() == []
    assert Store.agents(store.root) == ["agent-1"]
    assert (store.request_directory / "agent.json").is_file()
    assert (store.reply_directory / "agent.json").is_file()


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


@pytest.mark.parametrize(
    "content", [b"null", b"[]", b"{bad", b'{"request_id":"other"}']
)
def test_corrupt_intent_is_controlled_and_never_resent(
    content: bytes, store: Store, client: Client, backend: Backend
) -> None:
    (store.request_directory / f"{hash_id('request-1')}.json").write_bytes(content)
    with pytest.raises(StoreError):
        store.resend(client, "request-1")
    assert backend.creates == []


def test_escaped_surrogate_reply_is_saved_unmatched_before_delete(
    store: Store, client: Client, backend: Backend
) -> None:
    backend.replies = [task({"request_id": "\ud800", "value": "\ud800"})]

    def inspect() -> None:
        records = store.inbox()
        assert len(records) == 1 and records[0].request_id is None
        assert records[0].data == {"request_id": "\ud800", "value": "\ud800"}

    backend.on_delete = inspect
    assert store.drain(client, DOMAIN, REPLY) == 1
    assert len(backend.deletes) == 1
    assert (store.unmatched_directory / f"{TASK}.json").read_bytes().isascii()


def test_wait_returns_desired_reply_without_draining_unrelated_backlog(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [
        task({"request_id": "request-1"}),
        task({"request_id": "other"}, TASK2),
    ]
    assert store.wait(client, "request-1", 100) is not None
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


def test_concurrent_ordinary_submits_create_once(
    tmp_path: Path, client: Client, backend: Backend
) -> None:
    first, second = Store("agent-1", tmp_path), Store("agent-1", tmp_path)
    barrier = Barrier(2)

    def post(store: Store) -> None:
        barrier.wait()
        submit(store, client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post, first), pool.submit(post, second)]
        for future in futures:
            future.result()
    assert len(backend.creates) == 1
    assert first.request("request-1") == second.request("request-1")


@pytest.mark.parametrize("orphan", ["task", "reply"])
def test_ordinary_submit_cannot_reuse_orphaned_metadata(
    orphan: str, store: Store, client: Client, backend: Backend
) -> None:
    if orphan == "task":
        fsio.publish_new(
            store.request_directory / f"{hash_id('request-1')}.task.json",
            json.dumps({"task_id": TASK}).encode(),
        )
    else:
        backend.replies = [task({"request_id": "request-1"})]
        store.drain(client, DOMAIN, REPLY)
    with pytest.raises(StoreError, match="already has saved"):
        submit(store, client)
    assert backend.creates == []


def test_prune_removes_old_orphan_sidecar_and_keeps_recent_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    paths = [
        store.request_directory / f"{hash_id(value)}.task.json"
        for value in ("old", "recent")
    ]
    for path in paths:
        fsio.publish_new(path, json.dumps({"task_id": TASK}).encode())
    os.utime(paths[0], (now - 8 * 86400, now - 8 * 86400))
    os.utime(paths[1], (now, now))
    assert store.prune() == 1
    assert not paths[0].exists() and paths[1].exists()


@pytest.mark.parametrize("first,second", [(True, 1), (1, 1.0)])
def test_duplicate_input_comparison_preserves_json_value_types(
    first: object, second: object, store: Store, client: Client, backend: Backend
) -> None:
    store.submit(client, DOMAIN, TOPIC, REPLY, {"value": first}, "request-1")
    with pytest.raises(StoreError, match="different request"):
        store.submit(client, DOMAIN, TOPIC, REPLY, {"value": second}, "request-1")
    assert len(backend.creates) == 1


def test_duplicate_object_key_order_is_irrelevant(
    store: Store, client: Client, backend: Backend
) -> None:
    store.submit(client, DOMAIN, TOPIC, REPLY, {"a": 1, "b": 2}, "request-1")
    store.submit(client, DOMAIN, TOPIC, REPLY, {"b": 2, "a": 1}, "request-1")
    assert len(backend.creates) == 1


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
    assert store.wait(client, "request-1", 2) is None
    assert store.request("request-1") is not None


def test_transport_failure_before_wait_deadline_is_reported(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.on_own = lost_response
    with pytest.raises(TransportError):
        store.wait(client, "request-1", 100)


def test_wait_delivers_durable_reply_when_delete_response_is_lost(
    store: Store, client: Client, backend: Backend
) -> None:
    submit(store, client)
    backend.replies = [task({"request_id": "request-1", "result": "safe"})]
    backend.on_delete = lost_response
    reply = store.wait(client, "request-1", 100)
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
