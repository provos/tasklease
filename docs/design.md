# tasklease implementation design

Draft 1, 2026-11-07. Implements [spec.md](spec.md). The FluxShot
conventions and workers live in the FluxShot repository
([tasklease-profiles.md](https://github.com/provos/fluxshoteditor/blob/master/docs/point-designs/tasklease-profiles.md)); "profiles" below refers to that document.

The spec says *what* the service and its clients do. This document says how
to build them: languages, layout, the shared contract, module boundaries,
configuration, deployment, testing and the order of work. Where the two
disagree, the spec wins and this document is fixed.

## 1. Components

| Component | Language | Runs on | Spec |
|---|---|---|---|
| Service: HTTP API, Durable Objects, blob URLs | TypeScript, Cloudflare Workers | Cloudflare | §4, §7, §8 |
| Protocol contract | JSON Schema (2020-12) | build and test time | §4 |
| `tasklease` Python package: client, `tl` CLI, worker runtime, monitor server | Python 3.12+, published on PyPI, installed with `uv` | macOS, Linux, Windows | §5, §6, §9 |
| Monitor dashboard | Svelte 5 and TypeScript, built to static files shipped inside the Python package | the user's browser | §4.15, §9 |
| FluxShot workers, integrations and profiles (verify, build, advise) | Python and docs, in the fluxshoteditor repo | the three machines | profiles |

tasklease is generic: nothing FluxShot-specific is in this repo's code. The
FluxShot workers and every other FluxShot integration depend on FluxShot's
lock lanes, `verify.py`, `run_bounded` and its test rules, so they live in
fluxshoteditor (under `tools/`, the top import layer) and get their own
design there. They use the worker runtime from §5.4 as a library, installed
from PyPI. The profiles document lives there as well.

## 2. Repository layout

```text
tasklease/
├── docs/                      spec.md, design.md
├── schema/                    the protocol contract (§3)
│   ├── common.json            names, IDs, timestamps, task, error
│   ├── ops/<operation>.json   one file per operation: request and response
│   └── examples/<operation>/  valid and invalid example bodies
├── service/                   the Cloudflare Worker (§4)
│   ├── wrangler.jsonc
│   ├── src/
│   │   ├── index.ts           router: auth, validation, scope check, dispatch
│   │   ├── auth.ts            token hashing, the 60 s token cache
│   │   ├── scopes.ts          glob matching (spec §3)
│   │   ├── domain_do.ts       DomainObject: tasks, pollers, declared topics
│   │   ├── registry_do.ts     RegistryObject: identities
│   │   ├── blobs.ts           presigned R2 URLs
│   │   ├── ids.ts             ULID, cursor encoding
│   │   ├── errors.ts          error codes → HTTP status
│   │   └── generated/         types and validators built from schema/
│   └── test/
├── monitor/                   the Svelte 5 dashboard (§5.7)
│   ├── package.json, vite.config.ts
│   └── src/
├── python/                    the tasklease package (§5)
│   ├── pyproject.toml
│   ├── hatch_build.py         builds monitor/ and the Worker bundle into the wheel (§5.8)
│   ├── src/tasklease/
│   │   ├── client.py          one typed method per operation
│   │   ├── errors.py
│   │   ├── fsio.py            publish_new and friends (spec §6.3)
│   │   ├── store.py           request records, reply store
│   │   ├── worker.py          the generic worker loop (spec §6.1)
│   │   ├── config.py          config files, paths, tokens
│   │   ├── cli/               `tl`: one module per command group
│   │   ├── monitor/           local dashboard server; static/ is build output, not committed
│   │   ├── cloudflare/        `tl service deploy` and `status`: Cloudflare API calls (§8)
│   │   ├── service_bundle/    the built Worker and its manifest; build output, not committed
│   │   └── generated/         TypedDicts built from schema/
│   └── tests/
├── conformance/               Python tests run against a live service (§7.3)
└── deploy/                    unit files for systemd, launchd, Windows (§8.5)
```

One repository, two toolchains. `npm` (or `pnpm`) under `service/` and
`monitor/`, `uv` under `python/`. Users never need Node: they install the
wheel from PyPI, which already contains the built dashboard. A top-level `Makefile` (or `justfile`) runs codegen, both
test suites and the conformance suite.

## 3. Protocol contract

`schema/` is the single source of truth for the wire format. Both sides are
generated from it and tested against it.

- **One file per operation.** Each defines `Request` and `Response`, and
  references `common.json` for shared types: `DomainName`, `TopicName`,
  `TaskId`, `Timestamp` (integer milliseconds), `Task`, `TaskSummary`,
  `Error`. Patterns and limits from the spec are in the schema: name
  patterns, 256-character labels, 256-entry lists, the 16-task Consume limit.
- **What the schema cannot say** (the 256 KiB serialized size of `data`, a
  topic's 128 characters across segments, the cursor's contents) is checked
  in code and listed in a comment in the schema file.
- **TypeScript:** types generated with `json-schema-to-typescript`, and
  request validators compiled ahead of time with Ajv's standalone mode
  (Workers do not allow `eval`, which Ajv uses at run time).
- **Monitor:** the same TypeScript generation produces the types for the
  `monitor.snapshot` response that the Svelte app reads, so the dashboard
  cannot drift from the API either.
- **Python:** `TypedDict`s generated with `datamodel-code-generator`
  (`--output-model-type typing.TypedDict`). Responses are checked against the
  schema in tests, not at run time.
- **Examples:** `schema/examples/<operation>/{valid,invalid}/*.json`. Both
  test suites validate every example, so a schema change that breaks either
  side fails both.
- **Drift check:** `make codegen` regenerates both `generated/` folders, and
  the test run fails if the result differs from what is committed.

## 4. Service

### 4.1 Request path

```text
fetch(request)
  → parse POST /v1/<operation>; unknown operation → invalid
  → admin operations (identity add/revoke/rotate/list): check the admin token
    other operations: resolve token → identity (auth.ts, cached 60 s)
  → validate the body against the operation's schema → invalid
  → check X-Worker label → invalid
  → dispatch by kind:
    identity.*  → RegistryObject
    blob.*      → check domain ∈ identity.domains → forbidden
                  put_url: non-empty create scope; get_url: non-empty read or
                  own scope, and the key's first segment is the encoded domain
                  → blobs.ts (R2 binding and presigning, no Durable Object)
    all others  → check domain ∈ identity.domains → forbidden
                  check the topic scope for the request's topic, and for
                  Consume each target topic → forbidden
                  → DomainObject (getByName(domain))
  → map the result to {ok: true, ...} or {ok: false, error} and the HTTP
    status (errors.ts)
```

**Admin token.** Workers' `crypto.subtle.timingSafeEqual` throws when its two
inputs differ in length. So `auth.ts` hashes the presented token and
`ADMIN_TOKEN` with SHA-256 and compares the two 32-byte digests.

**Who checks what.** The Worker does every check that needs only the request
and the identity. Every operation that names a task (Read, Delete, Renew,
Consume) also names its topic, so the Worker checks that topic's scope. The
Durable Object does every check that needs stored state:

- the stored task's `topic` must equal the request's topic, or the result is
  `not_found` (spec §4: "no task with this domain, topic and ID");
- the holder rule, lease state and existence;
- scope filtering of results whose topics only the object knows:
  `topics.list` and `monitor.snapshot`.

So the object needs the caller's scopes too. `scopes.ts` has no Worker-only
dependencies and is used on both sides. All checks run before anything is
written (spec §4).

Durable Object calls use RPC methods (`stub.own(...)`), not `fetch`. The
Worker passes the caller as `{identity, worker, operator, scopes}`; the
object trusts it, since only the Worker can reach the object.

### 4.2 DomainObject

One instance per domain, named by the domain (`getByName(domain)`). SQLite
storage, with the tables in spec §7.2.

- **Schema setup** in the constructor under `blockConcurrencyWhile`, with a
  `schema_version` row; migrations are plain SQL steps keyed by version.
- **Every operation is one `ctx.storage.transactionSync(() => …)`** with no
  `await` inside, so it is atomic and other requests wait for it.
- **Results, not exceptions.** If the function passed to `transactionSync`
  throws, the whole transaction is rolled back. So no operation throws to
  report an outcome: each returns a result union from inside the transaction
  (`{ok: true, …} | {ok: false, code}`), the transaction commits, and only
  then does the Worker turn the result into a response. This matters for
  Own: an `empty` result must still commit its poller row and its
  `empty_polls` count. A refusal (`forbidden`, `not_found`) commits a
  transaction that wrote nothing, which has the same effect as a rollback. An
  exception thrown inside means a bug, and the rollback is then the right
  outcome.
- **Time:** `Date.now()` is read once at the start of each operation and used
  throughout it. (In Workers the clock does not advance during synchronous
  execution anyway.)
- **Own:** select the first row of `tasks_pick` for the topic with
  `lease_until <= now`, then delete it and insert the copy with a new ID,
  `owned_count + 1`, the new owner, `owned_at = now` and
  `lease_until = now + hold`. Upsert the poller row in the same transaction,
  also on `empty`.
- **Statistics:** every operation that counts (spec §4.15) upserts its
  topic's row in `stats_hourly` inside its own transaction, with
  `INSERT … ON CONFLICT(topic, hour) DO UPDATE SET created = created + 1, …`.
  A helper takes the topic, `now` and the increments, so each operation adds
  one line.
- **Snapshot:** `monitor.snapshot` reads topics, pollers, the first 100 task
  summaries per topic and the requested hours of statistics in one
  transaction, so the page shows one consistent moment.
- **Renew:** `UPDATE … SET lease_until = ? WHERE id = ? AND topic = ?`, after
  the holder check.
- **Consume:** delete, then insert each new task, in one transaction.
- **Alarm:** once a day, delete poller rows older than 7 days and statistics
  rows older than 30 days, then set the next alarm. It is set on first use,
  so an idle domain has no alarm.
- **Sizes:** `data` is stored as JSON text, after the Worker checked its
  serialized size.

### 4.3 RegistryObject

One instance (`getByName("registry")`). Tables: `identities` (name, token
hash, domains, scopes as JSON, operator flag, created_at, last_seen_at).

- `lookup(token_hash)` returns the identity and sets `last_seen_at` in the
  same call (spec §7.1), so the Worker makes one call per cache miss.
- `add`, `revoke`, `rotate`, `list`, `whoami` as in spec §4.13–4.14. `add`
  generates the token (32 random bytes, base64url, prefixed `tl_`) and expands
  each `own` glob into a `create` grant on `<glob>/error`.

### 4.4 Token cache

A module-level `Map<hash, {identity, expires}>` in the Worker isolate, with
entries kept for 60 seconds. Only found identities are cached. Each isolate
has its own cache; that is why revocation is "within 60 seconds" rather than
exact.

### 4.5 Blob URLs

Presigned with `aws4fetch` against R2's S3 endpoint, using an R2 API token
(access key ID and secret) stored as Worker secrets. Before signing a GET,
the Worker checks the object exists through the R2 binding (`head`), so a
missing blob is `not_found`, not a URL that fails later.

**Size of uploads.** The PUT URL should sign `Content-Length`, so R2 rejects
an upload of any other size (`403 SignatureDoesNotMatch`). With `aws4fetch`,
query signing (`aws: {signQuery: true}`) is reported to sign only `host`
unless `allHeaders: true` is also set, so the configuration to try is:

```ts
await aws.sign(url, {
  method: "PUT",
  headers: { "Content-Length": String(size) },
  aws: { signQuery: true, allHeaders: true },
});
```

This is checked early in M1 (§9): an upload of the signed size succeeds and
one of a different size is refused. If R2 does not refuse it, the size limit
is enforced after upload instead: the reader checks size and SHA-256, and
oversize objects are left to the lifecycle rule.

### 4.6 Configuration

`service/wrangler.jsonc` is the one description of the Worker's
configuration. Development uses it directly (`wrangler dev`). For
production, the build turns it into the manifest that `tl service deploy`
uploads (§8.2), so the two never disagree.

- Durable Object bindings `DOMAINS` (DomainObject) and `REGISTRY`
  (RegistryObject), with a migration listing both under `new_sqlite_classes`.
- R2 bucket binding `BLOBS` (`tasklease-blobs` by default).
- Secrets: `ADMIN_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`; plain
  variable `R2_ACCOUNT_ID`.
- The API's address: the account's `workers.dev` subdomain, or a custom
  domain (for example `tasks.<your domain>`).
- Observability (Workers Logs) on. Log the operation, identity, domain, topic,
  result code and duration. Never log tokens or `data`.

The R2 bucket and its lifecycle rule (delete after 7 days) are created by
`tl service deploy` (§8.2).

The Worker also answers `GET /v1/version` without a token, with
`{service_version, protocol}`. `tl service status` and the client's version
check use it.

## 5. Python package

Python 3.12 or newer. Runtime dependencies kept small: `httpx` (HTTP),
`platformdirs` (per-OS paths). `tomllib` from the standard library for
configuration. Development: `pytest`, `mypy --strict`, `ruff`,
`jsonschema` (tests only), `datamodel-code-generator` (codegen only).

Published on PyPI as `tasklease` (the name is free as of 2026-11-07) and
installed on every machine with:

```bash
uv tool install tasklease
```

The fluxshoteditor repo depends on it like any other package, with a version
range in its `pyproject.toml`. A git checkout is only for developing
tasklease itself (§5.8).

### 5.1 `client.py`

`Client(base_url, token, worker_label)` with one method per operation, taking
and returning the generated `TypedDict`s. Errors are exceptions, one per
error code (`NotFound`, `Empty`, `Forbidden`, `Invalid`, `TooLarge`, …), plus
`TransportError` for timeouts, connection failures and 5xx.

The client never retries on its own. Whether a retry is safe depends on the
operation and on the lease (spec §5), so retrying is the caller's decision:
the worker runtime and the store do it, each following the spec's table.

### 5.2 `fsio.py`

The one place that knows how files are written on each OS (spec §6.3).

```python
def publish_new(path: Path, data: bytes) -> Literal["created", "exists"]: ...
def read_json(path: Path) -> object | None: ...
```

Both platforms write a temp file in the same directory and `fsync` it, then
publish it, inside `try/finally` so the temp file is removed whatever
happens. A `FileExistsError` from the publish step returns `"exists"`.

- **POSIX (macOS, Linux):** publish with `os.link(tmp, path)`, which fails if
  `path` exists, so exactly one publisher wins. Then `fsync` the directory.
- **Windows:** publish with `os.rename(tmp, path)`. On Windows `os.rename`
  refuses to replace an existing file, which is exactly the no-replace rule.
  `os.fsync` flushes on Windows. A directory cannot be fsynced there, so that
  step is skipped.
- Temp files are named `.<name>.<random>.tmp` and ignored by every reader.
  Only a crash leaves one behind; those are removed when older than a day.

The same tests run on all three OSes (§7.4), including a race test in which
several processes publish the same path and exactly one sees `created`.

### 5.3 `store.py`

The local request records and reply store from spec §6.3, under the
platform's user data folder (`platformdirs.user_data_path("tasklease")`):
`requests/<agent-id>/`, `replies/<agent-id>/`, `replies/<agent-id>/unmatched/`.
Every write goes through `publish_new`.

`drain(client, reply_topic)` runs the own → save → delete loop. `wait(...)`
checks the store, drains, then sleeps with jitter until a reply arrives or
the timeout passes.

**Request states** are read from the files, never stored:

| State | Files |
|---|---|
| pending | `requests/<agent>/<id>.json` and no `replies/<agent>/<id>.json` |
| answered | `replies/<agent>/<id>.json` exists |
| cancelled | the request record was deleted by `tl cancel` |

`tl wait` leaves the request record in place: the reply's existence already
says "answered". `tl cancel` deletes the record after a successful Delete.

**Pruning.** Request records, replies and unmatched replies older than 7 days
are removed, the same retention as the blobs replies point to. `tl inbox
--prune` does it on demand, and `drain` does it at most once a day.

**All agents on the machine.** Several agent sessions on one machine have
different agent IDs and so different folders. `tl inbox --all` and
`tl monitor` read every agent folder under `requests/` and `replies/`.

### 5.4 `worker.py`

The generic worker loop from spec §6.1, as a library. A profile supplies two
objects:

```python
class Capacity(Protocol):
    def acquire(self) -> AbstractContextManager[None]: ...

class Handler(Protocol):
    def validate(self, task: Task) -> Request | Rejection: ...
    def run(self, request: Request, lost: Event) -> Outcome: ...
    def reply(self, task: Task, outcome: Outcome) -> list[NewTask]: ...
```

`WorkerLoop(client, topic, capacity, handler, lease, renew_every,
max_owned).run()` owns the rest: polling with jitter, the renewer thread,
the `lost` event, `finish` with the fallback to `<topic>/error`, and the
`too_large` truncation.

`WorkerLoop.step() -> Literal["worked", "empty"]` is the single-step entry
point: it owns at most one task, finishes it and returns. After `"worked"`
the caller polls again at once (there may be a backlog); after `"empty"` it
sleeps for the poll interval with jitter. `run()` is that loop with
`Capacity` taken around each step.

For FluxShot, the profiles run each step under `tests.verify_lock run`
(profiles §3.2), so the outer loop lives outside the lock and calls
`step()` in a short-lived process. At a 30–120 second poll interval, one
Python start and TLS handshake per poll is cheap. Taking the lock in-process
instead, as a `Capacity`, would avoid it; whether that fits FluxShot's import
layers and the lock's held-variable rule for child processes is a question
for the FluxShot worker design. The library supports both shapes.

**Stopping.** A worker stops the same way on every OS: a stop request finishes
the current task, then exits. A stop request is `SIGHUP` or `SIGTERM` on
POSIX, or a stop file (`<state dir>/workers/<label>.stop`) on any OS, which
is also what the Windows service wrapper uses. A second signal stops at once,
and the task's lease brings it back for another worker.

### 5.5 `tl`

`argparse`, one module per command group: `request`, `wait`, `cancel`,
`inbox`, `topics`, `tasks`, `read`, `delete`, `pollers`, `prune`, `move`,
`identity`, `topic`, `whoami`, `monitor`, `service` (§8), and `worker` (runs a generic worker from a
config file, for profiles that need no custom code). Output is JSON on
stdout for anything a program reads, and text on stderr for people. Exit
codes as in spec §6.3.

`tl monitor` is described in §5.7.

### 5.6 Configuration and tokens

`config.toml` in the platform's user config folder
(`platformdirs.user_config_path("tasklease")`):

```toml
url = "https://tasks.example.com"
domain = "github.com/provos/fluxshoteditor"   # default for --domain
agent_id = "mac-a1b2"                          # or TL_AGENT_ID
worker_label = "cli"                           # or TL_WORKER

[token]
file = "token"                                 # relative to the config folder
```

The token lives in its own file, created with mode `0600` on POSIX. On
Windows the user's profile folder is already private to the user. `TL_TOKEN`
and `TL_ADMIN_TOKEN` override the files. `tl service deploy` prints the admin
token once (§8.2). The admin token is never written to
disk by `tl`.

### 5.7 Monitor

**Server** (`tasklease/monitor/server.py`): the standard library's
`http.server` (`ThreadingHTTPServer`), bound to `127.0.0.1` on a free port
unless `--port` is given. It serves:

- `/` and the static files from `tasklease/monitor/static/`, found with
  `importlib.resources`;
- `/api/snapshot`: `monitor.snapshot` for each of the caller's domains (from
  `identity.whoami`), plus this machine's pending requests and inbox from
  the local store, for every agent folder on the machine (§5.3). The result
  is kept for 25 seconds.

It refuses any request whose `Host` header is not `127.0.0.1:<port>` or
`localhost:<port>` (403). That keeps a web page from another site from
reading the snapshot by pointing its own domain at `127.0.0.1` (DNS
rebinding). Everything is `GET` and read-only. Nothing from the browser
reaches the service except the choice of `hours`.

**Dashboard** (`monitor/`): Svelte 5 with Vite, built to static files (plain
Vite, no SvelteKit, since there is one page and no routing). It fetches
`/api/snapshot` every 30 seconds and shows the time of the last refresh,
and a stale warning when a refresh fails. Sections:

- **Workers:** pollers per topic with their last poll, so a dead machine is
  obvious.
- **Queues:** per topic, available and held tasks, the oldest available
  task's age, and the tasks themselves (owner, lease, `owned_count`).
- **Throughput and latency:** per topic and hour, created / consumed,
  average and maximum wait and run times, re-owns and empty polls. Charts are
  small inline SVG, with no chart library.
- **This machine:** pending requests and inbox, including unmatched replies.

### 5.8 Packaging and releases

- **Build backend:** `hatchling`, with a custom build hook
  (`hatch_build.py`) that runs `npm ci && npm run build` in `../monitor/` and
  copies the output to `tasklease/monitor/static/`. Both the wheel and the
  sdist contain the built dashboard, so installing either never needs Node.
- **The Worker bundle.** The same hook runs `wrangler deploy --dry-run
  --outdir` in `../service/`, which bundles the Worker into one JavaScript
  module without deploying it, and writes `manifest.json` next to it: the
  compatibility date, bindings and Durable Object migrations taken from
  `wrangler.jsonc`, and the package version. Both go to
  `tasklease/service_bundle/`. So a user who installed from PyPI can deploy
  the service with no Node and no checkout (§8.2), and the service they
  deploy is always the one that matches their client.
- **Building from an sdist.** `uv build` builds the sdist first, then builds
  the wheel from the unpacked sdist, where `../monitor/` does not exist. So
  the hook builds only when `../monitor/` (and `../service/`) are present.
  When they are absent and the built files already exist (they came in the
  sdist), the hook does nothing. When both are missing, the build fails with
  a clear message rather than shipping a wheel without the dashboard. The
  generated Python types (`tasklease/generated/`) are committed (§3), so they
  need no build step.
- **Development:** `uv sync` in `python/` gives an editable install. The
  build hook, or `make monitor`, builds the dashboard; until then `tl
  monitor` serves a page saying the dashboard was not built.
- **Versions:** `MAJOR.MINOR.PATCH` in `pyproject.toml`, with the protocol
  version (`/v1/`) independent of it. A release that needs a service change
  says so in the changelog, and the service is deployed first.
- **Releasing:** `make release` runs the checks, `uv build`, and
  `uv publish` with a project-scoped PyPI API token kept on the release
  machine. No CI job is needed; trusted publishing can be added if a cheap
  Linux-only release workflow is ever wanted.
- **Public code:** a PyPI release makes the package's source public, even
  while the GitHub repository stays private. Nothing secret belongs in the
  package; tokens and URLs come from configuration.

## 6. Code quality

- **TypeScript:** `strict`, ESLint, Prettier. No `any`. `svelte-check` for
  the dashboard.
- **Python:** `mypy --strict`, `ruff check`, `ruff format`. No `Any` outside
  generated code.
- Both run in `make check`.

## 7. Testing

### 7.1 Service

Cloudflare's Vitest integration runs tests inside the Workers runtime against
real Durable Objects and SQLite:

- one file per operation: success, every error code, every limit;
- the holder rule and fencing: owned, re-owned after lease expiry, Renew and
  Consume with a stale ID;
- **atomicity under concurrency:** many Owns at once on a topic with N tasks
  give exactly N successes and no task twice; Consume racing Own after lease
  expiry has exactly one winner;
- glob matching, as a table of (glob, topic, expected) cases taken from the
  spec;
- the token cache: revocation takes effect within 60 seconds (with a
  controlled clock), `last_seen_at` updates on lookup;
- cursor paging while tasks are being owned and consumed;
- the poller alarm, with the integration's alarm helper rather than waiting;
- statistics: each counted operation moves the right counters, and wait and
  run times use `created_at` and `owned_at`;
- `monitor.snapshot` filters topics by scope and caps tasks at 100.

### 7.2 Deploy

`tl service deploy` and `status` are tested against a fake Cloudflare API
(an `httpx` mock transport) for every step's created / updated / unchanged
path and every stop. A real deploy of a throwaway copy (`--name
tasklease-test`, its own bucket) runs before each release.

### 7.3 Conformance

`conformance/` is a pytest suite that runs the Python client against a live
service (`wrangler dev` locally, or a staging deployment): every schema
example, a full request → own → renew → consume → drain → wait flow, and
the lost-reply cases from spec §5 using an HTTP transport that drops chosen
responses. This is the test that proves the TypeScript server and the Python
client agree.

### 7.4 Python

Unit tests for the store, the worker loop (with a fake client), the CLI and
the monitor server (the `Host` check, the 25-second reuse, and a missing
dashboard build). `fsio` and the store run on all three OSes. The dashboard
gets `svelte-check` and a few component tests (Vitest) that render a
snapshot fixture taken from `schema/examples/`.

GitHub Actions is too expensive to use here, so the test runs are split:

- `make check` on any machine runs the service tests, the Python tests and
  the conformance suite against `wrangler dev`.
- The macOS and Windows runs of the Python tests are run by hand on those
  machines until M4. From then on they are requested through tasklease
  itself, as a `test/<os>` topic.

## 8. Deployment

### 8.1 Environments

`dev` is `wrangler dev` in a checkout, with local Durable Objects and R2.
`prod` is the deployed Worker, deployed with `tl service deploy` from any
machine where tasklease is installed. A `staging` environment is not needed
for one operator: deploy a second copy under another `--name` when one is
wanted.

### 8.2 `tl service deploy`

Deploys or upgrades the service in the operator's own Cloudflare account,
using Cloudflare's API directly. It needs no Node, no `wrangler` and no
checkout: the Worker comes from the installed package (§5.8). Every step
checks the current state first, so running it again is safe, and each step
reports `created`, `updated` or `unchanged`.

**Inputs.**

| Input | Flag | Environment | Saved in `config.toml` |
|---|---|---|---|
| Cloudflare account ID | `--account-id` | `CLOUDFLARE_ACCOUNT_ID` | yes |
| Cloudflare API token | prompted | `CLOUDFLARE_API_TOKEN` | **never** |
| Worker name | `--name` (default `tasklease`) | | yes |
| R2 bucket name | `--bucket` (default `tasklease-blobs`) | | yes |
| Custom hostname (optional) | `--hostname` | | yes |
| R2 Access Key ID | `--r2-access-key-id` | `TL_R2_ACCESS_KEY_ID` | no (Worker secret) |
| R2 Secret Access Key | prompted | `TL_R2_SECRET_ACCESS_KEY` | no (Worker secret) |

Later runs reuse the saved values, so an upgrade is
`uv tool upgrade tasklease && tl service deploy` plus the API token.

**Steps.**

1. **Check the API token** (`/user/tokens/verify`) and that it can reach the
   account. A missing permission is reported by name, with the line from the
   help text that explains it.
2. **R2 bucket:** create it if missing. Set the lifecycle rule
   `tasklease-expire` (delete objects after 7 days), replacing an older rule
   of that name.
3. **R2 credentials:** on the first deploy they are required. If they were
   not given, stop here with exit code 4 and print the dashboard steps for
   creating them (help text, step 3), naming the bucket just created. Run the
   command again once they exist. On later deploys they are optional: the
   Worker keeps its existing ones unless new ones are given.
4. **Admin token:** on the first deploy, generate one (32 random bytes,
   `tla_` prefix) and keep it to print at the end. On later deploys keep the
   existing secret, unless `--rotate-admin-token` asks for a new one. That
   is also how to recover a lost admin token.
5. **Upload the Worker** through the Workers script-upload API: the bundled
   module, the compatibility date, the bindings (`DOMAINS`, `REGISTRY`,
   `BLOBS`, `R2_ACCOUNT_ID`) and observability settings from the manifest,
   and new secret values if any. Existing secrets are kept with the
   upload's `keep_bindings` option. Durable Object migrations are applied
   from the manifest, starting after the migration tag the deployed script
   already has.
6. **Address:** attach `--hostname` as a Workers custom domain, or else turn
   on the script's `workers.dev` address.
7. **Check it works:** `GET /v1/version` on the new address must return the
   package's version.
8. **Write `config.toml`** with the URL and the saved inputs. Print the admin
   token if one was generated, once, with a warning that it is not stored
   anywhere and is needed for `tl identity add`. Then print the next steps:
   `tl identity add` for each machine, and `tl topic declare`.

`--dry-run` runs step 1, then prints what each later step would do and
changes nothing.

### 8.3 `tl service status`

Shows, for the configured service: the deployed Worker's version (from
`/v1/version`) against the installed package's, whether an upgrade is
needed, and, with an API token, the bucket, its lifecycle rule, the
Durable Object migration tag and the address. It changes nothing.

