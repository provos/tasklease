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
│   │   ├── cloudflare/        `tl service deploy` and `status`: runs a pinned wrangler (§8)
│   │   ├── service_bundle/    the built Worker and its manifest; build output, not committed
│   │   └── generated/         TypedDicts built from schema/
│   └── tests/
├── conformance/               Python tests run against a live service (§7.3)
└── deploy/                    unit files for systemd, launchd, Windows (§8.5)
```

One repository, two toolchains. `npm` (or `pnpm`) under `service/` and
`monitor/`, `uv` under `python/`. Agents and workers never need Node: they
install the wheel from PyPI, which already contains the built dashboard and
Worker. Only the one machine that deploys the service needs Node, to run
wrangler (§8.2). A top-level `Makefile` (or `justfile`) runs codegen, both
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
  Own: an `empty` result must still commit its poller row when that row is
  due a refresh. A refusal (`forbidden`, `not_found`) commits a
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
  also on `empty`, but on `empty` only when its `last_polled_at` is at least
  a minute old (spec §4.9). An empty poll then usually writes nothing, and a
  request that writes nothing does not wait for a storage commit: idle
  polling, the bulk of a domain's traffic, stays cheap. Empty polls are not
  counted in the statistics for the same reason. The index orders by `(topic, created_at, id)` and includes
  `lease_until` so lease filtering can use index entries before fetching a
  task's payload. Topic aggregates use the same index and supply the live
  topic names for both listing and monitor snapshots.
- **Long-poll Own:** the Worker drives the wait. Each Domain Object call
  registers an in-memory waiter for the topic, makes one claim attempt, and
  if that is empty parks until a hint or the remaining wait ends. A parked
  call never claims: a hint answers `retry`, and the Worker makes a fresh
  call, so every claim happens at the start of a call. A client that
  disconnects cancels its Worker, which then never makes that call, so no
  task is leased to a caller that is gone. Create and Consume hint one waiter
  per newly available task after their transaction commits. One timer per
  topic, armed for the next lease expiry (Own and Renew move it earlier when
  they write a sooner lease), hints one waiter per task that became available,
  so an expiry does not wake every waiter. A woken waiter always gets one more
  attempt, even at its deadline. Registration before the attempt closes the
  gap between an empty attempt and the waiter being visible, and a hint the
  waiter did not use passes to the next waiter. The hint is never consulted
  for availability, so a race costs one extra call and cannot double-claim.
  Waiters hold no task state and are capped per topic; a full topic still
  makes the claim attempt (and records the poll) but answers at once. An
  evicted object drops its waiters and the client retries. Nothing is
  persisted, so no schema migration or alarm change is needed.
- **Statistics:** every operation that counts (spec §4.15) upserts its
  topic's row in `stats_hourly` inside its own transaction, with
  `INSERT … ON CONFLICT(topic, hour) DO UPDATE SET created = created + 1, …`.
  A helper takes the topic, `now` and the increments, so each operation adds
  one line.
- **Snapshot:** `monitor.snapshot` reads topics, pollers, the first 100 task
  summaries per topic and the requested hours of statistics in one
  transaction, so the page shows one consistent moment. It serializes bounded
  chunks into temporary SQLite snapshot tables during that transaction, then
  returns an RPC byte-stream Response over the immutable chunks. This avoids
  both the 32 MiB RPC value limit and live SQL cursors across awaits (which
  have no snapshot isolation). At most two snapshots can stream concurrently;
  excess requests receive HTTP 503. Completion and cancellation clean up
  chunks; a five-minute expiry and the alarm clean up abandoned snapshots.
- **Renew:** `UPDATE … SET lease_until = ? WHERE id = ? AND topic = ?`, after
  the holder check.
- **Delete:** holder and optional `lease_before` cutoff checks precede deletion
  in the same transaction, so pruning cannot race a Renew that retains the ID.
- **Consume:** delete, then insert each new task, in one transaction.
- **Alarm:** once a day, delete poller rows older than 7 days and statistics
  rows older than 30 days, then set the next alarm. It is set on first use,
  so an idle domain has no alarm. The alarm always reschedules itself, so
  each in-memory object checks for it only on its first operation instead
  of awaiting a storage read on every request.
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
- `add` rejects an expanded `<glob>/error` grant longer than 256 characters
  before insertion (spec §3). With every glob capped, a legal identity
  serializes to at most about 265 KB, far below SQLite's row limit.
- Identity List uses internal RPC pages of 64 records, at most about 17 MB,
  below RPC's 32 MiB message limit. The Worker streams
  the same public `{ok: true, identities: [...]}` JSON response with
  backpressure and cancellation. Neither side assembles the complete list,
  so individually valid identities cannot overflow RPC's message limit when
  listed together. These cursors are internal, not part of the wire protocol.
  Concurrent registry changes can appear between pages; each page is read in
  one transaction, and the overall list is a best-effort view.

### 4.4 Token cache

A module-level `Map<hash, {identity, expires}>` in the Worker isolate, with
entries kept for 60 seconds. Only found identities are cached. Each isolate
has its own cache; that is why revocation is "within 60 seconds" rather than
exact.

The cache also has an 8 MiB conservative retained-byte budget and a 4096-entry
cap. Charges include UTF-16 string lengths and container overhead; a legal
identity charges under 1 MB. An entry over the whole budget would still
authenticate without being cached; expired and evicted entries release their
charges, so cached identities cannot exhaust the isolate.

### 4.5 Blob URLs

Presigned with `aws4fetch` against R2's S3 endpoint, using an R2 API token
(access key ID and secret) stored as Worker secrets. Before signing a GET,
the Worker checks the object exists through the R2 binding (`head`), so a
missing blob is `not_found`, not a URL that fails later.

Domain-key encoding follows spec §4.10. Encode each key segment again for
the S3 URL: a literal `%2F` in the key becomes `%252F` on the wire. The domain
names `.` and `..` are prohibited (spec §2).

**Size of uploads.** The PUT URL signs `Content-Length`, so R2 rejects
an upload of any other size (`403 SignatureDoesNotMatch`). With `aws4fetch`,
query signing (`aws: {signQuery: true}`) signs only `host` unless
`allHeaders: true` is also set. The validated configuration is:

```ts
await aws.sign(url, {
  method: "PUT",
  headers: { "Content-Length": String(size) },
  aws: { signQuery: true, allHeaders: true },
});
```

Live acceptance on 2026-11-07 confirmed that 16 signed bytes upload and
download correctly, while 17 bytes on the same signed URL return 403 and
create no object. Repeat this check before release. Readers also check size
and SHA-256 for integrity.

### 4.6 Configuration

`service/wrangler.jsonc` is the one description of the Worker's
configuration. Development uses it directly (`wrangler dev`). For
production, the build turns it into the manifest that `tl service deploy`
uploads (§8.2), so the two never disagree.

- Durable Object bindings `DOMAINS` (DomainObject) and `REGISTRY`
  (RegistryObject), with a migration listing both under `new_sqlite_classes`.
- R2 bucket binding `BLOBS` (`tasklease-blobs` by default).
- Secrets: `ADMIN_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Plain
  variables `R2_ACCOUNT_ID` and `R2_BUCKET_NAME`. An R2 binding cannot tell
  the Worker its bucket's name, and presigning needs the name in the S3 URL
  (`https://<account>.r2.cloudflarestorage.com/<bucket>/<key>`), so `blobs.ts`
  reads it from `R2_BUCKET_NAME` and never hard-codes it. Otherwise a deploy
  with another `--bucket` would check one bucket and sign URLs for another.
