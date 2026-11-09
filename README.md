# tasklease

A leased task board for coordinating agents and workers across machines.
Cloudflare Workers and SQLite Durable Objects hold tasks; R2 holds large
files. The Python package includes a typed client, durable requester, generic
worker, operator CLI, local dashboard and the prebuilt service bundle.

Licensed under [Apache-2.0](LICENSE).

Own rotates a task's ID, fencing previous owners. Consume atomically retires
that ID and posts up to 16 successors, including replies. Renew preserves the
ID. The requester saves replies locally before acknowledging the server copy.

## Example use cases

**Request and reply.** A coding agent on macOS needs to verify a commit on
Linux. It posts the repository, commit ID, and reply topic to `verify/linux`.
A Linux worker checks out the commit, runs the tests, and uploads the log to
R2. One Consume operation retires the task and posts a result with the log's
blob key. The requesting agent saves the reply locally. Outbound HTTPS lets
the Linux worker run behind a home or office router. The project supplies
the verification handler; tasklease supplies the leases and reply handling.

**A pipeline.** A crawler posts a URL to `check/url`. A worker fetches the
page, stores it in R2, and in one Consume retires its task and posts a pointer
to the content to both `classify/content` and `classify/security`. Workers on
those topics each post their finding to `assemble`, whose worker writes each
finding to its own column of the URL's row. Every handoff is atomic: a
worker's task is retired exactly when its successors exist, so a crash can
make a stage run again but never loses or duplicates a handoff. The topics
are the graph's stages, and the dashboard shows each one's backlog and
latency.

## Choosing tasklease

**Use tasklease** when agents and machines exchange discrete jobs, either as
requests that end in a reply or as pipelines whose stages hand work forward,
and you want no servers to operate:

- **Jobs:** platform builds, verification runs, GPU work, classification
  stages or requests for advice, taking seconds to hours. A worker holds a
  task for up to 24 hours per Own or Renew and keeps renewing while it works.
  Idle workers poll every 30 seconds by default, so that pickup delay must be
  acceptable.
- **Machines:** laptops, desktops and home or office servers on macOS, Linux
  and Windows. Workers need only outbound HTTPS, including R2 uploads through
  presigned URLs, so they run behind NAT without a VPN, open ports or a
  broker to expose.
