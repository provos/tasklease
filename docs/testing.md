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
        now[0] += 60_000  # Advance the injected clock without sleeping.
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

Own's `wait_seconds` long-poll is emulated with the same semantics as the
service: Create, Consume and Renew wake waiting callers, a wait ends at its
deadline with `Empty`, and a frozen injected clock falls back to wall time so
an offline test always terminates. The board releases its lock while waiting,
so a Create or Consume on another thread can wake a waiter.
`FakeBoard(domain, max_waiters=N)` holds at most `N` waits per topic (default
10,000, as on the service); beyond that, Own still attempts the claim and then
raises `Empty` with `wait_refused` set. `max_waiters=0` refuses every wait, to
test a client's backoff.

`blob_transport()` creates a fresh blob-only `httpx.MockTransport`; use
`board.blob_base_url` as its base URL. A custom `httpx.BaseTransport` can wrap
this transport to inspect, fail or delay a PUT before forwarding it. Intercept
`request.stream` in that wrapper to control individual upload chunks: HTTPX's
`MockTransport` reads the body before calling its handler. `blob_http()` builds
a separate credential-free client on `blob_transport()`.

`put_grants` is a read-only detached snapshot mapping unexpired signed PUT URLs
to frozen `BlobPutGrant(key, size, expires_at)` records. GET grants are omitted;
expiry uses the board's clock in Unix milliseconds. `overwrite_blob(key, bytes)`
replaces an existing object's body so tests can inject corruption; unknown keys
raise `KeyError`. It leaves signed grants unchanged. For example, after an
upload, call `board.overwrite_blob(uploaded.key, b"tampered")` and download with
the original size and SHA-256 to check that verification rejects it.

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

`timeout` (default 30 seconds) limits each HTTP transfer operation. Set
`total_timeout`, for example `total_timeout=120`, to budget the entire helper,
including upload hashing, signing and transfer, using monotonic time. The
helpers check the budget between file operations and chunks, and before
committing a download. Signing and transfer HTTP phases are capped to the
remaining budget when each request starts, preserving any shorter configured
signing timeout. This stops a peer from extending a transfer indefinitely by
trickling signing or blob body chunks. It is a cooperative deadline: HTTPX
can perform multiple reads before returning response headers, so trickled
headers or informational responses can delay a budget check indefinitely.
Blocking filesystem calls and custom transports also cannot be interrupted.
Use process-level supervision when a hard external cutoff must be guaranteed.
Omitting `total_timeout` preserves the existing timeout behavior.

Transfer clients and transport wrappers must preserve the raw stream of an
encoded response. If a wrapper has already read and decoded a response with
`Content-Encoding`, the helper raises `BlobTransferError` before creating a
partial file: the original stored bytes are no longer available to verify.
Preloaded responses without content encoding remain supported.

Download streams into a temporary sibling of the destination, rejects an
oversized body as soon as it is observed, and checks exact size and SHA-256.
Only a verified download replaces the destination. Failures remove the
partial file and preserve an existing destination. `BlobTransferError` is
the common error type for signing, transfer, integrity and filesystem errors.
If signing is refused, its typed API exception (for example `Forbidden` or
`TooLarge`) is available as the transfer error's `__cause__`. HTTP transfer
failures report HTTP status, timeout category, or OS errno and its standard
system description. Untrusted exception messages and filenames are omitted
to keep signed URLs out of displayed diagnostics and formatted tracebacks.
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

`tasklease.names.is_task_id(value)` validates a fencing ID (`t_` followed by a
canonical ULID). `tasklease.names.is_topic_glob(value)` validates a scope
glob's syntax and 256-character limit. These predicates are shared by the
requester store and fake's corresponding validation paths.

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
