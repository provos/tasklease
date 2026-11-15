# Server-carried `origin_id` and first-stage cancellation

Implementation design, 2026-11-14. Target release: `0.5.0`. Updates [spec.md](spec.md) and
[design.md](design.md); supersedes issue `#17` and the closed `reply_from`
approach.

## 1. Motivation

Four problems in `0.4.x` trace back to separating protocol identity (`id`,
which rotates on every `Own`) from request/reply correlation (`data.request_id`,
which callers and workers copy inside untrusted JSON):

1. **Forged reply slot squatting (`#17`).** `Store` saves the first reply
   carrying `data.request_id` at `replies/h(agent)/h(request_id).json` with
   `publish_new` (no-replace), then `Delete`s any later task carrying the same
   `request_id` as a duplicate. Whenever more than one identity has `create` on
   an agent's reply or inbox topic (for example `verified/*` across worker
   machines, or `inbox/*/*` across session machines), another identity can post
   a task with a victim's `request_id` first. The genuine reply that arrives
   later finds the slot taken, is deleted from the queue, and is lost.
2. **Why client-side sender pinning (`reply_from`) is the wrong direction.**
   Pinning an expected `creator` in the local request record fails in two
   protocol-level ways before implementation details even arise:
   - *Worker pools:* when `linux-1` and `linux-2` both serve `verify/linux`,
     the requester cannot know at `submit` time which worker identity will win
     `Own`.
   - *Confused-deputy bounce:* if another requester can submit a fast or
     invalid task to `verify/linux` with `reply_topic: "verified/<victim>"` and
     `request_id: "<victim>"`, `linux-1` itself `Consume`s that task onto the
     victim's reply topic with `creator = "linux-1"`, satisfying `reply_from`.
   - *Local state complexity:* partitioning replies into accepted vs. refused
     paths based on mutable/prunable local intents introduces retention races
     (pruning an unanswered intent reopens the plain answer slot), drain-loop
     early-exit bugs on refused replies, cancel-and-resubmit collisions, and
     full-directory scans.
3. **Post-`Own` cancellation gap.** `Delete` takes the live `id` and allows the
   `creator` only while `owner` is `null`. Once a worker calls `Own`, `id`
   rotates and the requester can neither name nor cancel its own first-stage
   request, even while it is still queued behind retries or running on the
   initial topic.
4. **Broken pending-request tracking in `tl monitor`.**
   `monitor/src/health.ts` joins each local pending request against the
   service snapshot using `topic.tasks.find((t) => t.id === request.task_id)`.
   Because `Own` rotates `t.id`, every running request disappears from that
   join as soon as a worker claims it and is misreported as
   `"Not on the board: finished, reply on its way"`.

Carrying an unforgeable, server-minted `origin_id` on every task solves all
four problems in the protocol itself.

---

## 2. Core design decisions

1. **Mandatory server-carried `origin_id`.**
   - `Create` mints `id = newTaskId(now)` and sets `origin_id = id`. Callers
     never supply `origin_id`.
   - `Own` rotates `id` (preserving lease fencing) and copies `origin_id`
     unchanged.
   - `Renew` updates `lease_until` in place; `id` and `origin_id` are
     unchanged.
   - `Consume` (including operator `tl move`) retires `id` and copies the
     consumed task's `origin_id` onto every successor task it posts.
   - `origin_id` is required on both `Task` (`own`, `renew`, `read`) and
     `TaskSummary` (`tasks.list`, `monitor.snapshot`).
2. **Drop `data.request_id` from `tasklease`.**
   - `Store`, `WorkerLoop`, `EchoHandler`, `tl`, and `tl monitor` correlate
     exclusively via `origin_id`.
   - `data` remains opaque JSON (`reply_topic` stays an optional convention in
     `data` for `WorkerLoop` and `Store.submit`).
   - Each new task inserted by `Create` receives a fresh `origin_id`. Only
     `Consume` of a task with `origin_id = X` can produce another task with that origin,
     no caller using `Create` can ever forge another task's `origin_id`.
