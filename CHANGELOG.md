# Changelog

User-visible changes and required operator actions are recorded here.
Release preparation and verification are documented in
[the release guide](docs/releasing.md).

## [Unreleased]

### Added

- `Own` accepts an optional `wait_seconds` from 0 to 30. With a positive
  value, a poll holds the request until a task is available in the topic or
  the wait ends, returning the task or `empty`. A worker that is idle picks
  up new work within a round trip instead of waiting for its next poll.
- `tl worker` and `WorkerLoop.run()` long-poll by default with a 30-second
  wait. Configure it with the `[worker] wait_seconds` setting, `--wait-seconds`
  or `WorkerLoop(wait_seconds=...)`; `0` or `off` restores immediate polling.
  Time a poll spends waiting counts toward `poll_interval`, so a worker
  configured to poll every 600 seconds still sends one Own about every 600
  seconds. `WorkerLoop.step()` polls at once unless called with
  `step(wait_seconds=...)`, and `tl worker --once` and a `WorkerLoop.run()`
  whose capacity is a real lock also poll at once, so an idle wait never
  holds a machine lock. `tl request --wait`, `tl wait` and the durable
  requester also wait on the server, so a reply is seen within about a second
  instead of after backoff; set the top-level `wait_seconds` setting or pass
  `--wait-seconds` to change it.
- When a long-polled Own cannot hold its wait (the topic already has the
  maximum number of waiters, or a deployment ends the wait before any claim),
  it answers `empty` with `"wait_refused": true` in its error object; the
  Python client exposes it as `Empty.wait_refused`, and the requester backs
  off instead of polling again straight away.
- `FakeBoard(max_waiters=...)` limits held long polls per topic, so offline
  tests can reach `wait_refused`; `0` refuses every wait.

- A worker stopped (`tl worker stop`, a first signal, or `stop.set()`) while
  its Own is held does not start a task that Own returns. It renews the task
  for one second so another worker can own it almost at once, and exits.

### Changed

- `Store.wait`, and so `tl request --wait` and `tl wait`, retries a transport
  failure with backoff until its timeout instead of failing, so a service
  deployment during a long wait does not end it.

### Compatibility and deployment

- Long-polling clients require service version 0.4.0 or later. An older
  service rejects the new Own field with `invalid`; no automatic downgrade is
  attempted. The client then raises `tasklease.errors.WaitUnsupported` (an
  `Invalid`), whose message names the service's reported version and the
  opt-out, and the CLI prints it. Upgrade the service first, or set
  `wait_seconds = 0` on older services: in the `[worker]` table for workers,
  and at the top level for `tl request` and `tl wait`. Existing clients and
  protocol `/v1/` requests without the field behave exactly as before.
- A held poll keeps a Durable Object resident and so bills compute duration
  for that time. Request counts for a 30-second wait match a 30-second short
  poll; a client that polls deliberately rarely should set `wait_seconds = 0`.
- Held waiters are in memory and bounded per topic and per identity within a
  topic. A deployment or eviction ends them with an unheld `empty`, so clients
  poll again; no task state depends on them.

## [0.3.0] - 2026-11-10

### Changed

- `tl topic declare TOPIC` selects its domain from `--domain`, `TL_DOMAIN` or
  the config's `domain`, in that order; the positional domain argument is removed.
- POSIX identity token files must have no group or other permission bits.
  Files such as mode `0644` are rejected with an instruction to use `chmod 600`.

### Fixed

- Document operator identity issuance, client config locations and token modes,
  topic declarations, scoped machine identities and error-topic operations.
  CLI config help, including service commands, now shows the platform's default
  config path. Token-permission errors explain the default token-file location.
- Validate explicit domains locally in commands using the shared domain resolver.
  Topic declaration uses one configuration snapshot for domain and connection
  settings.

### Compatibility and deployment