- The API's address: the account's `workers.dev` subdomain
  (`https://<name>.<account subdomain>.workers.dev`). The service is an
  internal tool, so version 1 has no custom domains.
- Observability (Workers Logs) on. Log the operation, identity, domain, topic,
  result code and duration. Never log tokens or `data`.

The R2 bucket and its lifecycle rule (delete after 7 days) are created by
`tl service deploy` (§8.2).

The Worker also answers `GET /v1/version` without a token, with
`{service_version, protocol}`. `tl service status` and the client's version
check use it.

## 5. Python package

Python 3.12 or newer. Runtime dependencies kept small: `httpx` (HTTP),
`platformdirs` (per-OS paths), `tomli-w` (preserving TOML updates). `tomllib`
from the standard library for
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
  `path` exists, so exactly one publisher wins. Sync the destination directory
  and file on both `created` and `exists`: a loser may observe the name before
  the winner has flushed it. Create private parents one at a time and sync
  ancestor entries, including concurrent creation. On macOS request
  `F_FULLFSYNC` after publication to flush the drive cache too.
- **Windows:** a small stdlib `ctypes` wrapper calls `MoveFileExW` with
  `MOVEFILE_WRITE_THROUGH`, without replacement or cross-volume copy flags.
  Flush the destination file on both `created` and `exists`; Windows file
  flushes include cached file metadata. There is no equivalent straightforward
  directory barrier, so ancestor-namespace power-loss persistence is not
  claimed. Native Windows and macOS execution are separate acceptance checks.
- **Configuration:** `atomic_replace` reuses the same temp/sync/cleanup core,
  with replacement explicitly enabled. The configuration module contains no
  platform-specific file publication code.
- Temp files are named `.<name>.<random>.tmp` and ignored by every reader.
  Only a crash leaves one behind; those are removed when older than a day.

The same tests run on all three OSes (§7.4), including a race test in which
several processes publish the same path and exactly one sees `created`.

### 5.3 `store.py`