3. **Create-first submission (Option A).**
   - `Store.submit` validates inputs locally, calls `Create` first, receives
     `{id}` (which is `origin_id`), and then publishes
     `requests/h(agent)/<origin_id>.json`.
   - `submission_unknown`, `.task.json` sidecars, `Store.resend`, and
     `tl request --resend` are removed.
   - Optional UUIDv7 keys in [create-idempotency.md](create-idempotency.md)
     extend this design with seven-day Create retry recovery. Keys are scoped
     to domain and creator and never choose an origin or reply slot. The first
     accepted request wins; replay can recover the origin even after Consume
     or Cancel. Callers retain the key and original request arguments.
   - *Unkeyed fault behaviour:* if `Create` fails or times out before returning
     `{id}`, no local request record is written. If the caller retries without
     a key, `Create` mints
     a new `origin_id` (`T2`). Even if the timed-out first `Create` (`T1`) had
     landed on the server, any eventual reply to `T1` carries `origin_id = T1`,
     saves harmlessly at `replies/h(agent)/<T1>.json` as an unmatched inbox
     entry, and never collides with `T2`. If the process crashes *after*
     `Create` returns `{id}` and before local disk publication, the same
     correlation isolation holds. Both uncertain submissions may still execute.
     A local save error after a known response raises `SubmissionError` with
     `origin_id`, allowing direct inspection or cancellation.
4. **First-stage cancellation (`cancel`).**
   - Add a dedicated operation `cancel {domain, topic, origin_id} -> {}`.
   - Cancellation applies **only to the first task** (the initial lineage
     created by `Create`, across any number of `Own` rotations or lease
     expirations). Once `Consume` transitions the task into one or more
     successor tasks (even onto the same topic), the initial task is gone and
     `cancel` returns `not_found`.
   - Keeping `cancel` separate from `delete {domain, topic, id, lease_before?}`
     preserves `delete`'s strict `id` fence (used by workers draining replies
     and by `tl prune`) and keeps every operation schema a flat object without
     `oneOf` unions in code generation.
