# Offline testing and blob transfers

The public `tasklease.testing.FakeBoard` runs a single-domain service inside
your Python process. It needs no sockets, Node.js, Wrangler or credentials.
It ships in the main typed package and uses the existing HTTPX dependency.

## An offline client

```python
from tasklease.testing import FakeBoard

now = [1_800_000_000_000]  # milliseconds since the Unix epoch
board = FakeBoard("demo", clock=lambda: now[0])
board.add_identity("requester", "requester-token", create=["work"], read=["work"])
board.add_identity("worker", "worker-token", own=["work"])

with board.client("requester-token", "requester") as requester:
    created = requester.create({"domain": "demo", "topic": "work", "data": {}})
    with board.client("worker-token", "worker-a") as worker:
        task = worker.own({"domain": "demo", "topic": "work", "hold_seconds": 60})[
            "task"
        ]
        assert task["id"] != created["id"]
        board.expire(task["id"])
        # Or advance now[0] to exercise time-dependent behavior.
```

`client()` returns the production `tasklease.client.Client`, using an
`httpx.MockTransport`. Failures have the service's HTTP status and error
envelope, so the client raises its usual `Empty`, `Forbidden`, `NotFound`,
and other API exceptions. `transport()` creates a fresh transport for tests
that need raw HTTP or wrap a transport to inject lost responses; use
`board.base_url` as its API URL.

Identities are provisioned with `add_identity(name, token, *, create=(),
own=(), read=())`. Own grants include the corresponding create grants for
error topics, as on the service. Request authorization and holder checks
still apply on each operation. Repeated worker labels share one poller
entry per identity and topic; holder checks use identity, as on the Worker.

The board supports Create, Own, Renew, Read, Delete, Consume, Tasks List,
Topics List, Topic Pollers, Identity Whoami, blob URL operations and Version.
Identity administration, operator identities, topic declaration and monitor
snapshots are outside its scope. Task IDs use independently random ULID
suffixes: tasks created in one millisecond have no insertion-order guarantee.

The board serializes operations across threads, including renewer calls from
`WorkerLoop`. `tasks`, `pollers` and `blobs` return detached snapshots for
assertions; changing a snapshot cannot change the board. Injected clocks must
be safe to call from every thread using the board. Omit `clock` to use wall
time, or advance an injected clock to test expiry without sleeping.

## Transfer files

`tasklease.blobs.upload_file()` hashes a file, requests a signed PUT URL and
streams the file with its signed Content-Length. It returns an `UploadedBlob`
with `key`, `size` and `sha256`. Save all three fields in your profile's task
or result data so the recipient can verify the download.

```python
from pathlib import Path

from tasklease.blobs import download_file, upload_file

board.add_identity("files", "files-token", create=["work"], read=["work"])
with board.client("files-token") as api, board.blob_http() as blobs:
    uploaded = upload_file(api, "demo", Path("report.txt"), http=blobs)
    download_file(
        api,
        "demo",
        uploaded.key,
        Path("downloaded.txt"),
        size=uploaded.size,
        sha256=uploaded.sha256,
        http=blobs,
    )
```

In production, omit `http` to create a separate transfer client. The helpers
do not send the API bearer token to blob URLs, do not follow redirects, and
apply a configurable HTTP timeout. An injected HTTP client stays open for
its caller to manage.

Download streams into a temporary sibling of the destination, rejects an
oversized body as soon as it is observed, and checks exact size and SHA-256.
Only a verified download replaces the destination. Failures remove the
partial file and preserve an existing destination. `BlobTransferError` is
the common error type for signing, transfer, integrity and filesystem errors.
Bucket retention and application deadlines remain the profile's policy.

The board's blob client enforces signed URL capabilities, expiry, method and
PUT Content-Length entirely in memory. It models the transfer contract; it
does not implement AWS signature cryptography, R2 durability or lifecycle
deletion. Real signed transfers remain a deployed-service acceptance check.

## Shared predicates

`tasklease.names.is_request_id(value)` accepts exactly the identifiers the
requester store can correlate: a string of 1 to 128 Unicode characters that
encodes as valid UTF-8. The identifier is opaque and need not be a topic name
or a portable filename.

`tasklease.names.topic_matches(glob, topic)` uses the Worker's whole-topic
matching rule. `*` stays within a segment and `**` consumes one or more
complete segments. Thus `work/**` matches `work/linux` and
`work/linux/error`, but does not match `work`. Name and length validation is
separate from matching.

## Conformance

`make conformance` runs the shared contract and runtime tests against both
the fake and an isolated local Worker. Backend-specific capabilities have
explicit exclusions. The shared tests use real clients and raw HTTP requests,
including malformed requests and permission failures. The fake-only target
can run without starting Wrangler; see `conformance/conftest.py` for the
backend selection option.