The local request records and reply store from spec §6.3, under the
platform's user data folder (`platformdirs.user_data_path("tasklease")`):
`requests/h(agent)/`, `replies/h(agent)/`, `replies/h(agent)/unmatched/`,
where `h` is the hash from spec §6.3. Every write goes through
`publish_new`. Each folder also holds an `agent.json` with the original
agent ID, so `tl inbox --all` and `tl monitor` can show it.

`drain(client, reply_topic)` runs the own → save → delete loop. `wait(...)`
checks the store, drains, then sleeps with jitter until a reply arrives or
the timeout passes.

**Request states** are read from the files, never stored:

Names are hashed (spec §6.3): `A = h(agent_id)`, `R = h(request_id)`.

| State | Files |
|---|---|
| submission unknown | `requests/A/R.json`, no `requests/A/R.task.json`, no `replies/A/R.json` |
| pending | `requests/A/R.json` and `R.task.json`, no `replies/A/R.json` |
| answered | `replies/A/R.json` exists |
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

The generic worker loop from spec §6.1, as a library. The library refuses
topics ending in `/error` before polling, just as
`tl worker` does (§5.5). Operators handle error topics manually; a
non-operator doing so needs explicit create grants for its Consume targets.
A profile supplies two objects:

```python
class Capacity(Protocol):
    def acquire(self) -> AbstractContextManager[None]: ...

class Handler(Protocol):
    def validate(self, task: Task) -> Request | Rejection: ...
    def run(self, request: Request, lost: Event) -> Outcome: ...
    def reply(self, task: Task, outcome: Outcome) -> list[NewTask]: ...
```

`WorkerLoop(client, domain, topic, capacity, handler, lease, renew_every,
max_owned).run()` owns the rest: polling with jitter, the renewer thread,
the `lost` event, `finish` with the fallback to `<topic>/error`, and the
`too_large` truncation.

`WorkerLoop.step() -> Literal["worked", "empty"]` is the single-step entry
point: it owns at most one task, finishes it and returns. After `"worked"`
the caller polls again at once (there may be a backlog). After `"empty"` it
sleeps for the rest of the poll interval, with jitter: the time the service
held the empty poll counts toward the interval. With the default 30-second
wait and interval it polls again at once; a 600-second interval keeps its
cadence (a 30-second wait, then about 570 seconds of sleep); an immediate
empty (wait disabled or refused) sleeps the whole interval. `step()` records
the empty poll's duration privately for `run()`. `run()` is that
loop with `Capacity` taken around each step. Because that capacity is held
across Own, `run()` long-polls only with `UnlimitedCapacity`; a real capacity
polls at once and sleeps outside it, so an idle wait never keeps a lock from
other work. `step(wait_seconds=...)` overrides the wait for one call. `step()`
sends `wait_seconds` unless it is zero, where the wire request stays
byte-for-byte the pre-long-poll shape. The local lease still starts at the
send time, because the service may claim at any point during the wait; the
wait is therefore capped at half the gap between `renew_every` and the lease,
so the first renewal comes well before that conservative deadline. The client raises
only the read-phase timeout, to the wait plus a response margin and still
within the caller's per-call cap when one is supplied, so an overall
deadline still bounds the poll.

For FluxShot, the profiles run each step under `tests.verify_lock run`
(profiles §3.2), so the outer loop lives outside the lock and calls
`step()` in a short-lived process. `tl worker --once` therefore polls at once
unless a wait is configured explicitly: a long poll there would hold the
verify lock through the idle wait. Taking the lock in-process
instead, as a `Capacity`, would avoid it; whether that fits FluxShot's import
layers and the lock's held-variable rule for child processes is a question
for the FluxShot worker design. The library supports both shapes.

**Stopping.** A worker stops the same way on every OS: a stop request finishes
the current task, then exits. A stop request is `SIGHUP` or `SIGTERM` on
POSIX, or a stop file (`<state dir>/workers/<h(label)>.stop`) on any OS, which
is also what the Windows service wrapper uses. A second signal stops at once,
and the task's lease brings it back for another worker.
Library callers can pass `on_second_signal=cleanup` to `WorkerControl` to end a
running child command before forced exit. The callback takes no arguments and
runs at most once in the signal handler. It must finish quickly without waiting
for work; an exception still forces exit, and another signal during cleanup
exits immediately without invoking the callback again.
The hash uses the store's first 32 lowercase hexadecimal SHA-256 characters,
so case-sensitive labels, Windows device names and long labels are portable.
`tl worker stop --worker <label>` creates the file without needing credentials.
A stop requested while an Own long poll is in flight takes effect when that
poll returns: the wait is bounded by `wait_seconds` (at most 30 seconds). A
task claimed by that poll counts as current work and is finished before exit.
Use `wait_seconds = 0` where an immediate stop matters more than pickup
latency.
The empty marker is transient: exclusive creation publishes the entire signal,
and the watcher removes it. It does not use immutable durable publication,
because removal can race a publisher's final flush. On Windows a sharing
violation while the creating handle is still open is retried by the watcher.

