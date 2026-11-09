# Changelog

User-visible changes and required operator actions are recorded here.
Release preparation and verification are documented in
[the release guide](docs/releasing.md).

## [Unreleased]

### Added

- Public typed `tasklease.testing.FakeBoard`: a thread-safe, single-domain
  in-process service with scoped identities, injectable time, real clients,
  lease controls, state snapshots and offline blob transfers. Shared
  conformance tests run against both this board and the local Worker.
- `tasklease.blobs.upload_file` and `download_file`, with streaming SHA-256,
  separate transfer clients, size checks and partial-download cleanup.
  `UploadedBlob` carries the key, size and digest; `BlobTransferError` covers
  signing, transfer, integrity and filesystem failures.
- Public `tasklease.names.is_request_id` and `topic_matches` predicates for
  requester-compatible IDs and the Worker's scope glob semantics.

### Fixed

- Correct the documented `WorkerLoop` constructor to include its domain.

### Compatibility and deployment

- The `0.2.0` candidate adds compatible Python APIs and retains protocol
  `/v1/`. Existing `0.1.0` services work with these APIs; no service upgrade
  is required. The bundled service reports the package's new version.
- The testing service and transfer helpers ship in the main wheel with no
  additional runtime dependencies. Offline fake-only tests need no Node.js,
  Wrangler, credentials or network access. Real presigned R2 transfers still
  require deployed-service acceptance.

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
