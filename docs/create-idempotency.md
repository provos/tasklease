# Idempotent task creation (`create.idempotency_key`)

Implementation design, 2026-11-15. Target release: `0.5.0` (rolled into the
held `0.5.0` release prior to publication). Companion to
[origin-id.md](origin-id.md); updates [spec.md](spec.md), [design.md](design.md),
[releasing.md](releasing.md), and [CHANGELOG.md](../CHANGELOG.md).

## 1. Motivation

Carrying server-minted `origin_id` (`0.5.0`, [origin-id.md](origin-id.md))
eliminates reply-slot forgery, enables post-`Own` first-stage cancellation, and
isolates local `Store` correlation across retries (`T1` and `T2` never overwrite
each other on disk). However, `Create` itself remains non-idempotent on the
wire:

1. **Duplicate work after uncertain `Create`.** If `POST /v1/create` commits in
   the Durable Object and the HTTP response is lost (read timeout, connection
   reset, edge `502`/`503`/`504`), or if the caller crashes before writing
   `requests/h(agent)/<origin_id>.json`, retrying `Create` inserts a second root
   task with a fresh `origin_id`. Both tasks execute. For expensive worker
   lanes (such as multi-minute `verify` or `build` jobs holding a machine lock),
   preventing duplicate task creation at the protocol layer avoids wasted
   compute and avoids bespoke deduplication state in every worker profile.
2. **Why atomic "work + receipt" tasks are the wrong direction.** Having
   `Create` atomically post both the work task and a receipt task on a caller
   topic appears to reuse existing queue primitives, but breaks down on both
   correctness and complexity:
   - _In-flight duplicate race:_ polling `Own` on a receipt topic and observing
     `empty` only proves the first `Create` had not committed _yet_. If the
     first `Create` was stalled in flight (slow upload, TCP retransmit, cold
     Worker/Durable Object hop) and commits milliseconds after `Own` returns
     `empty`, retrying `Create` still inserts a duplicate work task.
   - _Client nonces and two-phase local state return:_ when `Create`'s response
     is lost, the caller does not know `origin_id` yet, and `Own` claims FIFO
     rather than by ID. Distinguishing concurrent submissions for the same
     `agent_id` requires pre-`Create` client nonces and shared local receipt
     draining—reintroducing the `0.4.x` `submission_unknown` machinery removed
     in `0.5.0`—or per-request receipt topics that pollute `topics.list`,
     `stats_hourly`, and `tl monitor` for 7–30 days.
   - _Happy-path write amplification and scope coupling:_ every normal `200 OK`
     `Create` leaves a live receipt task that requires an extra `Delete` (or
     `Own` + `Delete`) round-trip, and every producer needs `create` + `own`
     grants on a receipt topic in addition to `create` on the work topic.
3. **Why general multi-task `Create` is out of scope.** Atomic multi-task root
   submission (`create.batch`) is an independent scatter-gather/throughput
   feature: each root task in a batch would still require its own independent
   `origin_id` (`root = 1`) so first-stage `Cancel` and `Store` 1-to-1 reply
   slots remain unambiguous, while mid-pipeline fan-out is already covered by
   `Consume`. It is strictly additive and unnecessary for `0.5.0`.

Because every domain in Tasklease is already serialized inside a single-threaded
SQLite Durable Object (`DomainObject` using `ctx.storage.transactionSync` with a
daily cleanup `alarm()`), an optional `idempotency_key` on `Create` solves
at-most-once task creation directly in SQLite with zero happy-path extra
round-trips.

---

## 2. Core design decisions

1. **Optional `idempotency_key` on `Create`.**
   - `create {domain, topic, data, idempotency_key?} -> {id}`
   - Omitting `idempotency_key` preserves current behavior without key-table
     reads or writes: every call mints a new task and `origin_id`.
   - Keys are canonical lowercase UUIDv7 strings. Generate one fresh key per
     logical submission and reuse it across retries. Supplying a key requests
     server-enforced idempotency for 7 days from the first accepted `Create`.
2. **Scoped to `(domain, creator, idempotency_key)`.**
   - Deduplication lives inside the target domain's `DomainObject` keyed by
     `(caller.identity, idempotency_key)` (including operator identities, each
     of which authenticates with its own `IdentityName`).
   - Token rotation preserves the namespace because the creator is the
     identity name. Different identity names have independent key namespaces.
   - One identity cannot claim or look up another identity's keys or recover
     its origins by replaying them.
