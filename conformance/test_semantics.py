"""Spec-derived authorization, fencing, validation, and atomicity checks."""

import time
from pathlib import Path
from typing import cast
from urllib.parse import quote, urlsplit

import pytest

from conformance.support import ServiceTarget
from tasklease.blobs import download_file, upload_file
from tasklease.errors import Empty, Forbidden, Invalid, NotFound, TooLarge
from tasklease.generated import protocol as p
from tasklease.names import topic_matches


@pytest.mark.parametrize(
    ("glob", "topic", "expected"),
    [
        ("verify/linux", "verify/linux", True),
        ("verify/linux", "verify/linux/error", False),
        ("verify/*", "verify/linux", True),
        ("verify/*", "verify", False),
        ("verify/*", "verify/linux/error", False),
        ("verify/**", "verify", False),
        ("verify/**", "verify/linux", True),
        ("verify/**", "verify/linux/error", True),
        ("**", "verify", True),
        ("**", "verify/linux/error", True),
        ("*", "verify/linux", False),
        ("mac-*", "mac-a1b2", True),
        ("mac-*", "mac-", True),
        ("mac-*", "mac", False),
        ("a*b*c", "abc", True),
        ("a*b*c", "a-one-b-two-c", True),
        ("a*b*c", "a-one-b-two-d", False),
        ("a*b*c", "ac", False),
        ("a.b", "axb", False),
        ("a.b", "a.b", True),
        ("**/error", "error", False),
        ("**/error", "work/error", True),
        ("**/error", "work/linux/error", True),
        ("**/work/**", "a/work/b", True),
        ("**/work/**", "work/b", False),
        ("**/work/**", "a/work", False),
        ("a/**/b/**/c", "a/x/b/y/c", True),
        ("a/**/b/**/c", "a/b/y/c", False),
        ("**/**", "one", False),
        ("**/**", "one/two", True),
        ("work/**/error", "work/error", False),
        ("work/**/error", "work/linux/error", True),
        ("work/**/error", "work/linux/mac/error", True),
        ("work", "other/work", False),
        ("work", "work/other", False),
    ],
)
def test_public_glob_match_agrees_with_service_authorization(
    target: ServiceTarget, glob: str, topic: str, expected: bool
) -> None:
    assert topic_matches(glob, topic) is expected
    _, token = target.identity(create=(glob,), own=(), read=())
    with target.client(token) as client:
        request: p.CreateRequest = {
            "domain": target.domain,
            "topic": topic,
            "data": {},
        }
        if expected:
            assert client.create(request)["id"].startswith("t_")
        else:
            with pytest.raises(Forbidden):
                client.create(request)


def test_error_grants_are_append_only_glob_expansions(target: ServiceTarget) -> None:
    name, token = target.identity(
        create=("reply/*",),
        own=("work", "work/error", "verify/**"),
        read=(),
    )
    with target.client(token) as client:
        whoami = client.identity_whoami({})
        assert whoami["name"] == name
        assert whoami["domains"] == [target.domain]
        assert whoami["operator"] is False
        assert whoami["scopes"] == {
            "create": ["reply/*", "work/error", "work/error/error", "verify/**/error"],
            "own": ["work", "work/error", "verify/**"],
            "read": [],
        }


