"""Focused checks for the offline service's controls and concurrent use."""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest

from tasklease.blobs import BlobTransferError, download_file
from tasklease.errors import (
    Empty,
    Forbidden,
    Invalid,
    NotFound,
    TooLarge,
    Unauthenticated,
)
from tasklease.generated import protocol as p
from tasklease.testing import BlobPutGrant, FakeBoard


@dataclass
class Clock:
    now: int = 1_800_000_000_123

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def board(clock: Clock) -> FakeBoard:
    service = FakeBoard("acme/project", clock=clock)
    service.add_identity("worker", "token", create=["**"], own=["**"], read=["**"])
    return service


def test_clients_are_offline_and_closing_one_does_not_close_board(
    board: FakeBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_connection(_socket: object, _address: object) -> None:
        raise AssertionError("An offline fake attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    with board.client("token") as client:
        created = client.create({"domain": board.domain, "topic": "work", "data": {}})
    with board.client("token", "second") as client:
        assert (
            client.read({"domain": board.domain, "topic": "work", "id": created["id"]})[
                "task"
            ]["owner"]
            is None
        )
    with board.blob_http() as http:
        assert not http.headers.get("Authorization")
        assert http.get("https://example.invalid/unsigned").status_code == 403


def test_identity_error_grants_and_limits(board: FakeBoard) -> None:
    board.add_identity("limited", "limited", own=["jobs/*"])
    with board.client("limited") as client:
        who = client.identity_whoami({})
        assert who["domains"] == [board.domain]
        assert who["operator"] is False
        assert who["scopes"] == {
            "create": ["jobs/*/error"],
            "own": ["jobs/*"],
            "read": [],
        }
        client.create({"domain": board.domain, "topic": "jobs/linux/error", "data": {}})
        with pytest.raises(Forbidden):
            client.create({"domain": board.domain, "topic": "jobs/linux", "data": {}})
    with pytest.raises(ValueError):
        board.add_identity("too-long", "long", own=["x" * 256])
    with pytest.raises(ValueError):
        board.add_identity(
            "too-many", "many", create=[f"x{i}" for i in range(256)], own=["job"]
        )
    with pytest.raises(ValueError):
        board.add_identity("worker", "another")
    with pytest.raises(ValueError):
        board.add_identity("another", "token")


def test_clock_expiry_fencing_and_same_identity_holder(
    board: FakeBoard, clock: Clock
) -> None:
    with (
        board.client("token", "first") as client,
        board.client("token", "second") as second,
    ):
        created = client.create(
            {"domain": board.domain, "topic": "work", "data": {"x": 1}}
        )["id"]
        held = client.own({"domain": board.domain, "topic": "work", "hold_seconds": 1})[
            "task"
        ]
        assert held["id"] != created
        assert held["created_at"] == clock.now
        assert held["lease_until"] == clock.now + 1000
        with pytest.raises(Empty):
            second.own({"domain": board.domain, "topic": "work", "hold_seconds": 1})
        # Worker labels are diagnostic; holder authorization uses the identity.
        renewed = second.renew(
            {
                "domain": board.domain,
                "topic": "work",
                "id": held["id"],
                "hold_seconds": 2,
            }
        )["task"]
        assert renewed["id"] == held["id"]
        board.expire(held["id"])
        reowned = second.own(
            {"domain": board.domain, "topic": "work", "hold_seconds": 60}
        )["task"]
        assert reowned["owned_count"] == 2
        assert reowned["owner"] == {"identity": "worker", "worker": "second"}
        for operation in ("renew", "consume", "delete"):
            request: dict[str, object] = {
                "domain": board.domain,
                "topic": "work",
                "id": held["id"],
            }
            with pytest.raises(NotFound):
                if operation == "renew":
                    request["hold_seconds"] = 1
                    client.renew(cast(p.RenewRequest, request))
                elif operation == "consume":
                    request["tasks"] = []
                    client.consume(cast(p.ConsumeRequest, request))
                else:
                    client.delete(cast(p.DeleteRequest, request))


def test_scope_checks_and_consume_atomicity(board: FakeBoard) -> None:
    board.add_identity("limited", "limited", own=["work"], create=["reply"])
    board.add_identity("none", "none")
    with (
        board.client("token") as requester,
        board.client("limited") as limited,
        board.client("none") as none,
    ):
        requester.create({"domain": board.domain, "topic": "work", "data": {}})
        task_id = limited.own(
            {"domain": board.domain, "topic": "work", "hold_seconds": 60}
        )["task"]["id"]
        base: p.ReadRequest = {"domain": board.domain, "topic": "work", "id": task_id}
        with pytest.raises(Forbidden):
            none.read(base)
        with pytest.raises(Forbidden):
            none.renew({**base, "hold_seconds": 1})
        with pytest.raises(Forbidden):
            none.consume({**base, "tasks": []})
        before = board.tasks
        with pytest.raises(Forbidden):
            limited.consume(
                {
                    **base,
                    "tasks": [
                        {"topic": "reply", "data": {}},
                        {"topic": "denied", "data": {}},
                    ],
                }
            )
        assert board.tasks == before
        with pytest.raises(TooLarge):
            limited.consume(
                {
                    **base,
                    "tasks": [{"topic": "reply", "data": {"x": "a" * (256 * 1024)}}],
                }
            )
        assert board.tasks == before
        with pytest.raises(Forbidden):
            requester.delete(cast(p.DeleteRequest, base))
        with pytest.raises(Forbidden):
            limited.delete({**base, "lease_before": before[task_id]["lease_until"]})
        assert board.tasks == before
        limited.consume({**base, "tasks": [{"topic": "reply", "data": {"ok": True}}]})
        assert task_id not in board.tasks
        assert len(board.tasks) == 1


def test_same_millisecond_order_uses_random_ids(
    board: FakeBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = iter([3, 1, 2, 4, 5, 6])
    monkeypatch.setattr("tasklease.testing.secrets.randbits", lambda _: next(values))
    with board.client("token") as client:
        for value in (0, 1, 2):
            client.create(
                {"domain": board.domain, "topic": "work", "data": {"value": value}}
            )
        assert [
            client.own({"domain": board.domain, "topic": "work", "hold_seconds": 60})[
                "task"
            ]["data"]["value"]
            for _ in range(3)
        ] == [1, 2, 0]


def test_snapshot_mutation_cannot_change_board(board: FakeBoard) -> None:
    with board.client("token") as client:
        task_id = client.create(
            {"domain": board.domain, "topic": "work", "data": {"list": [1]}}
        )["id"]
        snapshot = board.tasks
        cast(list[p.JsonValue], snapshot[task_id]["data"]["list"]).append(2)
        assert board.tasks[task_id]["data"] == {"list": [1]}
        client.own({"domain": board.domain, "topic": "work", "hold_seconds": 1})
        next(iter(board.pollers.values()))["last_polled_at"] = 0
        assert next(iter(board.pollers.values()))["last_polled_at"] != 0


def test_empty_poll_throttle_and_retention(board: FakeBoard, clock: Clock) -> None:
    with board.client("token") as client:
        for offset in (0, 1, 59_999, 60_000):
            clock.now = 1_800_000_000_123 + offset
            with pytest.raises(Empty):
                client.own({"domain": board.domain, "topic": "work", "hold_seconds": 1})
            poller = client.topic_pollers({"domain": board.domain, "topic": "work"})[
                "pollers"
            ][0]
            assert poller["last_polled_at"] == 1_800_000_000_123 + (
                60_000 if offset == 60_000 else 0
            )
            assert poller["last_owned_at"] is None
        clock.now += 7 * 86400 * 1000 + 1
        assert (
            client.topic_pollers({"domain": board.domain, "topic": "work"})["pollers"]
            == []
        )
        assert client.topics_list({"domain": board.domain})["topics"] == []


def test_concurrent_clients_and_renewals(board: FakeBoard) -> None:
    def process(index: int) -> str:
        with board.client("token", f"worker-{index}") as client:
            client.create(
                {"domain": board.domain, "topic": "work", "data": {"x": index}}
            )
            owned = client.own(
                {"domain": board.domain, "topic": "work", "hold_seconds": 60}
            )["task"]
            renewed = client.renew(
                {
                    "domain": board.domain,
                    "topic": "work",
                    "id": owned["id"],
                    "hold_seconds": 120,
                }
            )["task"]
            assert renewed["lease_until"] > owned["lease_until"]
            return owned["id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        task_ids = list(executor.map(process, range(32)))
    assert len(set(task_ids)) == len(board.tasks) == len(board.pollers) == 32


def test_blobs_size_scopes_expiry_and_url_integrity(
    board: FakeBoard, clock: Clock
) -> None:
    board.add_identity("none", "none")
    with (
        board.client("token") as client,
        board.client("none") as none,
        board.blob_http() as blobs,
    ):
        signed = client.blob_put_url(
            {"domain": board.domain, "name": "file.bin", "size": 3, "sha256": "a" * 64}
        )
        assert signed["key"].startswith("acme%2Fproject/")
        assert signed["expires_at"] == clock.now // 1000 * 1000 + 3_600_000
        with pytest.raises(NotFound):
            client.blob_get_url({"domain": board.domain, "key": signed["key"]})
        assert blobs.put(signed["url"], content=b"ab").status_code == 403
        assert (
            blobs.put(
                signed["url"], headers={"Content-Length": "3"}, content=b"ab"
            ).status_code
            == 400
        )
        assert not board.blobs
        assert blobs.put(signed["url"], content=b"abc").status_code == 200
        assert board.blobs[signed["key"]] == b"abc"
        with pytest.raises(Forbidden):
            none.blob_get_url({"domain": board.domain, "key": signed["key"]})
        with pytest.raises(Forbidden):
            none.blob_put_url(
                {"domain": board.domain, "name": "x", "size": 1, "sha256": "a" * 64}
            )
        get = client.blob_get_url({"domain": board.domain, "key": signed["key"]})
        assert blobs.get(get["url"]).content == b"abc"
        assert blobs.put(get["url"], content=b"abc").status_code == 403
        assert blobs.get(get["url"] + "0").status_code == 403
        clock.now = signed["expires_at"]
        assert blobs.get(get["url"]).status_code == 403
        assert blobs.put(signed["url"], content=b"abc").status_code == 403


def test_public_blob_transport_supports_streaming_fault_wrappers(
    board: FakeBoard,
) -> None:
    class RejectPut(httpx.BaseTransport):
        def __init__(self) -> None:
            self.inner = board.blob_transport()
            self.chunks: list[bytes] = []

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                assert isinstance(request.stream, httpx.SyncByteStream)
                self.chunks.extend(request.stream)
                return httpx.Response(503)
            return self.inner.handle_request(request)

        def close(self) -> None:
            self.inner.close()

    first, second = board.blob_transport(), board.blob_transport()
    assert first is not second
    first.close()
    wrapper = RejectPut()
    with board.client("token") as client:
        signed = client.blob_put_url(
            {"domain": board.domain, "name": "file", "size": 3, "sha256": "a" * 64}
        )
    assert signed["url"].startswith(board.blob_base_url + "/")
    with httpx.Client(transport=wrapper, base_url=board.blob_base_url) as blobs:
        assert blobs.put(signed["url"], content=iter([b"a", b"bc"])).status_code == 503
    assert wrapper.chunks == [b"a", b"bc"]
    with httpx.Client(transport=second) as blobs:
        assert blobs.put(signed["url"], content=b"abc").status_code == 200


def test_put_grants_are_read_only_detached_and_exclude_expired_gets(
    board: FakeBoard, clock: Clock
) -> None:
    with board.client("token") as client, board.blob_http() as blobs:
        signed = client.blob_put_url(
            {"domain": board.domain, "name": "file", "size": 3, "sha256": "a" * 64}
        )
        snapshot = board.put_grants
        grant = snapshot[signed["url"]]
        assert grant == BlobPutGrant(signed["key"], 3, signed["expires_at"])
        with pytest.raises(TypeError):
            cast(dict[str, BlobPutGrant], snapshot)[signed["url"]] = grant
        with pytest.raises(FrozenInstanceError):
            grant.__setattr__("size", 999)
        assert blobs.put(signed["url"], content=b"abc").status_code == 200
        client.blob_get_url({"domain": board.domain, "key": signed["key"]})
        assert board.put_grants == snapshot
        client.blob_put_url(
            {"domain": board.domain, "name": "other", "size": 1, "sha256": "b" * 64}
        )
        assert len(snapshot) == 1
        assert len(board.put_grants) == 2
        clock.now = signed["expires_at"]
        assert not board.put_grants
        assert snapshot[signed["url"]] == grant


def test_overwrite_blob_injects_corruption_without_changing_grants(
    board: FakeBoard, tmp_path: Path
) -> None:
    with board.client("token") as client, board.blob_http() as blobs:
        signed = client.blob_put_url(
            {"domain": board.domain, "name": "file", "size": 3, "sha256": "a" * 64}
        )
        with pytest.raises(KeyError):
            board.overwrite_blob(signed["key"], b"xyz")
        assert blobs.put(signed["url"], content=b"abc").status_code == 200
        before = board.put_grants
        board.overwrite_blob(signed["key"], b"xyz")
        assert board.blobs[signed["key"]] == b"xyz"
        assert board.put_grants == before
        with pytest.raises(TypeError):
            board.overwrite_blob(signed["key"], cast(bytes, bytearray(b"abc")))
        destination = tmp_path / "downloaded"
        destination.write_bytes(b"existing")
        with pytest.raises(BlobTransferError, match="SHA-256"):
            download_file(
                client,
                board.domain,
                signed["key"],
                destination,
                size=3,
                sha256=hashlib.sha256(b"abc").hexdigest(),
                http=blobs,
            )
        assert destination.read_bytes() == b"existing"
        assert not list(tmp_path.glob("*.part"))


def test_wire_validation_and_lone_surrogates(board: FakeBoard) -> None:
    with httpx.Client(transport=board.transport(), base_url=board.base_url) as http:
        headers = {"Authorization": "Bearer token", "X-Worker": "wire"}
        assert http.post("/v1/create", content=b"not-json").status_code == 401
        assert (
            http.post("/v1/create", content=b"not-json", headers=headers).status_code
            == 400
        )
        for body in (
            b'{"domain":"acme/project","topic":"work","data":{"x":NaN}}',
            b'{"domain":"acme/project","topic":"work","data":{"x":1e400}}',
        ):
            assert (
                http.post("/v1/create", content=body, headers=headers).status_code
                == 400
            )
        response = http.post(
            "/v1/create",
            content=b'{"domain":"acme/project","topic":"work","data":{"x":"\\ud800"}}',
            headers=headers,
        )
        assert response.status_code == 200
    with board.client("token") as client:
        assert (
            client.own({"domain": board.domain, "topic": "work", "hold_seconds": 1})[
                "task"
            ]["data"]["x"]
            == "\ud800"
        )
        with pytest.raises(Invalid):
            client.read({"domain": board.domain, "topic": "work", "id": "bad"})
        with pytest.raises(Unauthenticated):
            with board.client("unknown") as unknown:
                unknown.identity_whoami({})


def test_data_byte_limit_is_utf8(board: FakeBoard) -> None:
    with board.client("token") as client:
        client.create(
            {
                "domain": board.domain,
                "topic": "work",
                "data": {"x": "é" * ((256 * 1024 - 8) // 2)},
            }
        )
        with pytest.raises(TooLarge):
            client.create(
                {
                    "domain": board.domain,
                    "topic": "work",
                    "data": {"x": "é" * ((256 * 1024 - 8) // 2 + 1)},
                }
            )


def test_number_byte_limits_follow_worker_json_stringify(board: FakeBoard) -> None:
    # JSON.stringify emits 1 for 1.0; Python's json.dumps would emit 1.0 and
    # incorrectly reject the first task even though it is below 256 KiB.
    with board.client("token") as client:
        client.create(
            {"domain": board.domain, "topic": "work", "data": {"x": [1.0] * 100_000}}
        )
        client.create(
            {
                "domain": board.domain,
                "topic": "precision",
                "data": {
                    "unsafe": 9_007_199_254_740_993,
                    "small": 1e-6,
                    "large": 1e20,
                    "zero": -0.0,
                },
            }
        )
        data = client.own(
            {"domain": board.domain, "topic": "precision", "hold_seconds": 1}
        )["task"]["data"]
        assert data == {
            "unsafe": 9_007_199_254_740_992,
            "small": 1e-6,
            "large": 1e20,
            "zero": 0,
        }
        with pytest.raises(TooLarge):
            client.create(
                {
                    "domain": board.domain,
                    "topic": "work",
                    "data": {"x": [1.0] * 140_000},
                }
            )


@pytest.mark.parametrize(
    ("now", "day"),
    [(0, "1970-01-01"), (253402300800000, "+010000-01"), (2**48 - 1, "+010889-08")],
)
def test_blob_dates_follow_worker_across_ulid_clock_range(now: int, day: str) -> None:
    board = FakeBoard("demo", clock=lambda: now)
    board.add_identity("files", "token", create=["**"])
    with board.client("token") as client:
        signed = client.blob_put_url(
            {"domain": "demo", "name": "file", "size": 1, "sha256": "a" * 64}
        )
    assert signed["key"].split("/")[1] == day


def test_blob_key_uses_proleptic_gregorian_dates(board: FakeBoard) -> None:
    suffix = "/00000000000000000000000000/file"
    with board.client("token") as client:
        for day in ("0000-02-29", "2000-02-29"):
            with pytest.raises(NotFound):
                client.blob_get_url(
                    {"domain": board.domain, "key": "acme%2Fproject/" + day + suffix}
                )
        for day in ("0001-02-29", "1900-02-29", "2024-13-01", "2024-01-00"):
            with pytest.raises(Invalid):
                client.blob_get_url(
                    {"domain": board.domain, "key": "acme%2Fproject/" + day + suffix}
                )


def test_deep_snapshot_is_detached_and_does_not_hide_live_task(
    board: FakeBoard,
) -> None:
    data: p.Data = {}
    for _ in range(500):
        data = {"child": data}
    with board.client("token") as client:
        identifier = client.create(
            {"domain": board.domain, "topic": "work", "data": data}
        )["id"]
        snapshot = board.tasks[identifier]["data"]
        for _ in range(500):
            snapshot = cast(p.Data, snapshot["child"])
        snapshot["changed"] = True
        live = client.read({"domain": board.domain, "topic": "work", "id": identifier})[
            "task"
        ]["data"]
        for _ in range(500):
            live = cast(p.Data, live["child"])
        assert live == {}


def test_generated_optional_request_fields_remain_optional(board: FakeBoard) -> None:
    with board.client("token") as client:
        created = client.create({"domain": board.domain, "topic": "work", "data": {}})
        ref: p.ReadRequest = {
            "domain": board.domain,
            "topic": "work",
            "id": created["id"],
        }
        task = client.read(ref)["task"]
        assert len(client.topics_list({"domain": board.domain})["topics"]) == 1
        assert (
            client.topics_list({"domain": board.domain, "glob": "other"})["topics"]
            == []
        )
        assert (
            len(client.tasks_list({"domain": board.domain, "topic": "work"})["tasks"])
            == 1
        )
        cursor = (
            base64.urlsafe_b64encode(f"v1:{task['created_at']}:{task['id']}".encode())
            .decode()
            .rstrip("=")
        )
        assert (
            client.tasks_list(
                {"domain": board.domain, "topic": "work", "cursor": cursor}
            )["tasks"]
            == []
        )
        with pytest.raises(Forbidden):
            client.delete({**ref, "lease_before": task["lease_until"]})
        client.delete(cast(p.DeleteRequest, ref))
    with httpx.Client(transport=board.transport(), base_url=board.base_url) as http:
        response = http.post(
            "/v1/topics.list",
            headers={"Authorization": "Bearer token", "X-Worker": "test"},
            json={"domain": board.domain, "unknown": True},
        )
        assert response.status_code == 400


def test_topics_list_aggregates_tasks_and_empty_pollers(
    board: FakeBoard, clock: Clock
) -> None:
    start = clock.now
    with board.client("token") as client:
        client.create({"domain": board.domain, "topic": "b", "data": {}})
        clock.now += 1
        client.create({"domain": board.domain, "topic": "a", "data": {}})
        client.own({"domain": board.domain, "topic": "a", "hold_seconds": 60})
        clock.now += 1
        client.create({"domain": board.domain, "topic": "b", "data": {}})
        with pytest.raises(Empty):
            client.own({"domain": board.domain, "topic": "idle", "hold_seconds": 60})
        topics = client.topics_list({"domain": board.domain})["topics"]
        assert topics == [
            {
                "topic": "a",
                "available": 0,
                "held": 1,
                "oldest_available_at": None,
                "declared": False,
            },
            {
                "topic": "b",
                "available": 2,
                "held": 0,
                "oldest_available_at": start,
                "declared": False,
            },
            {
                "topic": "idle",
                "available": 0,
                "held": 0,
                "oldest_available_at": None,
                "declared": False,
            },
        ]


def test_number_wire_format_preserves_fixed_and_exponent_boundaries(
    board: FakeBoard,
) -> None:
    with board.client("token") as client:
        created = client.create(
            {
                "domain": board.domain,
                "topic": "work",
                "data": {
                    "numbers": [-0.0, 1.0, 1.5, 1e-6, 1e-7, 1e20, 1e21],
                },
            }
        )
    with httpx.Client(transport=board.transport(), base_url=board.base_url) as http:
        response = http.post(
            "/v1/read",
            headers={"Authorization": "Bearer token", "X-Worker": "test"},
            json={"domain": board.domain, "topic": "work", "id": created["id"]},
        )
        assert response.status_code == 200
        assert (
            b'"numbers":[0,1,1.5,0.000001,1e-7,100000000000000000000,1e+21]'
            in response.content
        )


def test_long_poll_wakes_on_create_without_holding_the_lock(
    board: FakeBoard,
) -> None:
    """A waiting Own releases the lock, so another thread can Create."""
    board.add_identity("creator", "creator-token", create=["work/wait"])
    waiter = board.client("token", "waiting-worker")
    created = threading.Event()

    def create() -> None:
        time.sleep(0.1)
        board.client("creator-token").create(
            {"domain": board.domain, "topic": "work/wait", "data": {"n": 1}}
        )
        created.set()

    thread = threading.Thread(target=create)
    thread.start()
    started = time.monotonic()
    task = waiter.own(
        {
            "domain": board.domain,
            "topic": "work/wait",
            "hold_seconds": 60,
            "wait_seconds": 1,
        }
    )["task"]
    thread.join()
    assert created.is_set()
    assert task["data"] == {"n": 1}
    assert time.monotonic() - started < 1.5


def test_long_poll_times_out_empty_and_zero_is_immediate(board: FakeBoard) -> None:
    client = board.client("token")
    started = time.monotonic()
    with pytest.raises(Empty):
        client.own(
            {
                "domain": board.domain,
                "topic": "work/idle",
                "hold_seconds": 60,
                "wait_seconds": 0,
            }
        )
    assert time.monotonic() - started < 0.5
    with pytest.raises(Empty):
        client.own(
            {
                "domain": board.domain,
                "topic": "work/idle",
                "hold_seconds": 60,
                "wait_seconds": 1,
            }
        )
    assert time.monotonic() - started < 3


def test_long_poll_claims_a_consumed_successor_once(board: FakeBoard) -> None:
    client = board.client("token")
    client.create({"domain": board.domain, "topic": "work/a", "data": {}})
    held = client.own({"domain": board.domain, "topic": "work/a", "hold_seconds": 60})[
        "task"
    ]

    def consume() -> None:
        time.sleep(0.1)
        board.client("token", "second").consume(
            {
                "domain": board.domain,
                "topic": "work/a",
                "id": held["id"],
                "tasks": [{"topic": "work/b", "data": {"step": 2}}],
            }
        )

    thread = threading.Thread(target=consume)
    thread.start()
    task = client.own(
        {
            "domain": board.domain,
            "topic": "work/b",
            "hold_seconds": 60,
            "wait_seconds": 1,
        }
    )["task"]
    thread.join()
    assert task["data"] == {"step": 2}
    with pytest.raises(Empty):
        client.own(
            {
                "domain": board.domain,
                "topic": "work/b",
                "hold_seconds": 60,
                "wait_seconds": 0,
            }
        )


def test_long_poll_rejects_out_of_range_waits(board: FakeBoard) -> None:
    client = board.client("token")
    for wait in (-1, 31, 1.5, True, "5"):
        with pytest.raises(Invalid):
            client.own(
                {
                    "domain": board.domain,
                    "topic": "work",
                    "hold_seconds": 60,
                    "wait_seconds": wait,  # type: ignore[typeddict-item]
                }
            )


def test_long_poll_refuses_a_wait_beyond_max_waiters() -> None:
    board = FakeBoard("example/project", max_waiters=0)
    board.add_identity("worker", "token", own=["work/full"])
    client = board.client("token")
    started = time.monotonic()
    with pytest.raises(Empty) as raised:
        client.own(
            {
                "domain": board.domain,
                "topic": "work/full",
                "hold_seconds": 60,
                "wait_seconds": 30,
            }
        )
    assert raised.value.wait_refused
    assert time.monotonic() - started < 1
    # An immediate poll is never refused.
    with pytest.raises(Empty) as immediate:
        client.own({"domain": board.domain, "topic": "work/full", "hold_seconds": 60})
    assert not immediate.value.wait_refused


def test_long_poll_is_not_cut_short_by_frequent_wakeups(
    board: FakeBoard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creates on other topics wake every waiter; only the deadline ends one."""
    wakeups = [0]

    def woken(timeout: float | None = None) -> bool:
        wakeups[0] += 1
        return True

    monkeypatch.setattr(board._changed, "wait", woken)
    started = time.monotonic()
    with pytest.raises(Empty):
        board.client("token").own(
            {
                "domain": board.domain,
                "topic": "work/quiet",
                "hold_seconds": 60,
                "wait_seconds": 1,
            }
        )
    assert time.monotonic() - started >= 0.9
    assert wakeups[0] > 1000