- **Scale:** thousands of workers. In a load test against a deployed service,
  5,000 workers, each with its own token, polled one domain with no errors,
  and one domain served about 1,000 requests a second. All domains share one
  identity registry, which has not been tested beyond 5,000 tokens. Plan for
  10,000–100,000 outstanding tasks per domain (see
  [Scale guidance](#scale-guidance)).
- **Operations:** after you create R2 access keys in the Cloudflare
  dashboard, one `tl service deploy` provisions the Worker, its storage, the
  bucket and the secrets in your own Cloudflare account; deploying needs
  Node.js and a Wrangler sign-in. Rerunning it upgrades the service; check the
  changelog for compatibility and migration instructions.
  There are no servers, brokers or databases to patch or back up:
  Cloudflare's durability is the backup. Requests are billed, idle polls
  included, so the scale figures assume a paid Workers plan.

Request/reply is built in: reply topics, a requester that saves replies
locally before acknowledging them, request IDs with explicit resend, a
dashboard, and a Python worker runtime with lease renewal. Pipelines need no
extra machinery: a handler's Consume posts up to 16 successors to any topics
its identity may create in the domain. Workers can reserve local capacity,
such as a build slot or GPU, before claiming work. Your application defines
each job's payload, handler and next step.

**Use something else** when:

- a process needs a central record of its state: steps that wait for several
  results before continuing, durable timers, compensation, or querying and
  cancelling a whole run (**Temporal**);
- Python services in one network already share a broker and need task
  chains, groups or schedules (**Celery**);
- throughput is high (thousands of messages a second), pickup must be
  sub-second, or consumers should scale automatically (**Amazon SQS**,
  **Cloudflare Queues**);
- you cannot use Cloudflare.

### What each option takes to run

| Option | You provision | You maintain | Worker networking | Prefer it for |
| --- | --- | --- | --- | --- |
| tasklease | A Cloudflare account and R2 access keys created in its dashboard. `tl service deploy`, run with Node.js and a Wrangler sign-in, creates the Worker, its Durable Objects, an R2 bucket with a seven-day lifecycle rule and the secrets; `tl identity add` issues one token per machine. | Rerun `tl service deploy` to upgrade; rotate or revoke machine tokens. Workers run as `tl worker` under the [startup templates](deploy/README.md). | Outbound HTTPS only. | Request/reply jobs and handoff pipelines across machines and agents. |
| Temporal | Temporal Cloud, or a self-hosted Temporal Server with its persistence database (PostgreSQL, MySQL or Cassandra). | Self-hosted: server and schema upgrades, database operations. Either way: workflow code must stay [deterministic](https://docs.temporal.io/workflow-execution#replays) and be versioned when it changes. | Workers connect to the Temporal frontend; self-hosted, it must be reachable from every worker. | Multi-step processes with a central record of their state: joins, durable [timers](https://docs.temporal.io/workflow-execution/timers-delays), [retry policies](https://docs.temporal.io/encyclopedia/retry-policies) and compensation. |
| Celery | A broker such as RabbitMQ or Redis, an optional result backend, and the worker processes. See the [Celery overview](https://docs.celeryq.dev/en/stable/getting-started/introduction.html). | Broker upgrades, persistence and monitoring, and result-backend cleanup. | Every worker must reach the broker: expose it with TLS or use a VPN. | Python task composition and scheduling where a broker already runs. |
| Amazon SQS | An AWS account, queues and per-machine IAM credentials, plus your own reply queues and result storage. | Managed queues; you build the worker loop, [visibility-timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html) extension, reply matching and dashboards. | Outbound HTTPS. | Sustained high message rates on AWS. |
| Cloudflare Queues | Queues and consumers in a Cloudflare account; API tokens for [HTTP pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/). | Managed queues; you build request/reply conventions, result storage and monitoring. | Outbound HTTPS for pull consumers. | Bursty traffic absorbed by [automatically scaled Workers consumers](https://developers.cloudflare.com/queues/configuration/consumer-concurrency/). |

Lease expiry makes a task available for another attempt. The supplied worker
turns handler exceptions into failure replies; application code decides
whether to retry failed work. Every Own gives the task a new ID, and Consume
only works with the current one. So if a worker's lease runs out and another
worker claims the task, the old worker's Consume fails and its reply or
downstream tasks are never posted. A queue's visibility timeout does not stop
a timed-out consumer from sending its output. Consume commits task removal
and up to 16 successors together within one domain, so a pipeline lives in
one domain. Fan-out is atomic; fan-in is not built in: a step that must wait
for several results keeps track of what has arrived itself, for example in
the row it writes. A pipeline exists only as the tasks its handlers post, so
carry an ID in the task data to follow or cancel one run. Workflow state,
scheduling and cooperative cancellation belong in application code. Fencing
covers what tasklease posts; external effects, such as writing a finding or
publishing an artifact, still need idempotence, because a crash can cause a
step to run again.

## Install

Python 3.12 or newer is required. Install from PyPI:

```sh
uv tool install tasklease
tl --help
```

Wheel and sdist installations include the dashboard and Worker, so installation
and normal client/worker/monitor use need no Node.js. Service deployment uses
Node.js and the exact Wrangler version recorded in the installed bundle.
Published packages are available on [PyPI](https://pypi.org/project/tasklease/).

For development, use Node `^22.12.0 || >=24`, Python 3.12+ and uv:

```sh
make install
make check
make build
```

Artifacts are written under `python/dist/`. `make check` runs generated-contract
drift checks, Workers-runtime tests, Svelte component/model checks, Python
tests and conformance against both an isolated local Worker and the in-process
fake service.

See [the release guide](docs/releasing.md) for first-release setup and the
checklist for every release, and [the changelog](CHANGELOG.md) for user-visible
changes and deployment notes.

For offline tests, `tasklease.testing.FakeBoard` provides real clients over
in-memory HTTP transports, controlled time, scoped identities and blob
transfers. `tasklease.blobs` supplies streaming file upload and verified
download helpers for tests and production. See [offline testing and blob
transfers](docs/testing.md) for examples and public validation helpers.

To work on the dashboard without a service, serve it with mock data:

```sh
npm run monitor:mock                      # dev server with hot reload
npm run monitor:mock:preview              # the production build
npm run monitor:mock -- --port 5180       # Vite options go after --
```

Pick a scenario in the page URL, for example `/?scenario=empty&delay=3000`:
`busy` (default; an incident with a down machine, an error task and a
lapsed lease), `building` (healthy during a long build), `empty`, `degraded`
(some sources unavailable), `large`
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
the message and request ID. Custom handlers are an importable
`module:factory`; see [Writing a handler](#writing-a-handler). The library's
`WorkerLoop.step()` also supports an externally held capacity lock;
`WorkerLoop.run()` acquires capacity before Own.

Gracefully stop the worker on its machine with
`tl --worker echo-on-this-machine worker stop`. Platform startup templates
and installation instructions are in [deploy/](deploy/README.md).

## API at a glance

Every operation is an authenticated JSON `POST` to `/v1/<operation>`, and the
Python `tasklease.client.Client` has a method for each. The
[specification](docs/spec.md#4-operations) defines each request and response.

| Operation | What it does | Needs |
| --- | --- | --- |
| Create | Posts a task to a topic. | `create` on the topic |
| Own | Leases the oldest available task for 1 second to 24 hours and gives it a new ID. | `own` on the topic |
| Renew | Extends the lease and keeps the ID. | `own`, and holding the task |
| Consume | Retires the held task and posts up to 16 successors, atomically. | `own` on the task's topic, `create` on each successor's topic |
| Delete | Removes a task: its holder's, or its creator's before anyone owned it. | the holder rule |
| Read, Tasks List | One task, or a topic's tasks in pages. | `read` or `own` |
| Topics List, Topic Pollers | Per-topic counts, and who polled a topic recently. | `read` or `own` |
| Topic Declare | Keeps a topic listed while it is empty. | an operator identity |
| Blob Put URL, Blob Get URL | Presigned R2 URLs for payloads too large for a task. | `create`, or `read` or `own`, in the domain |
| Monitor Snapshot | One consistent view of a domain for the dashboard. | `read` or `own` |
| Identity Add, Revoke, Rotate, List | Manage machine tokens. | the admin token |

### Writing a handler

`tl worker` runs a handler on one topic. It owns a task, calls `validate`,
calls `run` while it renews the lease, and then Consumes the task with the
successors `reply` returns: one reply for a request, or a pipeline's next
stages. A `Rejection`, an exception or too many Owns becomes a failure reply
to the task's `reply_topic` if it has one, and otherwise goes to the topic's
error topic for an operator. `run` receives a `lost` event that is set if the
lease is lost; long work should check it and stop.

The fetch stage of the [pipeline example](#example-use-cases), in
`crawl_stages.py`:

```python
from threading import Event

from tasklease.generated import protocol as p
from tasklease.worker import Rejection


class Fetch:
    def validate(self, task: p.Task) -> str | Rejection:
        url = task["data"].get("url")
        return url if isinstance(url, str) else Rejection("url must be text")

    def run(self, url: str, lost: Event) -> str:
        return fetch_and_store(url, lost)  # your code: returns an R2 blob key

    def reply(self, task: p.Task, blob: str) -> list[p.NewTask]:
        data = {"url": task["data"]["url"], "blob": blob}
        return [
            {"topic": "classify/content", "data": data},
            {"topic": "classify/security", "data": data},
        ]


def fetch() -> Fetch:
    return Fetch()
```

Run it with `topic = "check/url"` and `handler = "crawl_stages:fetch"` in the
worker's `[worker]` table, with the module importable by `tl` (for example
through `PYTHONPATH`). Its identity needs `own` on `check/url` and `create` on
`classify/*`; failures go to `check/url/error`, which `own` already grants.

### Calling the service from code

```python
from tasklease.client import Client
from tasklease.errors import Empty, NotFound

with Client(URL, token, "crawler-1") as client:
    client.create(
        {
            "domain": "crawl",
            "topic": "check/url",
            "data": {"url": "https://example.com"},
        }
    )

with Client(URL, worker_token, "fetcher-1") as client:
    try:
        task = client.own(
            {"domain": "crawl", "topic": "check/url", "hold_seconds": 600}
        )["task"]
    except Empty:
        task = None  # nothing to do; poll again later
    if task is not None:
        successor = {**task["data"], "blob": "<blob key>"}
        try:
            client.consume(
                {
                    "domain": "crawl",
                    "topic": "check/url",
                    "id": task["id"],
                    "tasks": [
                        {"topic": "classify/content", "data": successor},
                        {"topic": "classify/security", "data": successor},
                    ],
                }
            )
        except NotFound:
            pass  # the lease ran out and another worker took the task
```

Each error code maps to an exception in `tasklease.errors`, such as `Empty`,
`NotFound` and `Forbidden`. A worker written against the client renews long
leases itself; `tl worker` and `tasklease.worker.WorkerLoop` do it for you.

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

## Scale guidance

Each domain is one Durable Object with one database, so a domain's requests
and tasks share one object's capacity. Count tasks across all its topics,
including work, replies, and errors.

**Workers.** An idle worker polls every 30 seconds, so 1,000 idle workers
send about 33 requests a second to their domain. A load test against a
deployed service (below) handled 5,000 workers without errors, and a single
domain served about 1,000 requests a second, Cloudflare's documented soft
limit for one Durable Object: roughly 30,000 idle workers, less the requests
their actual work adds. An idle poll usually writes nothing, which keeps it
cheap. Workers with their own tokens added about 65 ms per request compared
with shared tokens: each Worker instance caches a token's identity for a
minute, and with many tokens most requests miss that cache and ask the
shared identity registry. Split independent workloads into separate domains to add
capacity.

**Tasks.** On a paid Workers plan, with task payloads around 1–16 KiB,
**10,000–100,000 outstanding tasks per domain** is a reasonable initial
planning range. This estimate comes from local SQLite benchmarks.

A million tasks can form a backlog, but calls that scan many held tasks or
count every task take longer and share the domain's capacity with other
requests. Measure those calls under load before adopting that scale. Keep
large outputs in R2 and budget task storage: one million 16 KiB payloads
alone exceed the paid plan's [10 GB storage limit per Durable Object](https://developers.cloudflare.com/durable-objects/platform/limits/).
Use separate domains for independent workloads; Consume operates within one
domain.

The [benchmark](scripts/bench_queue.py) reads the service's schema and SQL.
These p95 query times used 1 KiB payloads, one topic, 31 measured runs after
warmup, and a 64 MiB SQLite page cache on a local Linux machine with SQLite
3.37.2. The [1 KiB results](docs/benchmarks/queue-1k.json) and
[16 KiB results](docs/benchmarks/queue-16k.json) record the measurements and
method. Service latency and throughput require measurements in your deployment;
the benchmark excludes HTTP, authentication, concurrent requests, and
replicated durability.

| Tasks in the domain | Claim lookup after 32 held tasks | Poll with all tasks held | Topic counts |
| ---: | ---: | ---: | ---: |
| 10,000 | 0.015 ms | 0.58 ms | 1.2 ms |
| 100,000 | 0.007 ms | 5.3 ms | 11 ms |
| 1,000,000 | 0.007 ms | 70 ms | 121 ms |

Both benchmark programs are preserved under `scripts/`. Run the workload
benchmark against the current service schema, or compare index choices and
topic aggregation with [bench_queue_indexes.py](scripts/bench_queue_indexes.py):

```sh
python3 scripts/bench_queue.py --output /tmp/tasklease-queue.json
python3 scripts/bench_queue_indexes.py --output /tmp/tasklease-indexes.json
```

Each creates and removes temporary databases, then writes a JSON report.
Use `--sizes 10000 --samples 15` for a shorter run. The comparison program
also offers `--maintained-totals` to measure an experimental counter query;
that option excludes the cost of maintaining the counters.

### Load test

[load_pollers.py](scripts/load_pollers.py) simulates workers against a
deployed service: each worker polls one topic with Own at the real worker's
jittered interval, and a producer creates tasks that workers own, hold for
two seconds and consume. These runs used the acceptance deployment from one
Linux client on 2026-11-08 and 2026-11-09; latency is measured at the client and includes
about 45 ms of network path. The
[results](docs/benchmarks/load-acceptance.json) record each run.

| Simulated load | Requests/s | Own p50 / p95 / p99 | Errors |
| --- | ---: | ---: | ---: |
| 1,000 workers, 10 shared tokens, 30 s polls | 39 | 45 / 72 / 129 ms | 0 |
| 5,000 workers, own tokens, 30 s polls | 181 | 111 / 165 / 253 ms | 0 |
| 1,000 workers, 10 shared tokens, 3 s polls | 347 | 68 / 177 / 314 ms | 0 |
| 1,000 workers, 10 shared tokens, 0.5 s polls | 1,012 (of ~2,040 offered) | 480 / 775 / 1,038 ms | 0 |

The last row is the domain's ceiling: requests queued rather than failed,
and a probe on another topic of the same domain, timed from a separate
process, waited 117 ms at the median and 728 ms at p95. The first and third
rows predate the change that stopped idle polls from writing; then a domain
queued from about 550 requests a second, and the results file records both.

```sh
TL_ADMIN_TOKEN=... uv run --project python python scripts/load_pollers.py \
  --url https://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev --workers 1000 \
  --poll-seconds 30 --duration 240 --processes 2 --output /tmp/load.json
```

Run it only against a test deployment: requests are billed, and the domain
keeps the test's poller and statistics rows until they expire. One client
process sustains about 100 requests a second; use `--processes` for more.

## Reference and acceptance

- [Specification](docs/spec.md) and [implementation design](docs/design.md).
- [Phase checklist](docs/implementation-checklist.md) and [review evidence](docs/review-log.md).
- [Local release/native acceptance](docs/releasing.md) and [Windows agent prompt](docs/windows-agent-prompt.md).
- [Schema contract](schema/README.md), generated SDK DTOs and shared examples.

FluxShot-specific profiles and workers belong in the FluxShot repository:
[tasklease profiles](https://github.com/provos/fluxshoteditor/blob/master/docs/point-designs/tasklease-profiles.md).