There is no `tl service destroy`: deleting the Worker deletes every
domain's Durable Object data. That is done by hand in the dashboard.

### 8.4 Help text

`tl service deploy --help` explains how to get every input, in the order
needed. This is its text:

```text
usage: tl service deploy [--account-id ID] [--name NAME] [--bucket NAME]
                         [--hostname HOST] [--r2-access-key-id ID]
                         [--rotate-admin-token] [--dry-run]

Deploy or upgrade the tasklease service in your Cloudflare account.
Safe to run again: each step checks what already exists.

You need a Cloudflare account with Workers and R2 enabled. R2 asks for a
payment method before first use, even on the free tier.

Before the first deploy, collect three things from the Cloudflare
dashboard (https://dash.cloudflare.com):

1. ACCOUNT ID  (--account-id, or CLOUDFLARE_ACCOUNT_ID)
   Open "Workers & Pages". Under "Account Details", select the copy
   button next to "Account ID". (Or press Ctrl+K / Cmd+K on the account
   home page and search for "Copy account ID".)

2. API TOKEN  (asked for at the prompt, or CLOUDFLARE_API_TOKEN)
   Used only while this command runs; never saved.
   Open "My Profile" > "API Tokens" > "Create Token". Choose
   "Create Custom Token" ("Get started"), give it a name such as
   "tasklease deploy", and add these permissions:
     Account | Workers Scripts    | Edit
     Account | Workers R2 Storage | Edit
   Only if you use --hostname, also add, for the zone that holds the
   hostname:
     Zone    | Workers Routes     | Edit
     Zone    | Zone               | Read
   Under "Account Resources", include your account. Select "Continue to
   summary", then "Create Token", and copy the token: it is shown once.

3. R2 ACCESS KEYS  (--r2-access-key-id and a prompt, or
   TL_R2_ACCESS_KEY_ID and TL_R2_SECRET_ACCESS_KEY)
   The service uses these to give machines upload and download links.
   The bucket must exist first, so on the very first deploy run this
   command without them: it creates the bucket, then stops and repeats
   these steps. Then:
   Open "R2 Object Storage" > "Overview". Under "Account Details", select
   "Manage" next to "API Tokens", then "Create Account API token".
   Choose the permission "Object Read & Write", and under bucket scope
   choose "Apply to specific buckets only" with the tasklease bucket
   (default "tasklease-blobs"). Create it and copy both the "Access Key
   ID" and the "Secret Access Key": the secret is shown once.

Without --hostname, the service gets an address under your account's
workers.dev subdomain. If you have never opened "Workers & Pages", open
it once so Cloudflare creates that subdomain.

After the first deploy, this command prints the ADMIN TOKEN once. Store
it in a password manager: it is needed for "tl identity add" (as
TL_ADMIN_TOKEN) and is not saved anywhere. If it is lost, run
"tl service deploy --rotate-admin-token" to replace it.

Exit codes: 0 deployed; 4 more input needed (the message says which);
1 failed (the message says which step and why).
```

