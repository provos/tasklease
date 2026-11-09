# tasklease

A leased task board for coordinating agents and workers across machines.
Cloudflare Workers and SQLite Durable Objects hold tasks; R2 holds large
files. The Python package includes a typed client, durable requester, generic
worker, operator CLI, local dashboard and the prebuilt service bundle.

Own rotates a task's ID, fencing previous owners. Consume atomically retires
that ID and posts up to 16 successors, including replies. Renew preserves the
ID. The requester saves replies locally before acknowledging the server copy.

## Example use case

A coding agent on macOS needs to verify a commit on Linux. It posts the
repository, commit ID, and reply topic to `verify/linux`. A Linux worker
checks out the commit, runs the tests and uploads the log to R2. One Consume
operation retires the task and posts a result with the log's blob key. The
requesting agent saves the reply locally.

Outbound HTTPS lets the Linux worker run behind a home or office router.
The project supplies the verification handler; tasklease supplies the leases
and reply handling.

## Choosing tasklease

tasklease suits a small pool of agents and workers exchanging discrete jobs:
platform builds, verification runs, GPU work, or requests for advice. It runs
in your Cloudflare account and includes a Python worker runtime, a requester
that saves replies locally, and a dashboard. Your application defines each
job's payload, handler, and next step. Workers can reserve local capacity,
such as a build slot or GPU, before claiming work.

| Alternative | What it provides | When to prefer it over tasklease |
| --- | --- | --- |
| Temporal | Recovers workflow state by [replaying deterministic code](https://docs.temporal.io/workflow-execution#replays) against recorded history. Durable [timers](https://docs.temporal.io/workflow-execution/timers-delays) and Activity [retry policies](https://docs.temporal.io/encyclopedia/retry-policies) coordinate work across steps. | Processes that must preserve progress across dependent steps, long waits, or worker failures. |
| Celery | Python tasks dispatched through a broker such as RabbitMQ or Redis, with task groups, chains, scheduling, and optional result backends. See the [Celery overview](https://docs.celeryq.dev/en/stable/getting-started/introduction.html). | Python applications that need task composition or scheduling, especially with a broker already in place. |
| Amazon SQS | A managed message queue with [visibility timeouts](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html), redelivery, and deletion after processing. | AWS applications that already supply worker execution, reply correlation, and result storage. |
| Cloudflare Queues | A managed queue with Workers push consumers and [HTTP pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/) for external machines. | Message processing in Workers, or external consumers with their own request/reply conventions and result storage. |

The first version targets tens of queued tasks per topic. Each domain uses
one SQLite Durable Object. Idle workers poll every 30 seconds by default,
with jitter, so pickup delay should suit the workload. Workflow state,
scheduling, and cooperative cancellation of running jobs belong in
application code.

Lease expiry makes a task available for another attempt. The supplied worker
turns handler exceptions into failure replies. Application code decides
whether to retry failed work. ID rotation rejects an old owner's completion
after another worker claims the task. Consume commits task removal and up to
16 successors together within one domain. External effects, such as publishing
an artifact, still need idempotence: a crash can cause a job to run again.

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

To work on the dashboard without a service, serve it with mock data:

```sh
npm run monitor:mock                      # dev server with hot reload
npm run monitor:mock:preview              # the production build
npm run monitor:mock -- --port 5180       # Vite options go after --
```

Pick a scenario in the page URL, for example `/?scenario=empty&delay=3000`:
`busy` (default), `empty`, `degraded` (some sources unavailable), `large`
(many domains and topics, a truncated queue, long names, inbox paging) or
`unreachable` (HTTP 502). `TL_MOCK_SCENARIO` sets the default. Scenarios live
in `monitor/src/mock/` and are tested against the service schema.

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