5. **One selected reply per origin, with conflicting content archived.**
   - `Store` models 1-request-to-1-reply (`submit` / `wait` / `reply`) plus a
     general drain (`drain` / `poll` / `inbox`). Saving each reply at
     `replies/h(agent)/<origin_id>.json` via `publish_new` (no-replace) keeps
     concurrent lock-free draining idempotent when `Delete` fails: if `Store`
     saves a reply and loses the `Delete` response, the reply's 60 s lease
     expires and the next `Own` delivers that same reply with a rotated `id`
     and the same `origin_id`. `publish_new` returns `"exists"`, and `Store`
     acknowledges the redelivery with `Delete`.
   - Why not `<origin_id>_<count>.json`? Because `Own` rotates `task.id`, a
     local counter cannot tell a redelivery after a lost `Delete` apart from a
     second fan-out task sharing `origin_id`, so a lost `Delete` would write
     duplicate reply files. `reply()` and `wait()` keep returning the selected
     first answer. Distinct stable content (`topic`, `creator`, `created_at`,
     `data`) is saved before acknowledgement under
     `duplicates/<origin_id>-<sha256>.json` and exposed by `inbox()` with
     `duplicate: true`. Canonical JSON fingerprints exclude fencing IDs and
     local receipt times, so repeated delivery adds no archive copies. A stable
     per-lineage birth ID would still be needed to count separate successors
     with identical content; this archive preserves observations instead.
   - Because `origin_id` is a mandatory `TaskId` (`^t_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
     on every task, no reply is ever missing an identifier, and `TaskId` is
     already fixed-length, uppercase-only, and safe on case-insensitive macOS
     and Windows filesystems (no `CON`/`NUL` collisions). Both `.task.json` and
     `replies/h(agent)/unmatched/` disappear.

---

## 3. Protocol & schema changes (`schema/`)

### 3.1 `schema/common.json`

Add `origin_id` (`$ref: "#/$defs/TaskId"`) to `Task` and `TaskSummary`, and to
both `required` arrays:

```json
"Task": {
  "properties": {
    "id": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/TaskId" },
    "origin_id": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/TaskId" },
    "domain": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/DomainName" },
    "topic": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/TopicName" },
    "created_at": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/Timestamp" },
    "lease_until": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/Timestamp" },
    "owned_count": { "type": "integer", "minimum": 0 },
    "creator": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/IdentityName" },
    "owner": { "anyOf": [{ "$ref": "https://tasklease.dev/schema/common.json#/$defs/Owner" }, { "type": "null" }] },
    "owned_at": { "anyOf": [{ "$ref": "https://tasklease.dev/schema/common.json#/$defs/Timestamp" }, { "type": "null" }] },
    "data": { "$ref": "https://tasklease.dev/schema/common.json#/$defs/Data" }
  },
  "required": [
    "id", "origin_id", "domain", "topic", "created_at", "lease_until",
    "owned_count", "creator", "owner", "owned_at", "data"
  ],
  "additionalProperties": false
}
```

`TaskSummary` gains `"origin_id"` with the same `TaskId` reference and lists it
in `required` alongside `id`, `state`, `created_at`, `lease_until`,
`owned_count`, `creator`, `owner`, and `owned_at`.

`CreateResponse` (`{ok: true, id: TaskId}`) is unchanged: on `Create`, the
new task's `origin_id` is equal to the returned `id`.

### 3.2 `schema/ops/cancel.json`

Add `POST /v1/cancel`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://tasklease.dev/schema/ops/cancel.json",
  "title": "Cancel",
  "$defs": {
    "Request": {
      "title": "CancelRequest",
      "type": "object",
      "properties": {
        "domain": {
          "$ref": "https://tasklease.dev/schema/common.json#/$defs/DomainName"
        },
        "topic": {
          "$ref": "https://tasklease.dev/schema/common.json#/$defs/TopicName"
        },
        "origin_id": {
          "$ref": "https://tasklease.dev/schema/common.json#/$defs/TaskId"
        }
      },
      "required": ["domain", "topic", "origin_id"],
      "additionalProperties": false
    },
    "Response": {
      "title": "CancelResponse",
      "type": "object",
      "properties": {
        "ok": { "const": true }
      },
      "required": ["ok"],
      "additionalProperties": false
    }
  }
}
```

### 3.3 Holder rule & operation semantics (`spec.md` §2–§5)

| Operation | Authorization | Target & effect |
|---|---|---|
| `create` | `create` scope on `topic` | Mints `id`, sets `origin_id = id`, marks task as initial stage |
| `own` | `own` scope on `topic` | Retires `id`, mints new `id`, preserves `origin_id`, `creator`, and initial-stage status |
| `renew` | `own` scope on `topic`, and `owner.identity == caller` (or operator) | Extends `lease_until`; `id` and `origin_id` unchanged |
| `consume` | `own` scope on `topic`, `create` scope on each target topic, and `owner.identity == caller` (or operator) | Retires `id`; each new task gets fresh `id`, `origin_id = consumed.origin_id`, `creator = caller`, and non-initial-stage status |
| `delete` | `owner.identity == caller`, or (`owner == null` and `creator == caller`), or operator | Retires exact `id` (with optional `lease_before` guard) |
| `cancel` | `creator == caller` or operator (domain membership required; no topic scope required, matching `delete`) | Retires the initial-stage task matching `(topic, origin_id)` whether `available` or `held`. Returns `not_found` if never created, on another topic, already deleted/cancelled, or already `Consume`d; returns `forbidden` if a live initial-stage task belongs to another `creator` |

Hourly statistics (`stats_hourly.deleted`) count lineages ended in `topic` by
either `delete` or `cancel`.

---

## 4. Cloudflare service & SQLite storage (`service/`)

### 4.1 SQLite migration (`service/src/domain_do.ts`)

Append migration 3 to `migrations`:

```sql
ALTER TABLE tasks ADD COLUMN origin_id TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN root INTEGER NOT NULL DEFAULT 0;
UPDATE tasks SET origin_id = id WHERE origin_id = '';
CREATE UNIQUE INDEX tasks_root_origin ON tasks (origin_id) WHERE root = 1;
```

Why the internal `root` (`0 | 1`) column?
- `Create` inserts `root = 1` and `origin_id = id`.
- `Own` copies `row` (`origin_id: row.origin_id`, `root: row.root`).
- `Consume` inserts every successor task with `origin_id = row.origin_id` and
  `root = 0`.
- Why not rely on `row.creator === caller.identity` alone? In single-machine or
  pipeline setups where one identity holds both `create` and `own` on `topic`
  (or `Consume` posts successor tasks back onto the same topic), a stage-2 task
  would have the same `(topic, origin_id, creator)` as the stage-1 task, and
  fan-out could even produce multiple such rows. With `root = 1` set only by
  `Create` and cleared by `Consume`:
  1. At most **one** row in the domain ever has `(origin_id = X, root = 1)`.
  2. `Cancel` enforces the "first task only" rule unconditionally, regardless
     of identity overlap or self-looping topics.
- Migrated pre-`0.5.0` rows receive `origin_id = id` (satisfying the `TaskId`
  schema invariant on `Read`, `Own`, `Tasks List`, and `Monitor Snapshot`) and
  `root = 0` (so pre-upgrade reply/pipeline tasks whose true origin predates
  the migration cannot be misidentified as initial tasks; `delete` by `id`
  remains available).

### 4.2 `DomainObject` methods

- **`insertTask(topic, data, caller, now, originId?, root?)`:**
  - `Create` calls `insertTask(request.topic, JSON.stringify(request.data), caller, now)`
    → `const id = newTaskId(now); origin_id = id; root = 1`.
  - `Consume` calls `insertTask(target.topic, JSON.stringify(target.data), caller, now, row.origin_id)`
    → `const id = newTaskId(now); origin_id = row.origin_id; root = 0`.
- **`attemptOwn`:** `const next: TaskRow = { ...row, id: newTaskId(now), ... }`
  naturally preserves `row.origin_id` and `row.root`.
- **`cancel(request: CancelRequest, caller: Caller)`:**
  ```ts
  async cancel(
    request: CancelRequest,
    caller: Caller,
  ): Promise<Result<CancelResponse>> {
    return this.operation((now) => {
      const row = this.ctx.storage.sql
        .exec<Pick<TaskRow, "id" | "creator">>(
          "SELECT id, creator FROM tasks WHERE origin_id = ? AND topic = ? AND root = 1",
          request.origin_id,
          request.topic,
        )
        .toArray()[0];
      if (!row) return failure("not_found");
      if (!caller.operator && row.creator !== caller.identity) {
        return failure("forbidden");
      }
      this.ctx.storage.sql.exec("DELETE FROM tasks WHERE id = ?", row.id);
      this.recordStats(request.topic, now, { deleted: 1 });
      return { ok: true };
    });
  }
  ```
  `tasks_root_origin` enforces one initial task per origin and makes Cancel a
  unique-index lookup without loading payload data or scanning fan-out tasks.
  No long-poll timer adjustment is needed: if a held task's lease timer fires
  later, `recheck` queries live SQLite rows and ignores the deleted task.
- **`metadataColumns` & `taskFields`:** include `origin_id` so `tasksList`,
  `monitorSnapshot`, `own`, `renew`, and `read` emit `origin_id`.

### 4.3 Worker router (`service/src/index.ts`)

Register `cancel` alongside `delete`:

```ts
cancel: domainRoute(
  validators.validateCancelRequest,
  () => true,
  (body, caller, env) =>
    env.DOMAINS.getByName(body.domain).cancel(body, caller),
),
```

---

## 5. Python package (`python/`)

### 5.1 `client.py`, `names.py`, and `cloudflare/deployer.py`

- `client.py`:
  - Add `"cancel": {}` to `_SUCCESS_FIELDS`.
  - Add `Client.cancel(self, request: p.CancelRequest, *, timeout: float | None = None) -> p.CancelResponse`.
  - Validate canonical IDs on Create and `origin_id` on tasks and summaries
    in `own`, `renew`, `read`, `tasks.list`, and `monitor.snapshot` responses,
    raising `ProtocolError` on invalid or missing IDs. Task-bearing responses
    from an un-upgraded `< 0.5.0` service fail this check.
- `names.py`:
  - Remove `is_request_id` (only `data.request_id` used it; callers now validate
    `origin_id` with `is_task_id`). Keep `hash_id` in `store.py` (still used
    for `agent_id` directories and worker stop files).
- `cloudflare/deployer.py`:
  - In `_probe`, assert `owned["task"]["origin_id"] == created["id"]` during the
    end-to-end deploy check to confirm SQLite migration 3 is active.

### 5.2 `store.py`

#### File layout

```text
<user_data_dir>/tasklease/
├── requests/h(agent_id)/
│   ├── agent.json                  {"agent_id": "<agent_id>"}
│   └── <origin_id>.json            {"origin_id", "domain", "topic", "reply_topic", "created_at", "data"}
└── replies/h(agent_id)/
    ├── agent.json                  {"agent_id": "<agent_id>"}
    ├── .prune-<day>.json           {"created_at": <ms>}
    ├── <origin_id>.json            {"task_id", "origin_id", "topic", "creator", "created_at", "received_at", "data"}
    └── duplicates/
        └── <origin_id>-<sha256>.json  same saved reply fields
```

- `agent_id` directories keep `h(agent_id)` because topic segments allow
  case-colliding or Windows-reserved names such as `con`.
- Individual request and reply files are named `<origin_id>.json` (`t_` + 26
  uppercase Crockford base32 chars): portable across POSIX and Windows and
  readable in directory listings.
- Archive filenames include a 64-character lowercase SHA-256 content digest;
  reads validate both their syntax and correspondence to the saved content.
- `.task.json` sidecars and `unmatched/` are removed.
- `store.py` stays strictly fail-closed with **no `0.4.x` legacy file
  compatibility branches**: request and selected-reply JSON records use `<TaskId>.json` and
  match `record.origin_id`. Invalid record names and the legacy `unmatched/`
  directory raise `StoreError` without deleting data. Agent metadata, dotfiles
  and unrelated non-JSON files (such as `desktop.ini` or `Thumbs.db`) are ignored.
  Local store directories on existing `0.4.x` machines are cleared once during the
  `0.5.0` upgrade (§8).

#### Data types

```python
type RequestState = Literal["pending", "answered"]
type CancelResult = Literal["cancelled", "not_found"]

@dataclass(frozen=True)
class RequestRecord:
    agent_id: str
    origin_id: str
    domain: str
    topic: str
    reply_topic: str
    created_at: int
    data: dict[str, object]
    state: RequestState = "pending"

@dataclass(frozen=True)
class ReplyRecord:
    agent_id: str
    task_id: str
    origin_id: str
    topic: str
    creator: str
    created_at: int
    received_at: int
    data: dict[str, object]
    matched: bool = False
    duplicate: bool = False
```

- `RequestRecord.state` is derived at read time: `"answered"` when
  `replies/h(agent)/<origin_id>.json` exists, `"pending"` otherwise.
- `ReplyRecord.matched` is derived at read time: `True` when
  `requests/h(agent)/<origin_id>.json` currently exists. It can be `False`
  for a direct message, an uncertain Create, a cancelled/pruned request, or a
  concurrent publication. It is a view of local bookkeeping, not a trust flag.
- `ReplyRecord.duplicate` is derived from the archive directory. It marks a
  conflicting observation, even if the selected answer has since expired.

#### Operations

- **`submit(client, domain, topic, reply_topic, data) -> RequestRecord`:**
  1. Validate `domain`, `topic`, `reply_topic`, and `data` (ensuring
     `data["reply_topic"] == reply_topic` if present, setting
     `payload["reply_topic"] = reply_topic`, and round-tripping through
     `_encode` so caller mutations cannot alter it). `Store.submit` does **not**
     inject `request_id` into `payload`.
  2. Call `response = client.create({"domain": domain, "topic": topic, "data": payload})`.
  3. Validate `origin_id = _task_id(response["id"])`.
  4. Publish `requests/h(agent)/<origin_id>.json` with `publish_new` and verify
     its saved intent. Failure of this publication or intent readback raises
     `SubmissionError(origin_id)`, preserving the known ID and cancellation hint.
  5. Derive the returned record's state from its reply. If reply inspection
     fails after the intent was verified, raise `ReplyStateError(StoreError)`
     with the saved `origin_id` and a neutral diagnostic. The CLI adds the
     `tl wait` recovery command; library applications choose their own guidance.
- **`_save_reply(task, domain, reply_topic) -> ReplyRecord`:**
  - Read `origin_id = _task_id(task.get("origin_id"))` and
    `task_id = _task_id(task.get("id"))`.
  - Publish to `replies/h(agent)/<origin_id>.json` via `publish_new`.
  - On `"exists"`, read and validate the existing `ReplyRecord`, verifying
    `previous.origin_id == record.origin_id`, and return `previous` so `_drain`
    deletes the redelivered server copy. Before returning, compare canonical
    stable content and archive any difference using `publish_new`. An existing
    archive must be readable and match its expected origin/content fingerprint
    before acknowledgement. Failure leaves the server task unacknowledged.
- **`_drain(..., origin_id: str | None = None, ...)`:**
  - Stops early when `origin_id is not None and saved.origin_id == origin_id`.
- **`wait(client, origin_id, timeout_seconds, ...)`:**
  - Validates `origin_id` with `_task_id`, checks `self.reply(origin_id)`
    first, loads `self._required_request(origin_id)`, and drains `reply_topic`
    until `self.reply(origin_id)` exists or `deadline` passes.
- **`cancel(client, origin_id) -> CancelResult`:**
  - Loads `record = self._required_request(origin_id)`.
  - Calls `client.cancel({"domain": record.domain, "topic": record.topic, "origin_id": origin_id})`.
  - On `NotFound`, leaves the local request record intact (if `Consume` already
    ran, the reply may already be in flight) and returns `"not_found"`.
  - On success, unlinks `requests/h(agent)/<origin_id>.json` and returns
    `"cancelled"`.
- **`prune() -> int`:**
  - Removes selected and archived replies where their own `received_at < cutoff`.
    Each archive retains its first receipt time across redelivery and never
    replaces or extends the selected answer's retention.
  - Removes `requests/h(agent)/<origin_id>.json` where `created_at < cutoff`
    **and** `replies/h(agent)/<origin_id>.json` does not exist. (Because reply
    pruning runs first in `prune()`, a request whose reply also aged past 7
    days is removed in the same pass; keeping a request while its selected reply is
    retained avoids routinely changing `matched` at day 7. Concurrent pruning,
    cancellation or publication can still change this derived flag; lookup by
    origin remains independent of the request record.)
  - Cleans up orphaned publication temporaries and expired `.prune-<day>.json`
    markers as before, including temporaries in `duplicates/`.

### 5.3 `worker.py`

- Delete `_correlation(task)`.
- `EchoHandler.reply` returns `[{"topic": topic, "data": outcome}]`.
- `WorkerLoop._finish` on failed/rejected/abandoned tasks with a valid
  `reply_topic` sends `[{"topic": reply_topic, "data": cast(p.Data, outcome)}]`;
  the server's `Consume` copies `task["origin_id"]` onto that failure reply.
- **`LeaseEvent(threading.Event)` for mid-run cancellation vs. local expiry:**
  ```python
  type LeaseLossReason = Literal["not_found", "forbidden", "expired"]

  class LeaseEvent(Event):
      """Signals that the worker no longer holds its lease, and why."""
      reason: LeaseLossReason | None
      revoked: Event
  ```
  - `_Lease` sets `lost` through a single synchronized helper under
    `_Lease._condition` where **first transition wins**:
    - `NotFound` from `Renew` → `reason = "not_found"`, sets `lost.revoked` and
      `lost`.
    - `Forbidden` (or any other non-transport `TaskleaseError` / task-ID
      mismatch) from `Renew` → `reason = "forbidden"`, sets `lost.revoked` and
      `lost`.
    - Local deadline expiry (`_watch` or `remaining() <= 0`) →
      `reason = "expired"`, sets `lost` (`lost.revoked` remains unset).
  - `Handler.run(self, request: Request, lost: LeaseEvent) -> Outcome`:
    because `LeaseEvent` subclasses `threading.Event` and `Protocol` method
    parameters are contravariant, existing handlers typed with `lost: Event`
    continue to pass `mypy --strict`, while handlers that want to abort
    server-cancelled tasks (`lost.revoked` or `lost.reason == "not_found"`)
    without aborting offline cache-warming runs (`lost.reason == "expired"`)
    can type `lost: LeaseEvent`.
  - If a handler raises an `Exception` after `lost` is set (for example when
    unwinding a cancelled subprocess), `WorkerLoop` catches it, sees
    `lease.remaining() <= 0` in `_finish`, skips `Consume` and `<topic>/error`,
    releases capacity, and continues polling.
  - Even if the handler finishes before the next renewal tick, `_consume`
    receives `NotFound` and returns `True` without posting a reply or falling
    back to `<topic>/error`.

### 5.4 `cli/requester.py` and `cli/inbox.py`

- `tl request`:
  - Remove `--request-id` and `--resend`.
  - Require `--topic`, `--reply-topic`, and `--data` (with `--domain` from flag
    or config as before).
  - Output without `--wait`: `{"origin_id": record.origin_id, "state": record.state}`.
  - Output with `--wait` on reply:
    `{"origin_id": record.origin_id, "data": reply.data, "creator": reply.creator}` (exit `0`).
  - Output with `--wait` on timeout:
    `{"origin_id": record.origin_id, "timed_out": True}` (exit `3`).
  - If `--wait` raises after `submit` succeeded, stderr recovery hint prints:
    `Request <origin_id> remains saved. Resume with tl wait --origin-id <origin_id>.`
- `tl wait --origin-id <origin_id> [--timeout 6h] [--wait-seconds N]`
- `tl cancel --origin-id <origin_id> [--topic <topic> [--domain <domain>]]`:
  - Cancels queued, running, or lease-lapsed first-stage tasks.
  - **Store mode (default, `--topic` omitted):** reads `(domain, topic)` from
    `Store(agent_id)` via `store.cancel(api, origin_id)` and deletes the local
    request file on success. Passing `--domain` without `--topic` raises
    `ConfigError`.
  - **Direct stateless mode (`--topic` given):** resolves `domain` from
    `--domain` / `TL_DOMAIN` / `config.domain` and calls
    `api.cancel({"domain": domain, "topic": topic, "origin_id": origin_id})`
    directly without requiring `agent_id` or touching local `Store` files (so
    operators and non-`Store` callers can cancel any first-stage task seen in
    `tl tasks` or `tl monitor` without racing `Own` ID rotation).
  - Output in both modes:
    `{"origin_id": origin_id, "cancelled": result == "cancelled", "state": result}`.
  - On `"not_found"` (exit `1`), stderr notes that the task has already
    finished or been removed (and in store mode notes that the request remains
    saved for `tl wait`).
- `tl inbox [--all] [--prune]`: outputs `ReplyRecord.to_dict()` entries (with
  `origin_id`, `matched`, and `duplicate`).

### 5.5 `testing.py` (`FakeBoard`)

Update the in-memory `FakeBoard` to match `DomainObject` semantics:
- Track `origin_id` and internal `root` (`True` on `create`, preserved on `own`,
  `False` on `consume`).
- Include `origin_id` in `Task` and `TaskSummary`.
- Implement `cancel` (`POST /v1/cancel`) with the creator/operator check,
  `root` check, and `stats_hourly.deleted` increment.

---

## 6. Monitor (`monitor/` & `python/src/tasklease/monitor/`)

- **`monitor/src/model.ts`:**
  - `LocalRequest`: replace `request_id: string`, `task_id: string | null`, and
    `"submission_unknown"` with `origin_id: string` and
    `state: "pending" | "answered"`.
  - `LocalReply`: replace `request_id: string | null` with `origin_id: string`
    and `matched: boolean`, plus `duplicate: boolean` for archived observations.
- **`monitor/src/health.ts`:**
  - Remove `UNKNOWN_SUBMISSION_MS` and the `"submission_unknown"` branch in
    `requestHealth`.
  - Fix the board lookup in `requestHealth` to match by `origin_id`:
    1. Look up `topic?.tasks.find((t) => t.origin_id === request.origin_id)` on
       `request.topic`. A request claimed by a worker now stays matched across
       `Own` ID rotations and displays
       `"Running on <worker> on <identity>, lease <duration> left"` (or
       `"Lease lapsed ..."`).
    2. If not on `request.topic`, look up `<request.topic>/error` (when
       `request.topic` does not already end in `/error`) for
       `errorTopic?.tasks.find((t) => t.origin_id === request.origin_id)`. If
       found, report `level: "critical"` with
       `"Moved to <error_topic>"` instead of falsely reporting
       `"Not on the board: finished, reply on its way"`.
- **`monitor/src/App.svelte` & `monitor/src/mock/snapshot.ts`:**
  - Expanded topic task rows display the full origin alongside a rotated task
    ID so an operator can copy it for correlation or first-stage cancellation.
  - Pending-requests table displays `item.request.origin_id` instead of
    `request_id`.
  - Inbox entries display `reply.origin_id` (with an `"Unmatched"` label when
    `!reply.matched` and a `"Duplicate"` label for archived observations).
    Entries are keyed by agent, origin and saved task ID so observations of
    the same origin expand independently. The unmatched filter checks `!reply.matched`.
  - Update mock snapshot fixtures so rotated `id` values keep their original
    `origin_id` (removing the old mock workaround that copied the rotated `id`
    into `LocalRequest.task_id`), and include an error-topic fallback case.

---

## 7. Downstream impact on FluxShot profiles

Inspecting `fluxshoteditor/scripts/fse_tasklease/` shows two distinct patterns,
both of which become simpler once `data.request_id` is removed:

1. **Verify (and build) worker profile (`Own` → `Consume`):**
   - `VerifyRequest` (`extra="forbid"`), `InvalidRequest`, `VerifyResult`, and
     `new_result` drop `request_id`.
   - `ResultCache` already excludes `request_id` from `cache_key`; `_from_cache`
     stops overwriting `reply["request_id"]` (and strips any legacy
     `request_id` when loading pre-upgrade cached replies).
   - `_task_folder` simplifies from
     `<hash(request_id)>-<hash(task_id)[:12]>-<owned_count>` to
     `<hash(origin_id)>-<owned_count>` because Option A eliminates duplicate
     tasks per request ID.
   - `tl_request.py` uses `record = store.submit(...)` (`record.origin_id`) for
     `wait` and `cancel`.
   - *Mid-run cancellation in `VerifyHandler`:* `VerifyHandler.run` receives
     `lost: LeaseEvent`. Offline lease expiry (`lost.reason == "expired"`) keeps
     `lost.revoked` unset so `verify.py` still finishes into `ResultCache`,
     whereas an explicit server-side `cancel` sets `lost.revoked` (`reason =
     "not_found"`) so `StepRunner` can stop `verify.py` and release the
     machine's `verify_lock` lane early.
2. **Session messages profile (`fse.message/1`, `tl_session.py`):**
   - `tl_session.py listen` drains via `Store.poll` (`Own` → save → `Delete`)
     and exits to wake the session; `tl_session.py answer` runs later in a new
     process via `Store.submit` (`Create`, **not** `Consume`).
   - Therefore, an answer task gets its own fresh `origin_id` (`T_ans != T_msg`).
   - Dropping `data.request_id` and keying `Store` by `origin_id` fixes `#17`
     and deletes `Mailbox.answers` (the second `Store` workaround):
     - Every outgoing and incoming message or answer has a unique `origin_id`
       (`t_01J...`), so questions, answers, and forged answers from other
       machines never collide on disk.
     - `fse.message/1` already carries `in_reply_to` and `conversation` in
       `data` and already authenticates answers via `record.creator == sender.machine`
       and `answer.sender == asked.recipient` (`messages.py`). On hop 0,
       `conversation` in wire JSON can be `null` (meaning the message's own
       `origin_id`), so every message ID and conversation ID is a `TaskId`.
     - `tl_session.py ask` polls `inbox.poll(...)` until `Mailbox` sees a
       non-refused `answer` with `in_reply_to == asked.origin_id`. A forged
       answer from another machine lands in its own `<T_forged>.json` file, is
       marked `Refusal`, and never blocks or overwrites the genuine answer.

---

## 8. Upgrade & compatibility (`0.5.0`)

This requires a coordinated upgrade; old and new correlation conventions do
not interoperate on shared work topics. Extra metadata alone is tolerated by
the old Python client, but old requesters need `data.request_id` echoed and
old worker profiles may require that field in new requests.

1. Pause producers, drain queued/running work and replies with the old clients,
   then stop workers, requesters, listeners and monitors.
2. Deploy the accepted 0.5.0 service bundle with `tl service deploy`.
   SQLite migration 3 runs atomically before requests, backfilling
   `origin_id = id` and `root = 0` on any remaining legacy tasks.
3. Archive/reset old `requests/` and `replies/` directories in every Store
   root, including custom application roots. Keep configuration and tokens.
   There are no legacy parsing branches in the new Store.
4. Upgrade tasklease clients and FluxShot profiles together. New clients
   require origins from the new service; update profile payloads, APIs and
   session conversation handling as described above.
5. Restart workers/listeners, then producers. Verify origin-based replies,
   cancellation and monitor tracking. See [releasing.md](releasing.md) for
   the operator procedure.

---

## 9. Implementation plan

1. **Contract & codegen (`schema/`, `generated/`):**
   - Add `origin_id` to `Task` and `TaskSummary` in `schema/common.json`.
   - Add `schema/ops/cancel.json` and `schema/examples/cancel/{valid,invalid}/*.json`.
   - Update existing task/snapshot fixtures under `schema/examples/` with
     `origin_id`.
   - Run `make codegen` and verify `make codegen-check`.
2. **Service (`service/`):**
   - Add SQLite migration 3 (`origin_id`, `root`, `tasks_root_origin` index) and
     `cancel` to `service/src/domain_do.ts`.
   - Wire `cancel` route in `service/src/index.ts`.
   - Add Vitest coverage in `service/test/`: `origin_id` propagation across
     `Create` → `Own` → `Renew` → `Consume`, `cancel` on queued / owned /
     lapsed tasks, `cancel` returning `not_found` after `Consume` (including
     same-topic self-loops and `creator == worker`), `cancel` returning
     `forbidden` for non-creators and `ok` for operators, `stats_hourly.deleted`
     accounting, and SQLite migration backfill from schema version 3 to 4.
3. **Python package (`python/`):**
   - Update `client.py`, `names.py`, `cloudflare/deployer.py`, `store.py`,
     `worker.py` (`LeaseEvent`), `cli/requester.py` (including direct
     `tl cancel --topic`), `cli/inbox.py`, and `testing.py` (`FakeBoard`).
   - Update unit tests in `python/tests/` (`test_store.py`, `test_worker.py`,
     `test_cli.py`, `test_client.py`, `test_testing.py`, `test_monitor.py`,
     `test_names.py`, `test_cloudflare.py`).
4. **Monitor (`monitor/`):**
   - Update `monitor/src/model.ts`, `monitor/src/health.ts` (`origin_id` join on
     both `<topic>` and `<topic>/error`), `monitor/src/App.svelte`,
     `monitor/src/mock/snapshot.ts`, and `monitor/src/*.test.ts`.
5. **Documentation & conformance (`docs/`, `conformance/`):**
   - Update `docs/spec.md`, `docs/design.md`, `README.md`, and `CHANGELOG.md`.
   - Update `conformance/` (`test_requester.py`, `test_worker_flow.py`,
     `test_semantics.py`, `test_examples.py`) to test end-to-end `origin_id`
     correlation, forgery isolation on shared reply topics, `LeaseEvent`
     behaviour, and post-`Own` / post-`Consume` cancellation against the live
     Worker.
   - Run `make check` and `make build`.