def test_scope_checks_and_identity_holder_rule(target: ServiceTarget) -> None:
    _, owner_token = target.identity(create=("work", "reply"), own=("work",), read=())
    _, reader_token = target.identity(create=(), own=(), read=("work",))
    _, stranger_token = target.identity(create=(), own=(), read=())
    _, other_owner_token = target.identity(create=(), own=("work",), read=())
    with (
        target.client(owner_token, "first") as owner,
        target.client(owner_token, "second") as same_identity,
        target.client(reader_token) as reader,
        target.client(stranger_token) as stranger,
        target.client(other_owner_token) as other,
    ):
        created = owner.create({"domain": target.domain, "topic": "work", "data": {}})
        ref: p.ReadRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": created["id"],
        }
        assert reader.read(ref)["task"]["owner"] is None
        assert owner.read(ref)["task"]["owner"] is None  # own grants read
        for client in (reader, stranger):
            with pytest.raises(Forbidden):
                client.own(
                    {"domain": target.domain, "topic": "work", "hold_seconds": 1}
                )
        with pytest.raises(Forbidden):
            stranger.read(ref)
        with pytest.raises(Forbidden):
            stranger.topic_pollers({"domain": target.domain, "topic": "work"})
        held = owner.own(
            {"domain": target.domain, "topic": "work", "hold_seconds": 60}
        )["task"]
        held_ref: p.ReadRequest = {**ref, "id": held["id"]}
        for client in (reader, stranger, other):
            with pytest.raises(Forbidden):
                client.renew({**held_ref, "hold_seconds": 60})
            with pytest.raises(Forbidden):
                client.consume({**held_ref, "tasks": []})
            with pytest.raises(Forbidden):
                client.delete(cast(p.DeleteRequest, held_ref))
        # Worker labels identify pollers, not authorization holders.
        assert (
            same_identity.renew({**held_ref, "hold_seconds": 60})["task"]["id"]
            == held["id"]
        )
        same_identity.consume({**held_ref, "tasks": []})


@pytest.mark.parametrize("container", ["array", "object"])
def test_deep_json_remains_readable_through_task_lifecycle(
    target: ServiceTarget, container: str
) -> None:
    value: p.JsonValue = 0
    for _ in range(500):
        value = [value] if container == "array" else {"child": value}
    _, token = target.identity()
    with target.client(token) as client:
        created = client.create(
            {"domain": target.domain, "topic": "work", "data": {"nested": value}}
        )
        ref: p.ReadRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": created["id"],
        }
        original = client.read(ref)["task"]
        owned = client.own(
            {"domain": target.domain, "topic": "work", "hold_seconds": 60}
        )["task"]
        renewed = client.renew({**ref, "id": owned["id"], "hold_seconds": 60})["task"]
        successor_id = client.consume(
            {
                **ref,
                "id": owned["id"],
                "tasks": [{"topic": "reply", "data": owned["data"]}],
            }
        )["ids"][0]
        successor = client.read(
            {"domain": target.domain, "topic": "reply", "id": successor_id}
        )["task"]
    for task in (original, owned, renewed, successor):
        result = task["data"]["nested"]
        for _ in range(500):
            if container == "array":
                assert isinstance(result, list)
                result = result[0]
            else:
                assert isinstance(result, dict)
                result = result["child"]
        assert result == 0


def test_empty_polls_record_each_worker_and_successful_owns(
    target: ServiceTarget,
) -> None:
    name, token = target.identity()
    with target.client(token, "worker-a") as a, target.client(token, "worker-b") as b:
        request: p.OwnRequest = {
            "domain": target.domain,
            "topic": "work",
            "hold_seconds": 60,
        }
        for client in (a, b):
            with pytest.raises(Empty):
                client.own(request)
        pollers = a.topic_pollers({"domain": target.domain, "topic": "work"})["pollers"]
        assert {(p["identity"], p["worker"]) for p in pollers} == {
            (name, "worker-a"),
            (name, "worker-b"),
        }
        assert all(p["last_owned_at"] is None for p in pollers)
        a.create({"domain": target.domain, "topic": "work", "data": {}})
        task = b.own(request)["task"]
        pollers = a.topic_pollers({"domain": target.domain, "topic": "work"})["pollers"]
        owned = next(p for p in pollers if p["worker"] == "worker-b")
        assert owned["last_owned_at"] == task["owned_at"]
        assert owned["last_polled_at"] >= cast(int, task["owned_at"])


