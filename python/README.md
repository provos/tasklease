# tasklease

A leased task board for coordinating agents and workers across machines.
Use it for request/reply jobs and pipelines whose stages hand work forward.
Cloudflare Workers and SQLite Durable Objects hold tasks; R2 holds large files.
Workers need only outbound HTTPS.

The package includes:

- A typed Python client and durable requester with local request/reply storage.
- A worker runtime with capacity reservation, lease renewal and graceful shutdown.
- The `tl` CLI for task and identity administration, requesters and workers.
- A local dashboard, the prebuilt Cloudflare service and platform startup templates.

## Install

Python 3.12 or newer is required:

```sh
uv tool install tasklease==0.1.0
tl --help
```

For use as a Python library, add `tasklease` to your application's dependencies.
Wheel and sdist installations include the dashboard and Worker. Installation
and normal client, worker and monitor use do not require Node.js.

## Deploy a service

Service deployment requires Node.js, a Cloudflare account, Wrangler sign-in
and R2 access keys for the chosen bucket. Choose fresh Worker and bucket names:

```sh
tl service deploy --help
tl service deploy --config service.toml --name YOUR_WORKER --bucket YOUR_BUCKET
tl service status --config service.toml
```

The CLI uses the pinned Wrangler version bundled with the package, provisions
the service in your account and verifies a task/blob round trip. Follow its
credential instructions and save the confirmed admin token in private storage.
Use `tl identity add --help` to configure the machines that will exchange work.
Keep handlers idempotent: a task may run again after a worker loses its lease.

See the [repository](https://github.com/provos/tasklease) for client and worker
examples, the protocol specification and operational documentation, and the
[changelog](https://github.com/provos/tasklease/blob/main/CHANGELOG.md) for
compatibility and deployment notes. Repository access may be required.

## License

Apache-2.0. The distribution includes the license text in `LICENSE`.