### 5.5 `tl`

`tl worker` refuses to run on a topic ending in `/error`: error topics are
for people, and a worker there would only move tasks back into the same
topic (spec §2).

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
url = "https://tasklease.example.workers.dev"
domain = "github.com/provos/fluxshoteditor"   # default for --domain
agent_id = "mac-a1b2"                          # or TL_AGENT_ID
worker_label = "cli"                           # or TL_WORKER

[token]
file = "token"                                 # relative to the config folder
```

The token lives in its own file. POSIX reads check the opened file's permission
bits and reject any group or other access; use mode `0600` or owner-read-only.
On Windows, use a user-restricted ACL rather than POSIX mode bits. Token paths
may be absolute, expand `~`, or resolve relative to the config directory.
`TL_TOKEN` overrides the identity token file. `TL_ADMIN_TOKEN` is the separate,
environment-only credential for identity administration and service deployment;
ordinary topic/task commands use identity tokens. `tl service deploy` prints a
new admin token once (§8.2), never writing it to disk. See the README's
[client configuration](../README.md#client-configuration) for concrete platform
paths and the [operator setup](../README.md#operator-setup) for the first identity.

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

`tl monitor --domain D` fixes service snapshots to that permitted identity
domain. Without the flag it shows every permitted domain, independently of
the configuration's default domain. Local records still span this machine's
agents because reply records are not indexed by domain.

It refuses any request whose `Host` header is not `127.0.0.1:<port>` or
`localhost:<port>` (403). That keeps a web page from another site from
reading the snapshot by pointing its own domain at `127.0.0.1` (DNS
rebinding). Everything is `GET` and read-only. Nothing from the browser
reaches the service except the choice of `hours`.
Local store views use `Store(..., initialize=False)` so reading never
publishes metadata or runs retention cleanup. One cached hours selection and
a shared refresh lock bound retained cache data and coalesce parallel tabs.

**Dashboard** (`monitor/`): Svelte 5 with Vite, built to static files (plain
Vite, no SvelteKit, since there is one page and no routing). It fetches
`/api/snapshot` every 30 seconds and shows the time of the last refresh,
and a stale warning when a refresh fails. The first screen answers "is
anything broken?" before anything else:

- **Top bar:** a health pill (the number of problems, or "All healthy"), the
  domain, waiting and running work, machines up, and the refresh controls.
  The page title carries the problem count.
- **Alerts:** one line per problem, most severe first, capped at six with
  the rest behind a toggle.
- **Topics:** one row per declared work topic and error topic, sorted by
  severity: workers serving/seen, waiting, running, oldest wait and average
  wait, expandable to the tasks (owner, lease, `owned_count`). Reply topics
  (those of this machine's requests) and undeclared topics are listed apart
  and left out of the totals.
- **Machines:** workers on declared topics grouped by identity, each marked
  busy, idle, waiting for capacity, silent, down or lease lapsed.
- **Pending requests:** a summary of each request's data and its task's
  state: queued with its position, running and on which worker, lapsed,
  unconfirmed, or queued with no live worker.

Below them: the inbox (including unmatched replies) and throughput and
latency per topic and hour (created / consumed, average and maximum wait
and run times, re-owns and empty polls), as small inline SVG charts with no
chart library.

Health is derived in `monitor/src/health.ts` from the snapshot alone,
measured against each domain's `generated_at`:

- A worker is **busy** while it holds a task with an unexpired lease (a
  worker at capacity stops polling, §4.9), **lapsed** when it holds one whose
  lease has expired, **idle** within 3 minutes of its last poll (workers
  poll every 30 s), **silent** up to 10 minutes, **down** after that and
  **inactive** after a day. A quiet worker on a machine with a busy worker
  is **waiting for capacity**, since workers on one machine share it.
- A machine is down when none of its workers is busy, idle or waiting and
  one is down; silent likewise. Only pollers of declared work topics count,
  because `tl wait` also polls reply topics. A domain with no declared
  topics shows a hint instead.
- A work topic is **critical** when tasks wait and no worker serves it,
  and a **warning** when its oldest task has waited over 10 minutes and 3×
  its average wait, or a held lease has lapsed. An error topic with tasks
  is critical.
- A pending request finds its task by `task_id` among the topic's first
  100 tasks. A request whose submission stays unconfirmed for 5 minutes is
  a warning.

Busy detection only sees tasks among the first 100 per topic, and a worker
that waits for capacity held outside tasklease (such as a machine-wide test
lock) stops polling and looks silent, then down. A service-side liveness
signal would remove both limits.

### 5.8 Packaging and releases

- **Build backend:** `hatchling`, with a custom build hook
  (`hatch_build.py`) that uses the root npm workspace lockfile and builds
  `monitor/`, copying output to `tasklease/monitor/static/`. Both the wheel and the
  sdist contain the built dashboard, so installing either never needs Node.
- **The Worker bundle.** The same hook runs `wrangler deploy --dry-run
  --outdir` in `../service/`, which bundles the Worker and its npm
  dependencies into one JavaScript module without deploying it. Next to it
  goes `wrangler.template.jsonc`: `service/wrangler.jsonc` with the
  account-specific values (name, bucket, account ID) left as
  placeholders, `main` rewritten to the bundled module's file name, and
  `no_bundle: true`. Copying the checkout's configuration as is would point
  `main` at the TypeScript source, which is not in the package. A `manifest.json` records the package
  version and the exact wrangler version the bundle was built and tested
  with. All three go to `tasklease/service_bundle/`. So a user who installed
  from PyPI deploys with no checkout and no `npm install` (§8.2), and the
  service they deploy always matches their client.
- **Worker version:** build and development wrappers derive it from the
  Python project's version and inject it through a temporary Wrangler config;
  they do not rewrite source files or maintain an independent service version.
- **Shipping build output.** Hatchling leaves out of the sdist and wheel
  every path that git ignores, even one the build hook has just created. So
  `pyproject.toml` lists both build outputs under `artifacts` in
  `[tool.hatch.build]`: `src/tasklease/monitor/static/**` and
  `src/tasklease/service_bundle/**`. A release check opens the built wheel
  and fails unless both are inside.
- **Building from an sdist.** `uv build` builds the sdist first, then builds
  the wheel from the unpacked sdist, where `../monitor/` does not exist. So
  the hook builds only when `../monitor/` (and `../service/`) are present.
  When they are absent and the built files already exist (they came in the
  sdist), the hook does nothing. When both are missing, the build fails with
  a clear message rather than shipping a wheel without the dashboard or the
  Worker. The
  generated Python types (`tasklease/generated/`) are committed (§3), so they
  need no build step.
- **Development:** `uv sync` in `python/` gives an editable install. The
  build hook, or `make monitor`, builds the dashboard; until then `tl
  monitor` serves a page saying the dashboard was not built.
- **Versions:** `MAJOR.MINOR.PATCH` in `pyproject.toml`, with the protocol
  version (`/v1/`) independent of it. A release that needs a service change
  says so in the changelog, and the service is deployed first.
- **Releasing:** routine checks remain local. The optional Linux-only
  `.github/workflows/workflow.yml` runs manually for an existing version tag
  on `main`, checks/builds it, and publishes only with an explicit input.
  Its separate publishing job uses PyPI Trusted Publishing via OIDC, without
  a stored PyPI token. `make release` remains a local token-based alternative.
  See [releasing.md](releasing.md) for setup and exact commands.
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

`tl service deploy` and `status` are tested with a fake `wrangler`: a small
script put first on `PATH` that records its arguments and replays canned
output and exit codes. The tests cover every step's created / unchanged
path, every stop, the generated `wrangler.jsonc`, and that a new admin token
is printed even when a later step fails.

Before each release, a real deploy of a throwaway copy runs with
`--config <temporary file> --name tasklease-test --bucket
tasklease-test-blobs`, so it never touches the release machine's own
`config.toml`.

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
dashboard build). `fsio` and the store run on all three OSes, including
request IDs that differ only in case (`Foo`, `foo`) and Windows reserved
names (`CON`), which must land in separate files. The dashboard
gets `svelte-check` and a few component tests (Vitest) that render a
snapshot fixture taken from `schema/examples/`.

Routine GitHub Actions testing is avoided for cost; the optional manual
Linux-only publishing workflow also runs the local checks before uploading.
The routine test runs are split:

- `make check` on any machine runs the service tests, the Python tests and
  the conformance suite against `wrangler dev`.
- The macOS and Windows runs of the Python tests are run by hand on those
  machines until M4. From then on they are requested through tasklease
  itself, as a `test/<os>` topic.

## 8. Deployment

### 8.1 Environments

`dev` is `wrangler dev` in a checkout, with local Durable Objects and R2.
`prod` is the deployed Worker, deployed with `tl service deploy` from any
machine where tasklease and Node are installed. A `staging` environment is
not needed for one operator: deploy a second copy under another `--name`
with its own `--config` when one is wanted.

### 8.2 `tl service deploy`

Deploys or upgrades the service in the operator's own Cloudflare account. It
does not talk to Cloudflare's API itself: it runs **wrangler**, Cloudflare's
own deployment tool, which already handles the script upload, Durable Object
migrations, keeping existing secrets, the `workers.dev` address, and sign-in.

- **What it needs:** Node.js (a current LTS) on the machine that deploys,
  and only there. Agents and workers on every other machine never need Node.
- **Which wrangler:** always the exact version recorded in the bundle's
  `manifest.json`, run as `npx --yes wrangler@<version>`, so every deploy
  uses the wrangler the release was tested with. No global install.
- **Target isolation:** discard inherited `CLOUDFLARE_ENV` and
  `WRANGLER_CI_OVERRIDE_NAME` when launching Wrangler. The selected account,
  name and top-level config determine the target; another project's shell
  environment cannot rename this deployment.
- **What it deploys:** the Worker bundled in the installed package (§5.8),
  from a temporary project folder holding that bundle and a `wrangler.jsonc`
  filled in from the template. `no_bundle: true`, so wrangler uploads the
  bundle as is and nothing is installed from npm.
- **Sign-in:** whatever wrangler is signed in with. Either `wrangler login`
  once (browser sign-in, stored by wrangler), or `CLOUDFLARE_API_TOKEN` for
  scripted use. `tl` never sees or stores either.

Every step checks the current state first, so running it again is safe, and
each step reports `created`, `updated` or `unchanged`. A failed wrangler
command stops the deploy and shows wrangler's own message.

**Inputs.**

| Input | Flag | Environment | Saved in config |
|---|---|---|---|
| Cloudflare account ID | `--account-id` | `CLOUDFLARE_ACCOUNT_ID` | yes |
| Worker name | `--name` (default `tasklease`) | | yes |
| R2 bucket name | `--bucket` (default `tasklease-blobs`) | | yes |
| R2 Access Key ID | `--r2-access-key-id` | `TL_R2_ACCESS_KEY_ID` | no (Worker secret) |
| R2 Secret Access Key (always together with the ID) | prompted | `TL_R2_SECRET_ACCESS_KEY` | no (Worker secret) |
| Admin token, for the end-to-end check on upgrades | | `TL_ADMIN_TOKEN` | no |
| Service URL (only if wrangler does not report it, step 5) | `--url` | | yes |
| Config file | `--config` (default: the user config, §5.6) | `TL_CONFIG` | |

Later runs reuse the saved values, so an upgrade is
`uv tool upgrade tasklease && tl service deploy`. `--config` points the whole
command, reads and writes, at another file: the throwaway test deploy (§7.2)
and a second copy use it, so they never overwrite the production config.

**Steps.** The Worker is deployed before its secrets are read, because
`wrangler secret list` fails on a Worker that does not exist yet. That
keeps `tl` from having to tell "no such Worker" apart from other wrangler
failures by parsing error text: every failed wrangler command stops the
deploy.

1. **Check wrangler and sign-in:** Node is on `PATH`, and
   `wrangler whoami` shows a signed-in account. `--account-id` is required
   when several accounts are visible, and always when signing in with
   `CLOUDFLARE_API_TOKEN`: the token template cannot list accounts, so
   wrangler may not find the account on its own. On failure, print the
   matching part of the help text.
2. **Check the R2 key inputs:** the two R2 keys are given together or not
   at all; one without the other stops with exit code 4. If `--bucket`
   differs from the bucket in the saved config, new keys are required,
   since keys scoped to the old bucket would not work on the new one.
3. **R2 bucket:** `wrangler r2 bucket list`, then `wrangler r2 bucket create`
   if missing. Set the lifecycle rule `tasklease-expire` (delete objects
   after 7 days) with `wrangler r2 bucket lifecycle add`, replacing an older
   rule of that name.
4. **Deploy the Worker:** fill in `wrangler.jsonc` (name, account ID,
   bucket, `main` pointing at the bundled module, the variables
   `R2_ACCOUNT_ID` and `R2_BUCKET_NAME`, and `workers_dev: true`) and run
   `wrangler deploy`. Wrangler applies pending Durable Object migrations and
   keeps existing secrets.
   **The URL:** `tl` sets `WRANGLER_OUTPUT_FILE_PATH` to a temporary file,
   where wrangler appends one JSON line per event. `tl` reads it line by line
   and selects the `deploy` entry, whose `targets` lists the deployment's
   addresses (wrangler source, `packages/workers-utils/src/output.ts`). It takes
   the single target ending in `.workers.dev`, accepting a bare hostname or
   an HTTPS origin and normalizing it to HTTPS. It does not parse other
   entries or wrangler's terminal output, whose wording can change. If there
   is no such target, the deploy stops with exit code 4 and asks for the URL
   with `--url`, which is then saved like any other input.
   After a successful upload and URL resolution, save these nonsecret target
   coordinates to the selected config, marked unverified in command output.
   Expected setup stops can then resume the same custom account/name/bucket.
   Credentials and unrelated client settings are never replaced by this save.
   When an R2 key pair is being updated, this checkpoint also sets
   `cloudflare.r2_credentials_pending = true`. Retries require a pair while
   this flag is set, even when saved target coordinates already match. Clear
   it only after the secret bulk command succeeds.
5. **Existing secrets:** `wrangler secret list --name <name>`. The Worker
   exists now, so this succeeds; a failure is a real failure. Which secrets
   are missing is decided from this list.
6. **Admin token:** if `ADMIN_TOKEN` is missing, or `--rotate-admin-token`
   was given, generate one (32 random bytes, `tla_` prefix). **Print it
   before setting it**, marked "not yet confirmed", with a warning that it
   is stored nowhere. Then set it, together with any new R2 keys, in one
   `wrangler secret bulk` call, with the values on standard input, never on
   the command line.
   **Confirm it by using it,** whether or not `secret bulk` reported
   success: call `identity.list` (read-only, admin only) with the printed
   token, retrying for about 15 seconds while the new secret takes effect.
   The admin token is never cached (§4.4), so the answer is exact. A secret
   *existing* in `secret list` proves nothing about its *value*: after a
   failed rotation, the old token still exists and still works.
   - It works: print "confirmed". The printed token is the admin token.
   - The deadline passes with `unauthenticated` or transport failures: print
     that the token is **unconfirmed**, and stop. Propagation may still be in
     progress; do not infer that the candidate was permanently rejected or
     that the old token still works. Keep the candidate and retry with
     `TL_ADMIN_TOKEN` set to it, or explicitly rotate again.
7. **R2 credentials:** if `R2_ACCESS_KEY_ID` or `R2_SECRET_ACCESS_KEY` is
   still missing, stop here with exit code 4 and print the dashboard steps
   for creating them (help text, step 3), naming the bucket. The message
   also says to `export TL_ADMIN_TOKEN=<the token printed above>` before
   running the command again, so that run can do the end-to-end check
   (step 9). The Worker and its admin token are in place by then, so nothing
   is lost.
8. **Check the version:** `GET /v1/version` on the deployed URL must return
   the package's version. A new address takes a little while to come up, so
   this retries with backoff for about 15 seconds before it fails.
9. **Check everything end to end:** with the admin token (from step 6, or
   `TL_ADMIN_TOKEN` on later deploys), using only ordinary operations:
   1. `identity.add` a temporary identity `deploy-check-<random>`, scoped to
      the reserved domain `tasklease.deploy-check` with `create`, `own` and
      `read` on `check`;
   2. Create, Own and Consume a probe task in topic `check`, which proves the
      Durable Objects and SQLite work;
   3. `blob.put_url`, upload 16 random bytes to the URL, `blob.get_url`,
      download them and compare, which proves the R2 keys, the bucket and
      `R2_BUCKET_NAME` all agree;
   4. `identity.revoke` the temporary identity, also when a step failed.

   A failure names the part that failed: Durable Objects, upload, or
   download. Without an admin token (an upgrade with no `TL_ADMIN_TOKEN`),
   this step is skipped with a warning that the deploy was not verified.
   The probe blob expires with the lifecycle rule.
10. **Finish the config checkpoint** with the URL and saved inputs, preserving
    unrelated settings, and report the verification result. Then print the
    next steps: `tl identity add` for each machine, and `tl topic declare`.

Between steps 4 and 6 of a first deploy, the Worker runs for a few seconds
without its secrets. Requests then fail as `unauthenticated`, which is
harmless: nothing can have an identity yet.

`--dry-run` runs steps 1 and 2 and the read-only parts of step 3, and, if
the Worker exists, step 5. It prints the filled-in `wrangler.jsonc` and what
each later step would do, and changes nothing.

### 8.3 `tl service status`

Shows, for the configured service: the deployed Worker's version (from
`/v1/version`) against the installed package's, and whether an upgrade is
needed. With wrangler signed in, it also shows the bucket and its lifecycle
rule, and which of the three secrets exist (never their values). It changes
nothing.

There is no `tl service destroy`: deleting the Worker deletes every
domain's Durable Object data. That is done by hand in the dashboard.

### 8.4 Help text

`tl service deploy --help` explains how to get every input, in the order
needed. This is its text:

```text
usage: tl service deploy [--account-id ID] [--name NAME] [--bucket NAME]
                         [--r2-access-key-id ID] [--url URL]
                         [--rotate-admin-token] [--config FILE] [--dry-run]

Deploy or upgrade the tasklease service in your Cloudflare account.
Safe to run again: each step checks what already exists.

You need:
  - A Cloudflare account with Workers and R2 enabled. R2 asks for a
    payment method before first use, even on the free tier.
  - Node.js (current LTS) on this machine, for Cloudflare's "wrangler"
    tool. tl runs the exact wrangler version it was tested with through
    npx; you do not need to install wrangler yourself.

Before the first deploy:

1. SIGN IN TO CLOUDFLARE
   Run once:   npx wrangler login
   A browser window opens; approve the access. wrangler keeps the
   sign-in; tl never sees it.
   (For scripts instead: create an API token in the dashboard under
   "My Profile" > "API Tokens" > "Create Token", using the template
   "Edit Cloudflare Workers". It grants Workers Scripts, Workers R2
   Storage, Workers KV Storage and Workers Routes write access and
   Workers Tail read access, which covers everything this command does.
   Export it as CLOUDFLARE_API_TOKEN, and also pass --account-id: the
   template cannot list your accounts.)

2. ACCOUNT ID  (--account-id, or CLOUDFLARE_ACCOUNT_ID)
   Needed with an API token, or if your sign-in can see more than one
   account; tl tells you if so. In the dashboard (https://dash.cloudflare.com), open
   "Workers & Pages". Under "Account Details", select the copy button
   next to "Account ID". (Or press Ctrl+K / Cmd+K on the account home
   page and search for "Copy account ID".)

3. R2 ACCESS KEYS  (--r2-access-key-id and a prompt, or
   TL_R2_ACCESS_KEY_ID and TL_R2_SECRET_ACCESS_KEY)
   The service uses these to give machines upload and download links.
   Give both or neither. The bucket must exist first, so on the very
   first deploy run this command without them: it creates the bucket
   and the service, prints the admin token, then stops and repeats these
   steps. Before running it again, store the admin token and set
   TL_ADMIN_TOKEN to it, so the second run can check the whole service.
   Then:
   Open "R2 Object Storage" > "Overview". Under "Account Details", select
   "Manage" next to "API Tokens", then "Create Account API token".
   Choose the permission "Object Read & Write", and under bucket scope
   choose "Apply to specific buckets only" with the tasklease bucket
   (default "tasklease-blobs"). Create it and copy both the "Access Key
   ID" and the "Secret Access Key": the secret is shown once.

The service gets an address under your account's workers.dev
subdomain: https://<name>.<subdomain>.workers.dev. If you have never
opened "Workers & Pages", open it once so Cloudflare creates that
subdomain.

On the first deploy, this command prints the ADMIN TOKEN once, just
before setting it, and then confirms it by using it. Store it in a
password manager: it is needed for "tl identity add" (as
TL_ADMIN_TOKEN) and is not saved anywhere. If the command says the
printed token is unconfirmed, keep it: propagation may still be in progress.
Retry with TL_ADMIN_TOKEN set to it, or explicitly rotate again. If the token is lost, run
"tl service deploy --rotate-admin-token" to replace it.

At the end, the command checks the whole service: it adds a temporary
identity, runs a task through it, uploads and downloads a small file,
and removes the identity again. On upgrades this needs TL_ADMIN_TOKEN
in the environment; without it the check is skipped with a warning.

If you change --bucket, give new R2 access keys for the new bucket too.

To deploy a separate test copy without touching this machine's settings:
  tl service deploy --config /tmp/tl-test.toml --name tasklease-test \
                    --bucket tasklease-test-blobs

Exit codes: 0 deployed; 4 more input needed (the message says which);
1 failed (the message says which step, with wrangler's own output).
```

The dashboard labels and the token template's permissions in this text
were checked against Cloudflare's documentation on 2026-11-07, and the
output file's `deploy` entry against wrangler's source. The first real
deploy in M1 exercises all of it; the help text is updated if anything
differs.

### 8.5 Running workers

`deploy/` documents the packaged templates. `tl worker template --platform`
prints one for editing before installation; automated `tl worker install`
remains a future convenience:

- **Linux:** a systemd user unit (`Restart=on-failure`, `KillSignal=SIGHUP`
  for a graceful stop).
- **macOS:** a launchd agent plist (restart on unsuccessful exit). Graceful
  stop uses the stop command; unloading or logout has a bounded stop timeout.
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

Settled: the service lives at its `workers.dev` address, with no custom
domain in version 1 (§8.2). The "Edit Cloudflare Workers" token template
includes Workers R2 Storage write access, and wrangler's output file lists
the deployed URLs in the `deploy` entry's `targets`.

Settled: the cursor format is spec §4.8's base64url of
`v1:<created_at>:<id>`, used as an SQLite keyset
(`WHERE (created_at, id) > (?, ?) ORDER BY created_at, id`), which does not
need the cursor's own task to still exist.

Settled: **Content-Length in presigned PUTs** (§4.5) is enforced by R2 with
the validated `allHeaders: true` configuration and live rejection test.

1. **A `tl worker install` command**, or are the templates in `deploy/`
   enough?
2. **Cloudflare's own metrics** (requests, errors, Durable Object running
   time, cost) on the dashboard. They need a Cloudflare API token on the
   machine running `tl monitor`; left out of the first version.