def test_reown_fences_every_mutation_and_preserves_lineage(
    target: ServiceTarget,
) -> None:
    _, token = target.identity()
    with (
        target.client(token, "first") as first,
        target.client(token, "second") as second,
    ):
        created = first.create(
            {"domain": target.domain, "topic": "work", "data": {"value": 42}}
        )
        original = first.read(
            {"domain": target.domain, "topic": "work", "id": created["id"]}
        )["task"]
        owned = first.own(
            {"domain": target.domain, "topic": "work", "hold_seconds": 1}
        )["task"]
        assert owned["id"] != original["id"] and owned["owned_count"] == 1
        if target.board is not None:
            target.board.expire(owned["id"])
        else:
            time.sleep(1.05)
        # Expiry alone leaves the ID live; the previous holder can renew it.
        old_ref: p.RenewRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": owned["id"],
            "hold_seconds": 1,
        }
        assert first.renew(old_ref)["task"]["id"] == owned["id"]
        if target.board is not None:
            target.board.expire(owned["id"])
        else:
            time.sleep(1.05)
        reowned = second.own(
            {"domain": target.domain, "topic": "work", "hold_seconds": 60}
        )["task"]
        assert reowned["id"] != owned["id"] and reowned["owned_count"] == 2
        for field in ("created_at", "creator", "data", "domain", "topic"):
            assert reowned[field] == original[field]
        stale: p.ReadRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": owned["id"],
        }
        with pytest.raises(NotFound):
            first.read(stale)
        with pytest.raises(NotFound):
            first.renew(old_ref)
        with pytest.raises(NotFound):
            first.consume({**stale, "tasks": []})
        with pytest.raises(NotFound):
            first.delete(cast(p.DeleteRequest, stale))
        second.consume(
            {"domain": target.domain, "topic": "work", "id": reowned["id"], "tasks": []}
        )


def test_creator_delete_and_strict_lease_cutoff(target: ServiceTarget) -> None:
    _, token = target.identity(create=("work",), own=(), read=())
    _, inspector_token = target.identity(create=(), own=(), read=("work",))
    with target.client(token) as creator, target.client(inspector_token) as reader:
        identifier = creator.create(
            {"domain": target.domain, "topic": "work", "data": {}}
        )["id"]
        ref: p.ReadRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": identifier,
        }
        lease = reader.read(ref)["task"]["lease_until"]
        with pytest.raises(Forbidden):
            reader.delete(cast(p.DeleteRequest, ref))
        with pytest.raises(Forbidden):
            creator.delete({**ref, "lease_before": lease})
        assert reader.read(ref)["task"]["id"] == identifier
        creator.delete({**ref, "lease_before": lease + 1})
        with pytest.raises(NotFound):
            creator.delete(cast(p.DeleteRequest, ref))


def test_consume_validates_all_successors_before_changing_state(
    target: ServiceTarget,
) -> None:
    _, token = target.identity(create=("work", "reply"), own=("work",), read=("**",))
    with target.client(token) as client:
        client.create(
            {"domain": target.domain, "topic": "work", "data": {"request_id": "atomic"}}
        )
        task = client.own(
            {"domain": target.domain, "topic": "work", "hold_seconds": 60}
        )["task"]
        ref: p.ReadRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": task["id"],
        }
        good: p.NewTask = {"topic": "reply", "data": {"value": "good"}}
        cases: list[tuple[list[p.NewTask], type[Exception]]] = [
            ([good, {"topic": "denied", "data": {}}], Forbidden),
            ([good, {"topic": "UPPER", "data": {}}], Invalid),
            (
                [good, {"topic": "reply", "data": {"value": "x" * (256 * 1024)}}],
                TooLarge,
            ),
            ([good] * 17, TooLarge),
        ]
        for successors, error in cases:
            with pytest.raises(error):
                client.consume({**ref, "tasks": successors})
            assert client.read(ref)["task"]["id"] == task["id"]
            assert (
                client.tasks_list({"domain": target.domain, "topic": "reply"})["tasks"]
                == []
            )
        ids = client.consume({**ref, "tasks": [good] * 16})["ids"]
        assert len(set(ids)) == 16
        assert (
            len(client.tasks_list({"domain": target.domain, "topic": "reply"})["tasks"])
            == 16
        )
        with pytest.raises(NotFound):
            client.consume({**ref, "tasks": [good]})


