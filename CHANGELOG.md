# Changelog

User-visible changes and required operator actions are recorded here.
Release preparation and verification are documented in
[the release guide](docs/releasing.md).

## [Unreleased]

Planned initial release: `0.1.0`. This version has not been published yet.

### Added

- Task service on Cloudflare Workers with SQLite Durable Objects, scoped
  machine identities, leases, atomic task completion and successor creation,
  and signed R2 blob uploads and downloads.
- Typed Python client and durable requester with local request/reply storage,
  explicit resend and inbox management.
- Python worker runtime with capacity reservation, lease renewal, deadlines,
  failure replies and graceful shutdown.
- `tl` CLI for service deployment, upgrades, identity and task administration,
  requesters, workers and a local Svelte dashboard.
- Bundled dashboard, Worker and startup templates for Linux, macOS and Windows;
  wheel and sdist installation and normal runtime use do not require Node.js.

### Compatibility and deployment

- Python 3.12 or newer is required. Deploying the bundled service also requires
  Node.js and uses the pinned Wrangler version in the bundle.
- The service uses protocol `/v1/`; its service version follows the Python
  package version.
- New installations must deploy the bundled service with `tl service deploy`
  before configuring requesters and workers. For upgrades from prerelease
  builds, preserve the existing service configuration and credentials and
  follow the tested upgrade path in the release guide.
