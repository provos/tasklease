"""Protocol checks shared by the packaged Worker and in-process board."""

import json
from pathlib import Path
from typing import cast
from urllib.parse import quote

import pytest

from conformance.support import ADMIN_TOKEN, LostResponse, ServiceTarget
from tasklease import __version__
from tasklease.errors import Empty, Forbidden, NotFound, TransportError, Unauthenticated
from tasklease.generated.protocol import ConsumeRequest

ROOT = Path(__file__).resolve().parents[1]
INVALID_REQUESTS = sorted((ROOT / "schema/examples").glob("*/invalid/request*.json"))


def test_packaged_version(target: ServiceTarget) -> None:
    with target.client("unused") as client:
        assert client.version() == {"service_version": __version__, "protocol": "v1"}


def test_client_task_flow(target: ServiceTarget) -> None:
    domain = target.domain
    name, token = target.identity()
    with target.client(token, "worker-1") as client:
        assert client.identity_whoami({})["name"] == name
        created = client.create(
            {
                "domain": domain,
                "topic": "work",
                "data": {"request_id": "r-1", "value": 42},
            }
        )
        assert client.read({"domain": domain, "topic": "work", "id": created["id"]})[
            "task"
        ]["data"] == {"request_id": "r-1", "value": 42}
        assert (
            client.tasks_list({"domain": domain, "topic": "work"})["tasks"][0]["id"]
            == created["id"]
        )
        owned = client.own({"domain": domain, "topic": "work", "hold_seconds": 60})[
            "task"
        ]
        assert owned["owned_count"] == 1
        renewed = client.renew(
            {"domain": domain, "topic": "work", "id": owned["id"], "hold_seconds": 120}
        )["task"]
        assert renewed["lease_until"] >= owned["lease_until"]
        assert (
            client.topic_pollers({"domain": domain, "topic": "work"})["pollers"][0][
                "worker"
            ]
            == "worker-1"
        )
        reply = client.consume(
            {
                "domain": domain,
                "topic": "work",
                "id": renewed["id"],
                "tasks": [
                    {"topic": "reply", "data": {"request_id": "r-1", "status": "ok"}}
                ],
            }
        )["ids"][0]
        with pytest.raises(NotFound):
            client.read({"domain": domain, "topic": "work", "id": created["id"]})
        drained = client.own({"domain": domain, "topic": "reply", "hold_seconds": 60})[
            "task"
        ]
        assert drained["id"] != reply  # Own replaces the old ID as its fencing token.
        assert drained["data"] == {"request_id": "r-1", "status": "ok"}
        client.consume(
            {"domain": domain, "topic": "reply", "id": drained["id"], "tasks": []}
        )
        with pytest.raises(Empty):
            client.own({"domain": domain, "topic": "reply", "hold_seconds": 60})
        topics = client.topics_list({"domain": domain})["topics"]
        assert (
            next(topic for topic in topics if topic["topic"] == "work")["available"]
            == 0
        )
        disposable = client.create({"domain": domain, "topic": "delete", "data": {}})[
            "id"
        ]
        assert client.delete(
            {"domain": domain, "topic": "delete", "id": disposable}
        ) == {"ok": True}
        signed = client.blob_put_url(
            {"domain": domain, "name": "report.json", "size": 42, "sha256": "a" * 64}
        )
        assert signed["key"].startswith(quote(domain, safe="") + "/")
        assert signed["url"].startswith("https://")
        assert "X-Amz-Signature=" in signed["url"]
        assert signed["expires_at"] > 0
        # The local bucket has no uploaded object; signed HTTP upload/download
        # remains a separate external acceptance check.
        with pytest.raises(NotFound):
            client.blob_get_url({"domain": domain, "key": signed["key"]})
        with pytest.raises(Forbidden):
            client.topics_list({"domain": "another-domain"})


def test_identity_admin_and_operator_flow(target: ServiceTarget) -> None:
    target.require("identity.add")
    name, token = target.identity(operator=True, create=(), own=(), read=())
    with target.client(ADMIN_TOKEN) as admin:
        assert any(
            identity["name"] == name
            for identity in admin.identity_list({})["identities"]
        )
        with target.client(token, "operator") as client:
            assert client.topic_declare(
                {"domain": target.domain, "topic": "work", "declared": True}
            ) == {"ok": True}
            client.create({"domain": target.domain, "topic": "work", "data": {}})
            assert client.monitor_snapshot({"domain": target.domain})["topics"]
        rotated = admin.identity_rotate({"name": name})["token"]
        assert rotated != token
        with target.client(rotated) as replacement:
            assert replacement.identity_whoami({})["name"] == name
        admin.identity_revoke({"name": name})
        assert all(
            identity["name"] != name
            for identity in admin.identity_list({})["identities"]
        )
        # Cached credentials may remain valid for 60 seconds. Revoke a
        # never-authenticated credential to test rejection without waiting.
        unused_name, unused = target.identity(create=(), own=(), read=())
        admin.identity_revoke({"name": unused_name})
        with target.client(unused) as revoked:
            with pytest.raises(Unauthenticated):
                revoked.identity_whoami({})


@pytest.mark.parametrize(
    "path", INVALID_REQUESTS, ids=lambda path: str(path.relative_to(ROOT))
)
def test_invalid_request_corpus(
    corpus_target: ServiceTarget, corpus_token: str, path: Path
) -> None:
    operation = path.parents[1].name
    corpus_target.require(operation)
    example: object = json.loads(path.read_text())
    assert isinstance(example, dict)
    body = cast(dict[str, object], example)["body"]
    token = (
        ADMIN_TOKEN
        if operation.startswith("identity.") and operation != "identity.whoami"
        else corpus_token
    )
    with corpus_target.http() as http:
        response = http.post(
            corpus_target.url + "/v1/" + operation,
            headers={"Authorization": f"Bearer {token}", "X-Worker": "corpus"},
            json=body,
        )
    assert response.status_code in (400, 413), response.text
    value: object = response.json()
    assert isinstance(value, dict)
    envelope = cast(dict[str, object], value)
    assert envelope["ok"] is False
    error = envelope["error"]
    assert isinstance(error, dict) and error["code"] in ("invalid", "too_large")


def test_lost_create_and_consume_are_not_retried(target: ServiceTarget) -> None:
    domain = target.domain
    _, token = target.identity()
    with target.client(
        token, "lost-worker", transport=LostResponse(target.transport(), "create")
    ) as client:
        with pytest.raises(TransportError):
            client.create(
                {"domain": domain, "topic": "work", "data": {"request_id": "lost"}}
            )
        tasks = client.tasks_list({"domain": domain, "topic": "work"})["tasks"]
        assert len(tasks) == 1
        held = client.own({"domain": domain, "topic": "work", "hold_seconds": 60})[
            "task"
        ]
    with target.client(
        token, "lost-worker", transport=LostResponse(target.transport(), "consume")
    ) as client:
        consume: ConsumeRequest = {
            "domain": domain,
            "topic": "work",
            "id": held["id"],
            "tasks": [{"topic": "reply", "data": {"request_id": "lost"}}],
        }
        with pytest.raises(TransportError):
            client.consume(consume)
        with pytest.raises(NotFound):
            client.consume(consume)
        assert (
            len(client.tasks_list({"domain": domain, "topic": "reply"})["tasks"]) == 1
        )