@pytest.mark.parametrize(
    ("operation", "body", "status", "code", "message"),
    [
        (
            "create",
            {"topic": "UPPER", "data": {}},
            400,
            "invalid",
            "Invalid request body",
        ),
        (
            "own",
            {"topic": "work", "hold_seconds": 0},
            400,
            "invalid",
            "Invalid request body",
        ),
        (
            "own",
            {"topic": "work", "hold_seconds": 86401},
            400,
            "invalid",
            "Invalid request body",
        ),
        ("own", {"topic": "work", "hold_seconds": 1}, 404, "empty", "empty"),
        (
            "create",
            {"topic": "work", "data": {"value": "x" * (256 * 1024)}},
            413,
            "too_large",
            "Task data exceeds 256 KiB",
        ),
        (
            "blob.put_url",
            {"name": "file", "size": 2**31 + 1, "sha256": "a" * 64},
            413,
            "too_large",
            "Blob exceeds 2 GiB",
        ),
        (
            "blob.put_url",
            {"name": "file", "size": 0, "sha256": "a" * 64},
            400,
            "invalid",
            "Invalid request body",
        ),
    ],
)
def test_refusal_envelopes_match_worker(
    target: ServiceTarget,
    operation: str,
    body: dict[str, object],
    status: int,
    code: str,
    message: str,
) -> None:
    _, token = target.identity()
    with target.http() as http:
        response = http.post(
            target.url + "/v1/" + operation,
            headers={"Authorization": "Bearer " + token, "X-Worker": "wire"},
            json={"domain": target.domain, **body},
        )
    assert response.status_code == status
    assert response.json() == {"ok": False, "error": {"code": code, "message": message}}


def test_blob_domain_scopes_key_shape_and_absence(target: ServiceTarget) -> None:
    _, token = target.identity(create=("work",), own=(), read=("work",))
    _, denied = target.identity(create=(), own=(), read=())
    with target.client(token) as client, target.client(denied) as stranger:
        request: p.BlobPutUrlRequest = {
            "domain": target.domain,
            "name": "file.bin",
            "size": 2**31,
            "sha256": "a" * 64,
        }
        signed = client.blob_put_url(request)
        assert signed["key"].startswith(quote(target.domain, safe="") + "/")
        assert signed["key"].endswith("/file.bin")
        assert len(signed["key"].split("/")[-2]) == 26
        assert urlsplit(signed["url"]).query
        with pytest.raises(NotFound):
            client.blob_get_url({"domain": target.domain, "key": signed["key"]})
        with pytest.raises(Invalid):
            client.blob_get_url(
                {"domain": target.domain, "key": "other/" + signed["key"]}
            )
        with pytest.raises(Forbidden):
            stranger.blob_put_url(request)
        with pytest.raises(Forbidden):
            stranger.blob_get_url({"domain": target.domain, "key": signed["key"]})


def test_presigned_blob_transfer_and_public_helpers(
    target: ServiceTarget, tmp_path: Path
) -> None:
    if target.board is None:
        pytest.skip(
            "Local Wrangler signs real R2 URLs; transfer needs deployed R2 acceptance"
        )
    _, token = target.identity()
    source = tmp_path / "input.bin"
    source.write_bytes(b"offline payload\x00" * 1000)
    with target.client(token) as client, target.board.blob_http() as blobs:
        uploaded = upload_file(client, target.domain, source, http=blobs)
        result = download_file(
            client,
            target.domain,
            uploaded.key,
            tmp_path / "download.bin",
            size=uploaded.size,
            sha256=uploaded.sha256,
            http=blobs,
        )
        assert result.read_bytes() == source.read_bytes()
        signed = client.blob_put_url(
            {"domain": target.domain, "name": "bad.bin", "size": 3, "sha256": "a" * 64}
        )
        assert (
            blobs.put(
                signed["url"], headers={"Content-Length": "2"}, content=b"hi"
            ).status_code
            >= 400
        )
        with pytest.raises(NotFound):
            client.blob_get_url({"domain": target.domain, "key": signed["key"]})


def test_data_limit_uses_compact_javascript_json(target: ServiceTarget) -> None:
    _, token = target.identity()
    with target.client(token) as client:
        # JSON.stringify serializes integral floats as integers. Counting the
        # Python spelling instead would reject this otherwise valid 200 KiB task.
        data: p.Data = {"values": [1.0] * 100_000}
        identifier = client.create(
            {"domain": target.domain, "topic": "work", "data": data}
        )["id"]
        ref: p.ReadRequest = {
            "domain": target.domain,
            "topic": "work",
            "id": identifier,
        }
        assert client.read(ref)["task"]["data"] == data
        exact: p.Data = {"text": "x" * (256 * 1024 - len('{"text":""}'))}
        client.create({"domain": target.domain, "topic": "work", "data": exact})
        with pytest.raises(TooLarge):
            client.create(
                {
                    "domain": target.domain,
                    "topic": "work",
                    "data": {"text": "x" * (256 * 1024 - len('{"text":""}') + 1)},
                }
            )