- The CLI syntax and POSIX token-file checks are breaking changes. Replace
  `tl topic declare DOMAIN TOPIC` with `tl topic declare TOPIC --domain DOMAIN`,
  or configure the domain. On macOS/Linux, run `chmod 600` on identity token files
  that permit group or other access before using them.
- Python APIs and protocol `/v1/` are unchanged. The `0.3.0` client works with
  existing `0.1.0` and `0.2.0` services; no service upgrade is required. The bundled
  service reports version `0.3.0`.

## [0.2.0] - 2026-11-09

### Added

- Public typed `tasklease.testing.FakeBoard`: a thread-safe, single-domain
  in-process service with scoped identities, injectable time, real clients,
  lease controls, state snapshots and offline blob transfers. Shared
  conformance tests run against both this board and the local Worker.
- `tasklease.blobs.upload_file` and `download_file`, with streaming SHA-256,
  separate transfer clients, size checks and partial-download cleanup.
  `UploadedBlob` carries the key, size and digest; `BlobTransferError` covers
  signing, transfer, integrity and filesystem failures.
- Optional `total_timeout` budgets hashing, signing and blob transfer, with
  cooperative monotonic deadline checks and capped HTTP phase timeouts.
  Blocking calls and HTTPX response-header reads cannot be interrupted;
  hard cutoffs require process supervision.
  Transfer errors retain safe HTTP status, timeout and filesystem reasons.
- `FakeBoard.blob_transport()`, `blob_base_url`, immutable `put_grants`
  snapshots and `overwrite_blob()` support transfer fault and corruption tests.
- `WorkerControl(..., on_second_signal=cleanup)` runs an optional fast cleanup
  callback once before forced exit, including when the callback raises.
- Public `tasklease.names.is_request_id`, `is_task_id`, `is_topic_glob` and
  `topic_matches` predicates for requester-compatible IDs, fencing IDs and
  the Worker's scope glob syntax and matching semantics.

### Fixed

- Correct the documented `WorkerLoop` constructor to include its domain.
- Blob downloads reject encoded responses already decoded by a transport
  wrapper, preserving stored-byte verification. Blob signing failures retain
  their typed API error as the cause of `BlobTransferError`.

### Compatibility and deployment

- `0.2.0` adds compatible Python APIs and retains protocol
  `/v1/`. Existing `0.1.0` services work with these APIs; no service upgrade
  is required. The bundled service reports the package's new version.
- The testing service and transfer helpers ship in the main wheel with no
  additional runtime dependencies. Offline fake-only tests need no Node.js,
  Wrangler, credentials or network access. Real presigned R2 transfers use
  the deployed service and its configured bucket.

## [0.1.0] - 2026-11-08

Initial release.

### Added

- Task service on Cloudflare Workers with SQLite Durable Objects, scoped
  machine identities, leases, atomic task completion and successor creation,
  and signed R2 blob uploads and downloads.
- Typed Python client and durable requester with local request/reply storage,
  explicit resend and inbox management.
- Python worker runtime with capacity reservation, lease renewal, deadlines,
  failure replies and graceful shutdown. A handler replies to a request or
  posts a pipeline's next stages; `reply_topic` is an optional field of the
  task data, and a failure without one goes to the topic's error topic.
- `tl` CLI for service deployment, upgrades, identity and task administration,
  requesters, workers and a local Svelte dashboard.
- Bundled dashboard, Worker and startup templates for Linux, macOS and Windows;
  wheel and sdist installation and normal runtime use do not require Node.js.
- Apache-2.0 licensing and package metadata with installation and deployment
  instructions, plus third-party license notices for the bundled assets.

### Compatibility and deployment

- Python 3.12 or newer is required. Deploying the bundled service also requires
  Node.js and uses the pinned Wrangler version in the bundle.
- The service uses protocol `/v1/`; its service version follows the Python
  package version.
- New installations must deploy the bundled service with `tl service deploy`
  before configuring requesters and workers. For upgrades from prerelease
  builds, preserve the existing service configuration and credentials and
  follow the tested upgrade path in the release guide.
