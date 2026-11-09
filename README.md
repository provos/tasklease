# tasklease

A leased task board for coordinating agents and workers across machines.
Cloudflare Workers and SQLite Durable Objects hold tasks; R2 holds large
files. The Python package includes a typed client, durable requester, generic
worker, operator CLI, local dashboard and the prebuilt service bundle.

Own rotates a task's ID, fencing previous owners. Consume atomically retires
that ID and posts up to 16 successors, including replies. Renew preserves the
ID. The requester saves replies locally before acknowledging the server copy.

## Install

Python 3.12 or newer is required. Install a built release wheel:

```sh
uv tool install /path/to/tasklease-0.1.0-py3-none-any.whl
tl --help
```

Wheel and sdist installations include the dashboard and Worker, so installation
and normal client/worker/monitor use need no Node.js. Service deployment uses
Node.js and the exact Wrangler version recorded in the installed bundle.
Public package publication is tracked separately in the acceptance checklist.

For development, use Node `^22.12.0 || >=24`, Python 3.12+ and uv:

```sh
make install
make check
make build
```

Artifacts are written under `python/dist/`. `make check` runs generated-contract
drift checks, Workers-runtime tests, Svelte component/model checks, Python
tests and cross-language conformance against an isolated local Worker.

## Deploy the service

Choose fresh Worker and bucket names for a new service. Follow the CLI's
Cloudflare login and R2 credential instructions:

```sh
tl service deploy --help
tl service deploy --config service.toml --name YOUR_WORKER --bucket YOUR_BUCKET
tl service status --config service.toml
```

Use a Cloudflare login with Workers and R2 management access. For signed R2
uploads, supply `TL_R2_ACCESS_KEY_ID` and `TL_R2_SECRET_ACCESS_KEY` for the chosen
bucket. The command configures the bucket lifecycle, uploads the bundled
Worker, installs secrets and verifies a task/blob round trip.

Save the admin candidate printed during bootstrap and follow its confirmation
status. A timed-out confirmation leaves an unconfirmed candidate; the CLI
explains recovery. Expected setup stops save the nonsecret target coordinates
so the next invocation resumes the same service. Tokens and R2 credentials
are never saved in `service.toml`. See [deployment design](docs/design.md#82-tl-service-deploy)
for the complete recovery sequence.

## Request work and run an echo worker

Set `TL_ADMIN_TOKEN` to the confirmed admin token, then issue two identities:

```sh
tl --config service.toml identity add requester --domain demo --create work/echo --own reply/agent
tl --config service.toml identity add echo-worker --domain demo --own work/echo --create reply/agent
```

Save each returned token in its own private token file. Give the requester
this `requester.toml`:

```toml
url = "https://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev"
domain = "demo"
agent_id = "agent"

[token]
file = "requester.token"
```

Give the worker this `worker.toml`; token paths are relative to their config:

```toml
url = "https://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev"
domain = "demo"
worker_label = "echo-on-this-machine"

[token]
file = "worker.token"

[worker]
topic = "work/echo"
handler = "echo"
```

Start the worker in another terminal or on another machine:

```sh
tl --config worker.toml worker
```

Post a request and wait for its reply:

```sh
tl --config requester.toml request --topic work/echo --reply-topic reply/agent --data '{"message":"hello"}' --wait --timeout 1m
tl --config requester.toml inbox
```

The echo handler accepts a message of at most 4096 UTF-8 bytes and returns
the message and request ID. Custom handlers use an importable
`module:factory`, implementing `validate`, `run` and `reply`. The library's
`WorkerLoop.step()` also supports an externally held capacity lock;
`WorkerLoop.run()` acquires capacity before Own.

Gracefully stop the worker on its machine with
`tl --worker echo-on-this-machine worker stop`. Platform startup templates
and installation instructions are in [deploy/](deploy/README.md).

## Recovery and monitoring

Request commands print a request ID, including after uncertain submission or
an interrupted wait. Resume with `tl wait --request-id ID`; it checks durable
local replies first. `tl request --resend ID` explicitly retries only an
unknown submission and can create another execution. `tl cancel --request-id
ID` uses the saved task ID and reports when cancellation cannot be confirmed.

Worker leases default to 600 seconds, with renewal every 180 seconds. A lost
lease sets the handler's `lost` event; handlers should cooperate with it.
Fenced Consume prevents stale completion, while external side effects still
need application-level idempotence. Automated workers refuse `/error` topics;
operators inspect those manually.

```sh
tl --config worker.toml monitor
tl --config worker.toml topics
tl --config worker.toml pollers --help
tl inbox --all
```

The monitor binds to `127.0.0.1`, keeps the identity credential in Python,
and shows only service topics the identity may read or own. It also reads
this machine's agent stores. Requests are read-only and cached for 25 seconds.
Use `--no-browser` for a headless monitor and `--port` for a fixed local port.
`--domain D` selects one service domain; local records still cover every
agent on this machine.

Configuration supports `TL_CONFIG`, `TL_URL`, `TL_DOMAIN`, `TL_AGENT_ID`,
`TL_WORKER` and `TL_TOKEN`; admin commands use environment-only `TL_ADMIN_TOKEN`.
Domain names `.` and `..` are prohibited. Domains otherwise follow the grammar
in the [protocol specification](docs/spec.md).

## Reference and acceptance

- [Specification](docs/spec.md) and [implementation design](docs/design.md).
- [Phase checklist](docs/implementation-checklist.md) and [review evidence](docs/review-log.md).
- [Local release/native acceptance](docs/releasing.md) and [Windows agent prompt](docs/windows-agent-prompt.md).
- [Schema contract](schema/README.md), generated SDK DTOs and shared examples.

FluxShot-specific profiles and workers belong in the FluxShot repository:
[tasklease profiles](https://github.com/provos/fluxshoteditor/blob/master/docs/point-designs/tasklease-profiles.md).