def test_authentication_validation_and_worker_header_order(
    target: ServiceTarget,
) -> None:
    _, token = target.identity()
    with target.http() as http:
        url = target.url + "/v1/create"
        unauthenticated = http.post(url, content=b"not-json")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {
            "ok": False,
            "error": {"code": "unauthenticated", "message": "unauthenticated"},
        }
        malformed = http.post(
            url, headers={"Authorization": "Bearer " + token}, json={}
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"] == {
            "code": "invalid",
            "message": "Invalid request body",
        }
        missing_worker = http.post(
            url,
            headers={"Authorization": "Bearer " + token},
            json={"domain": target.domain, "topic": "work", "data": {}},
        )
        assert missing_worker.status_code == 400
        assert missing_worker.json()["error"] == {
            "code": "invalid",
            "message": "Invalid X-Worker header",
        }
        boolean_hold = http.post(
            target.url + "/v1/own",
            headers={"Authorization": "Bearer " + token, "X-Worker": "wire"},
            json={"domain": target.domain, "topic": "work", "hold_seconds": True},
        )
        assert boolean_hold.status_code == 400
        assert boolean_hold.json()["error"] == {
            "code": "invalid",
            "message": "Invalid request body",
        }


def test_oldest_available_order_and_consume_result_order(target: ServiceTarget) -> None:
    _, token = target.identity()
    with target.client(token) as client:
        tasks: list[p.Task] = []
        for value in range(4):
            identifier = client.create(
                {"domain": target.domain, "topic": "work", "data": {"value": value}}
            )["id"]
            tasks.append(
                client.read(
                    {"domain": target.domain, "topic": "work", "id": identifier}
                )["task"]
            )
        expected = min(tasks, key=lambda task: (task["created_at"], task["id"]))
        listing = client.tasks_list({"domain": target.domain, "topic": "work"})["tasks"]
        assert [task["id"] for task in listing] == [
            task["id"]
            for task in sorted(tasks, key=lambda task: (task["created_at"], task["id"]))
        ]
        assert all("data" not in task for task in listing)
        owned = client.own(
            {"domain": target.domain, "topic": "work", "hold_seconds": 60}
        )["task"]
        assert owned["data"] == expected["data"]
        ids = client.consume(
            {
                "domain": target.domain,
                "topic": "work",
                "id": owned["id"],
                "tasks": [
                    {"topic": "reply", "data": {"value": value}} for value in range(3)
                ],
            }
        )["ids"]
        assert [
            client.read({"domain": target.domain, "topic": "reply", "id": identifier})[
                "task"
            ]["data"]["value"]
            for identifier in ids
        ] == [0, 1, 2]


def test_topics_list_uses_read_scopes_and_optional_glob(target: ServiceTarget) -> None:
    _, all_token = target.identity()
    _, reader_token = target.identity(create=(), own=(), read=("visible/*",))
    _, owner_token = target.identity(create=(), own=("visible/*",), read=())
    with target.client(all_token) as all_topics:
        for topic in ("visible/one", "visible/two", "hidden"):
            all_topics.create({"domain": target.domain, "topic": topic, "data": {}})
    with target.client(reader_token) as reader, target.client(owner_token) as owner:
        topics = reader.topics_list({"domain": target.domain})["topics"]
        assert {topic["topic"] for topic in topics} == {"visible/one", "visible/two"}
        filtered = reader.topics_list({"domain": target.domain, "glob": "**/one"})[
            "topics"
        ]
        assert [topic["topic"] for topic in filtered] == ["visible/one"]
        assert owner.topics_list({"domain": target.domain})["topics"] == []
        assert (
            len(
                owner.tasks_list({"domain": target.domain, "topic": "visible/one"})[
                    "tasks"
                ]
            )
            == 1
        )