The permission names and dashboard labels in this text were checked against
Cloudflare's documentation on 2026-11-07. Two points are confirmed in M1 by a
real first deploy: whether the custom-domain permissions above are exactly
the ones needed, and whether `keep_bindings` keeps secrets across uploads.
The help text is updated if either differs.

### 8.5 Running workers

`deploy/` holds templates; `tl worker install` fills them in later:

- **Linux:** a systemd user unit (`Restart=on-failure`, `KillSignal=SIGHUP`
  for a graceful stop).
- **macOS:** a launchd agent plist (`KeepAlive`).
- **Windows:** a scheduled task that runs at logon and restarts on failure.
  It stops a worker by writing its stop file (§5.4).

Workers run as the logged-in user, not as a system service, because they need
the user's checkouts, `uv`, `gh` credentials and footage.

## 9. Milestones

| | Delivers | Done when |
|---|---|---|
| M1 | Service with every operation except `monitor.snapshot`; schema and codegen; `tl` operator commands (`identity`, `topics`, `tasks`, `read`, `delete`, `pollers`, `prune`, `move`, `topic`, `whoami`); `tl service deploy` and `status`; packaging and a first PyPI release | service tests and conformance pass; `uv tool install tasklease` works on all three machines; the production service was deployed from a PyPI install with `tl service deploy`, following only its help text; identities issued for the three machines |
| M2 | `fsio`, `store`, `tl request`, `wait`, `cancel`, `inbox` | a request posted from one machine is answered by hand (`tl` as a fake worker) from another and delivered; `fsio` passes on all three OSes |
| M3 | `worker.py`, `tl worker`; statistics, `monitor.snapshot` and `tl monitor` | a generic echo worker on each machine answers requests; the lost-reply cases pass in conformance; the dashboard shows all three machines polling and their tasks moving |
| M4 | FluxShot verify worker on Linux (in fluxshoteditor, with its own design) | a Claude session on the Mac verifies a PR on Linux and gets the result back |
| M5 | Verify on Windows and macOS (needs the Windows lock); build workers; the adviser | packaging and advice work across all three machines |

## 10. Open questions

Settled: the cursor format is spec §4.8's base64url of
`v1:<created_at>:<id>`, used as an SQLite keyset
(`WHERE (created_at, id) > (?, ?) ORDER BY created_at, id`), which does not
need the cursor's own task to still exist.

1. **Content-Length in presigned PUTs** (§4.5): the `allHeaders: true`
   configuration is to be checked in M1.
2. **Deploy details** (§8.4): the exact permissions for a custom domain, and
   whether `keep_bindings` keeps secrets across uploads, are confirmed by the
   first real deploy in M1.
3. **A `tl worker install` command**, or are the templates in `deploy/`
   enough?
4. **Cloudflare's own metrics** (requests, errors, Durable Object running
   time, cost) on the dashboard. They need a Cloudflare API token on the
   machine running `tl monitor`; left out of the first version.