3. **The first accepted request wins.**
   - Each live key record stores only `(creator, key, origin_id, created_at)`;
     the domain is implicit in its `DomainObject`.
   - For a valid, currently authorized request, replaying the same
     `(creator, idempotency_key)` within 7 days returns
     `200 {"ok": true, "id": origin_id}` without inserting a task, incrementing
     `stats_hourly.created`, extending `created_at`, or waking parked `Own`
     waiters.
   - A replay ignores the new request's `topic` and `data` after normal
     validation and authorization. It leaves the original task unchanged and
     returns its original origin. There is no payload fingerprint,
     canonicalization, or content-conflict error. Callers are responsible for
     reusing a key only for retries of the same logical submission; intentional
     reuse for different work has first-request-wins behavior.
   - Refused requests (`400 invalid`, `401 unauthenticated`, `403 forbidden`,
     `413 too_large`) fail before `create_keys` is written and never claim a
     key.
4. **Operation-level idempotency across the full task lifecycle.**
   - `create_keys` stores `origin_id` directly and keeps a fixed 7-day TTL
     (`created_at >= now - 7 * DAY`, matching `pollers`, R2 blob expiry, and
     local `Store` retention).
   - Within that 7-day window, an authorized, valid replay returns the original
     `id` (`origin_id`), even if the created task has already been `Own`ed
     (rotating its fencing `id`), `Consume`d into downstream/reply tasks,
     `Delete`d, or `Cancel`led:
     - If a fast worker already owned and answered the request while the
       initial `Create` HTTP response was lost, the retry recovers `origin_id`,
       publishes `requests/h(agent)/<origin_id>.json`, and drains the waiting
       reply cleanly.
     - If an operator or another handle concurrently `Cancel`led the task while
       a retry was in flight, the retry cannot accidentally resurrect a new live
       task.
     - Intentionally re-running cancelled or completed work uses a new
       `idempotency_key` (or omits `idempotency_key`).
   - After the window expires, reusing a key may create a new task, even if the
     original task is still live. This bounds duplicate creation within the
     retention window; lease expiry and redelivery can still cause repeated
     execution of one task.
   - UUIDv7's time prefix improves insertion locality within each creator's
     `(creator, key)` index range. Its timestamp is never used for expiry or
     authorization: the service records its own acceptance time. No strict
     ordering within a millisecond or across client clocks is required.
5. **Single-shot `Client` and `Store.submit`; explicit caller retry.**
   - Each `Client.create` or `Store.submit` call makes one Create attempt and
     never hides retries or multi-minute sleeps. A bounded version probe may
     explain a schema refusal, as with existing long-poll diagnostics.
   - `Store.submit(..., *, idempotency_key: str | None = None)` and
     `tl request --idempotency-key <key>` forward the key and make local intent
     publication replay-safe: if `requests/h(agent)/<origin_id>.json` already
     exists from an earlier attempt that crashed after saving locally,
     a keyed `Store.submit` returns the saved request with its original intent,
     `created_at`, and current reply state. It checks that the saved domain
     matches the requested domain, while ignoring changes to the other intent
     fields.
   - `tasklease.names.new_idempotency_key()` generates a fresh UUIDv7. Callers
     retain it across transport retries, and persist it before the first
     Create if they need recovery across process crashes. Minting another key
     on retry creates independent work. The key never chooses an origin or a
     reply storage path; `origin_id` remains server-minted.

---

## 3. Protocol & schema changes (`schema/`)

### 3.1 `schema/common.json`

Add `IdempotencyKey` to `$defs`:

```json
"IdempotencyKey": {
  "title": "IdempotencyKey",
  "type": "string",
  "minLength": 36,
  "maxLength": 36,
  "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\\s\\S])"
}
```

- The wire form is a lowercase, hyphenated UUIDv7, with version `7` and RFC
  variant bits, for example `017f22e2-79b0-7cc3-98c4-dc0c0c07398f`. Require this
  canonical text rather than normalizing alternate spellings. The pattern
  uses `(?![\s\S])` like all other contract patterns.
- The UUIDv7 layout follows [RFC 9562 §5.7](https://www.rfc-editor.org/rfc/rfc9562.html#section-5.7).
  Clients may generate keys using another conforming UUIDv7 implementation.
  The service treats the key as an opaque lookup value after format validation.
- Keys are safe to show in CLI recovery hints and are never used in filesystem
  paths. They are identifiers, not authorization capabilities.

### 3.2 `schema/ops/create.json`

Add optional `idempotency_key` to `CreateRequest`:

```json
"Request": {
  "title": "CreateRequest",
  "type": "object",
  "properties": {
    "domain": {
      "$ref": "https://tasklease.dev/schema/common.json#/$defs/DomainName"
    },
    "topic": {
      "$ref": "https://tasklease.dev/schema/common.json#/$defs/TopicName"
    },
    "data": {
      "$ref": "https://tasklease.dev/schema/common.json#/$defs/Data"
    },
    "idempotency_key": {
      "$ref": "https://tasklease.dev/schema/common.json#/$defs/IdempotencyKey"
    }
  },
  "required": ["domain", "topic", "data"],
  "additionalProperties": false
}
```

`CreateResponse` (`{"ok": true, "id": TaskId}`) is unchanged.

### 3.3 Examples & codegen

- Add valid fixtures under `schema/examples/create/valid/` (omitted key and
  canonical UUIDv7 keys covering each valid variant nibble).
- Add invalid fixtures under `schema/examples/create/invalid/` (empty string,
  wrong length, whitespace/newline suffix, non-string key, uppercase text,
  missing hyphens, braces/URN form, another UUID version, or invalid variant).
- Run `make codegen` to update `service/src/generated/{protocol.ts,requests.ts,
validators.js,validators.d.ts}` and
  `python/src/tasklease/generated/protocol.py`.

---

## 4. Cloudflare service (`service/`)

### 4.1 SQLite schema & migration (`service/src/domain_do.ts`)

Append Migration 4 (`migrations[3]`):

```sql
CREATE TABLE create_keys (
  creator TEXT NOT NULL,
  key TEXT NOT NULL,
  origin_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (creator, key)
);
CREATE INDEX create_keys_expiry ON create_keys (created_at);
```

- Appending Migration 4 (rather than editing Migration 3) keeps upgrades safe
  from every prior schema version: `DomainObject`'s constructor runs all pending
  migrations inside one `ctx.storage.transactionSync` under
  `blockConcurrencyWhile`, advancing `0.4.x` Durable Objects (`version = 3`)
  atomically through `3 -> 5` and any local development state at `version = 4`
  through `4 -> 5`.
- Key rows contain no topic or payload and survive after `Consume`, `Delete`,
  or `Cancel` removes the task. Their size is independent of task data size.

### 4.2 `DomainObject.create` & `alarm()`

Look up the key, insert a task on a miss, and record its origin in the same
`ctx.storage.transactionSync` operation. No await separates these steps.
Inside `DomainObject`, serialize the payload only when inserting a task:

```ts
const KEY_RETENTION_MS = 7 * DAY;

interface CreateKeyRow {
  [key: string]: SqlStorageValue;
  origin_id: string;
  created_at: number;
}

async create(
  request: CreateRequest,
  caller: Caller,
): Promise<Result<CreateResponse>> {
  const key = request.idempotency_key;
  let inserted = false;
  const result = await this.operation((now) => {
    if (key !== undefined) {
      const existing = this.ctx.storage.sql
        .exec<CreateKeyRow>(
          `SELECT origin_id, created_at
           FROM create_keys WHERE creator = ? AND key = ?`,
          caller.identity,
          key,
        )
        .toArray()[0];
      if (existing && existing.created_at >= now - KEY_RETENTION_MS) {
        return { ok: true, id: existing.origin_id };
      }
    }
    const id = this.insertTask(
      request.topic,
      JSON.stringify(request.data),
      caller,
      now,
    );
    if (key !== undefined) {
      this.ctx.storage.sql.exec(
        `INSERT OR REPLACE INTO create_keys
         (creator, key, origin_id, created_at)
         VALUES (?, ?, ?, ?)`,
        caller.identity,
        key,
        id,
        now,
      );
    }
    inserted = true;
    return { ok: true, id };
  });
  if (result.ok && inserted) this.notify(request.topic);
  return result;
}
```

- **Concurrency safety:** concurrent `Create` calls with the same
  `(caller.identity, idempotency_key)` serialize through `transactionSync`.
  The first transaction inserts the task and key row; subsequent transactions
  return that origin with `inserted = false`. Only the insertion notifies a
  waiter and updates creation statistics. If concurrent callers supply
  different valid topics or payloads, the first committed request wins.
- **Expiration & cleanup:** `existing.created_at >= now - KEY_RETENTION_MS`
  enforces the exact 7-day boundary at query time even before the next alarm
  fires; an expired key is replaced cleanly via `INSERT OR REPLACE`. In
  `alarm()`, add:
  ```ts
  this.ctx.storage.sql.exec(
    "DELETE FROM create_keys WHERE created_at < ?",
    now - KEY_RETENTION_MS,
  );
  ```

### 4.3 Router (`service/src/index.ts`)

No route changes are needed beyond regenerated `validators.validateCreateRequest`:

1. Bearer authentication (`401 unauthenticated`)
2. Request schema validation including `IdempotencyKey` (`400 invalid`)
3. Domain membership and `allows(caller.scopes, "create", body.topic)` (`403 forbidden`)
4. `dataTooLarge(body.data)` (`413 too_large`)
5. `DomainObject.create(body, caller)` (`200`, for both insertion and replay)

Every replay passes the existing schema, scope, and data-size checks before
looking up its key. Reusing a key does not bypass current authorization. The
original task is neither mutated nor recreated, even when the replay supplies
a different valid topic or payload. Existing serialization and excessive-depth
rejection remain unchanged.

---

## 5. Python runtime, CLI, test double & deployer (`python/`)

### 5.1 `python/src/tasklease/names.py`

Add UUIDv7 validation and a generator compatible with Python 3.12+:

```python
from uuid import UUID

_IDEMPOTENCY_KEY = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def is_idempotency_key(value: object) -> TypeGuard[str]:
    """A canonical lowercase UUIDv7 idempotency key."""
    return isinstance(value, str) and _IDEMPOTENCY_KEY.fullmatch(value) is not None


def new_idempotency_key() -> str:
    """Mint one UUIDv7 for a logical submission and all its retries."""
    milliseconds = time.time_ns() // 1_000_000
    if not 0 <= milliseconds < (1 << 48):
        raise ValueError("Clock is outside UUIDv7's timestamp range")
    value = (
        (milliseconds << 80)
        | (7 << 76)
        | (secrets.randbits(12) << 64)
        | (2 << 62)
        | secrets.randbits(62)
    )
    return str(UUID(int=value))
```

Import `time` alongside the existing `re`, `secrets`, and `TypeGuard` imports.
This uses a 48-bit Unix-millisecond timestamp and 74 cryptographically random
bits, with the UUID version and variant bits set explicitly. No process-local
counter or persisted generator state is needed; ordering within one millisecond
is intentionally unspecified. Python's built-in `uuid.uuid7()` was
[added in 3.14](https://docs.python.org/3.14/library/uuid.html#uuid.uuid7), so
using the layout directly preserves the existing Python 3.12 minimum without
adding a dependency or a version-dependent generation path.

### 5.2 `python/src/tasklease/client.py`

- Update `Client.create` to accept the standard optional per-call `timeout` cap
  (matching `own`, `renew`, `delete`, `consume`, and `cancel`):
  ```python
  def create(
      self, request: p.CreateRequest, *, timeout: float | None = None
  ) -> p.CreateResponse:
  ```
- **Pre-`0.5.0` service diagnostic:** an un-upgraded `0.4.x` Worker rejects
  `idempotency_key` at schema validation (`additionalProperties: false`) with
  `400 {"code": "invalid", "message": "Invalid request body"}` _before_ creating
  any task. When `"idempotency_key" in request` and `self._post` raises
  `Invalid` with `str(error) == _INVALID_BODY`, check `/v1/version` (bounded by
  `_VERSION_TIMEOUT`, matching `_check_wait_support`): if the reported
  `service_version` is below `(0, 5, 0)`, raise:
  ```python
  ProtocolError(
      f"The tasklease service (version {reported}) rejected Create's "
      "idempotency_key; upgrade to tasklease 0.5.0 or later"
  )
  ```

### 5.3 `python/src/tasklease/store.py`

Extend `Store.submit`:

```python
def submit(
    self,
    client: Client,
    domain: str,
    topic: str,
    reply_topic: str,
    data: Mapping[str, object],
    *,
    idempotency_key: str | None = None,
) -> RequestRecord:
```

1. Validate `domain`, `topic`, `reply_topic`, `data`, and (when not `None`)
   `idempotency_key` via `_idempotency_key` (raising `StoreError` locally before
   making any network call).
2. Inject `payload["reply_topic"] = reply_topic` and detach `payload` via
   `_encode` round-trip before calling `client.create`, as today.
3. Build `request: p.CreateRequest = {"domain": domain, "topic": topic, "data":
cast(p.Data, payload)}` and set `request["idempotency_key"] = key` only when
   `idempotency_key is not None`.
4. **Replay-safe local intent publication:** when
   `_publish(self._request_path(origin_id), record._intent())` returns
   `"exists"` for a keyed submission, read and validate the existing request
   record and require `saved.domain == domain`, raising `StoreError` on a
   mismatch. Then return `self._request_state(saved)`. Preserve its original
   domain, topic, reply topic, payload, and `created_at`, and report
   `state="answered"` if the reply has already been drained. Do not compare it
   with the new call's intent or rewrite it: the first saved record wins,
   matching the service's first-accepted-request-wins contract. The domain
   check protects the lookup namespace against a buggy mock or custom
   transport returning another domain's origin. Existing
   missing/corrupt-record handling remains, and unkeyed submissions retain
   their existing collision checks.
5. If there is no local record, save the submitted intent under the recovered
   origin as usual. The service returns only the origin, so callers must retain
   the original arguments for recovery on another machine or after local state
   loss. Reusing a key for unrelated work is caller misuse, not something the
   service or Store attempts to detect.

### 5.4 `python/src/tasklease/cli/requester.py`

Add `--idempotency-key` to `tl request`:

- `request.add_argument("--idempotency-key", help="Deduplicate Create retries for 7 days")`
- Validate `optional_text(args, "idempotency_key")` locally with
  `is_idempotency_key`, raising `ConfigError` on malformed input before touching
  `Store` or the network.
- Pass `idempotency_key=key` into `store.submit(...)`.
- Recovery guidance on failure:
  - If `store.submit` raises `TransportError` and `key is not None`, write to
    stderr before raising:
    `f"Submission outcome is uncertain; retry within 7 days using --idempotency-key={key}."`
  - `SubmissionError` continues to print the stateless `tl cancel` command (and
    when `key is not None`, also notes that retrying with
    `--idempotency-key={key}` within 7 days after fixing local storage saves
    the local record for the existing `origin_id`).

### 5.5 `python/src/tasklease/testing.py` (`FakeBoard`)

Keep `FakeBoard` behaviorally identical to `DomainObject`:

- `_valid_request`: validate `"idempotency_key"` with `is_idempotency_key`.
- Maintain `self._create_keys: dict[tuple[str, str], tuple[str, int]]`
  mapping `(identity, key) -> (origin_id, created_at)` under
  `self._lock`, checked against `now - _WEEK` using the board's injected clock:
  - Live hit → `_success({"ok": True, "id": origin_id})` without calling
    `_insert` or `_wake()`, regardless of the new valid topic or data.
  - Miss or `created_at < now - _WEEK` → `_insert(...)`, store entry, `_wake()`,
    and return `_success({"ok": True, "id": task_id})`.
- Keep normal validation and authorization before this lookup, and preserve
  existing JSON serialization behavior. No canonicalization or hashing changes
  are required. Key expiry uses service acceptance time, never the UUID's
  embedded timestamp.

### 5.6 `python/src/tasklease/cloudflare/deployer.py`

Extend `_probe` during `tl service deploy`:

1. Generate `probe_key = new_idempotency_key()` once and call
   `client.create({"domain": domain, "topic": "check", "data": {"probe": True}, "idempotency_key": probe_key})`.
2. Replay the exact same `client.create(...)` call and verify
   `replayed["id"] == created["id"]`.
3. `Own` + `Consume([])` the task, verifying `owned["task"]["origin_id"] ==
created["id"]`, and verify a follow-up `client.own({"domain": domain,
"topic": "check", "hold_seconds": 30})` raises `Empty` (confirming on live
   Durable Objects that the replay did not enqueue a duplicate task).

---

## 6. Downstream usage patterns (FluxShot profiles)

A requester wrapper (such as `tl_request.py`) mints
`key = new_idempotency_key()` once before its submission retry loop and calls
`store.submit(..., idempotency_key=key)` across transient `TransportError`s.
Retries within the retention window recover the same origin. Separate logical
invocations testing the same `(PR, SHA)` each receive their own UUIDv7 and run
independently.

For recovery across process crashes or duplicate webhook deliveries, persist
the UUIDv7 and original request arguments with the application's submission
record before the first Create. Reload that key on retry rather than generating
another one. A durable mapping from an application event to its submission
record can reuse the UUIDv7 across duplicate deliveries. The protocol key itself
is a fresh UUIDv7, not a hash of the payload or a deterministic application name.

The service recognizes the key without comparing payloads. Callers should
resubmit the original arguments when recovering an operation; changing them
does not update already accepted work. A deliberately new run needs a new key.

---

## 7. Rolling into `0.5.0` & verification plan

Because `0.5.0` publication is currently held prior to PyPI/GitHub Release,
this design rolls directly into `0.5.0` (`python/pyproject.toml` and
`service/src/version.ts` remain `0.5.0`):

1. **Contract & codegen:** update `schema/common.json`, `schema/ops/create.json`,
   and `schema/examples/create/`; run `make codegen` and `make codegen-check`.
2. **Service & tests (`service/`):** implement Migration 4,
   `DomainObject.create` deduplication, and `alarm()`
   cleanup. Add unit and Worker HTTP tests in `service/test/` covering:
   - Migration `3 -> 5` and `4 -> 5` upgrades preserving existing tasks.
   - Exact replay returning `id` without creating a second task, incrementing
     `stats_hourly.created`, or waking extra parked `Own` waiters.
   - A replay with a different valid topic or payload returning the first
     origin, without changing the original task or creating work on the new
     topic. Existing data validation and excessive-depth rejection still apply.
   - Replay after `Own`, `Consume`, `Delete`, and `Cancel` still returning the
     original `origin_id` without resurrecting a task.
   - Non-recording on `400`/`401`/`403`/`413`, including when the rejected call
     would have been a key replay.
   - Cross-identity isolation (same key used by two identities creates two
     independent tasks; neither recovers the other's `origin_id`), plus
     same-identity token rotation preserving the namespace.
   - Concurrent duplicate `Create` calls racing into `DomainObject.create`.
   - Exact 7-day retention boundary (`now - 7 * DAY` live vs `now - 7 * DAY - 1`
     expired, both before and after `alarm()`).
   - UUID timestamps far before or after service time not changing acceptance
     or expiry; only `create_keys.created_at` determines retention.
3. **Python, CLI, `FakeBoard` & conformance (`python/`, `conformance/`):**
   - Implement `names.py`, `client.py`, `store.py`, `cli/requester.py`,
     `testing.py`, and `deployer.py`.
   - Add `test_names.py` cases for canonical UUIDv7 validation, version and
     variant bits, timestamp encoding, random fields, and timestamp ordering
     across different milliseconds. No strict within-millisecond ordering is
     required. Exercise the generator on supported Python 3.12 environments.
   - Add unit tests in `test_client.py` (including `< 0.5.0` version diagnostic
     and `timeout` cap), `test_store.py` (lost-response retry with same key,
     post-save crash replay with advancing clock preserving `saved.created_at`,
     fast-reply-before-replay recovery, and returning the first saved intent
     even when a keyed replay supplies different valid arguments, plus
     rejecting a cross-domain origin returned by a mock/custom transport),
     `test_request_cli.py` (`--idempotency-key` validation, replay, and
     `TransportError` stderr retry hint), `test_testing.py`, and `test_deploy.py`.
   - Add shared cross-language conformance tests in `conformance/` exercising
     replay, first-request-wins behavior with changed valid input,
     post-`Consume` and post-`Cancel` replay, and cross-identity isolation
     identically against both `FakeBoard` and the real Cloudflare Worker.
4. **Documentation & release gate:**
   - Update `docs/spec.md` (§4.1 Create, §6.3 CLI),
     `docs/design.md` (§4 SQLite tables, §5.3 `store.py`),
     `docs/origin-id.md` (§2.3 cross-reference), `docs/releasing.md` (`0.5.0`
     upgrade notes), and `CHANGELOG.md`.
   - Run full `make check`, `make build`, native macOS/Linux/Windows acceptance,
     and live isolated Cloudflare upgrade/deploy acceptance. Prior acceptance
     and rehearsal artifacts describe the earlier candidate and must be
     superseded by evidence for this implementation.
   - The remote `v0.5.0` tag already identifies the earlier held candidate.
     Replacing that pre-publication tag requires an explicit release decision
     after acceptance, followed by a new build-only publishing rehearsal.
     Publication remains on hold until the user authorizes it again.
