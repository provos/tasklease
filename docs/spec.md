# tasklease specification

Draft 3, 2026-11-07.

## 1. Purpose

Agents and workers on different machines need to hand work to each other and
get results back. Examples: an agent on macOS asks for a verification run on
Linux, a packaging agent asks for a Windows build, a developer agent asks an
adviser agent a question.

tasklease is a task board for this. It knows nothing about verification,
builds or advice. Those are *profiles*: conventions for topic names and for
the JSON a task carries. FluxShot Editor's profiles are in
[fluxshot-profiles.md](fluxshot-profiles.md).

Goals:

- One small set of atomic operations that covers request, work, reply and
  hand-off.
- Workers pull work. The service never calls a worker, and nothing has to
  reach a worker machine from outside.
- Correlation is the caller's business. Callers put their own stable IDs in
  the task data. Task IDs are short-lived handles that make the atomic
  operations safe.

Non-goals: push notifications to workers, cancelling a task that is already
being worked on, workflow definitions, scheduling.

## 2. Concepts

**Domain.** Every task belongs to one domain: a separate namespace for
topics, tasks, pollers and blobs. A domain name is lowercase segments of
`[a-z0-9._-]+` joined by `/`, at most 128 characters. The service gives the
name no meaning. A project on GitHub can use its repository
(`github.com/provos/fluxshoteditor`); a team or a home lab can use
`render-farm.example` or `acme/pipeline`. No operation reads or writes across
two domains, and every operation names exactly one. Identities are granted
domains (§3), and each domain has its own Durable Object (§7.1).

**Topic.** A path-like name: segments of `[a-z0-9._-]+` joined by `/`, at
most 128 characters. Examples: `verify/linux`, `verified/mac-a1b2`. A topic
exists while it holds tasks or has recent pollers. Well-known topics declared
at provisioning are always listed, even when empty.

**Task.** A record whose contents never change. `domain`, `topic`, `data`,
`creator`, `created_at`, `owner` and `owned_count` are fixed for as long as
an ID lives. A change of owner retires the ID and posts a copy (Own). The
only field that changes in place is `lease_until`, and only the current owner
can change it (Renew).

| Field | Type | Set by |
|---|---|---|
| `id` | string, unique forever (`t_` + ULID) | server |
| `domain` | domain name | Create / Consume |
| `topic` | topic name | Create / Consume |
| `created_at` | UTC timestamp | server at Create / Consume; copied by Own |
| `lease_until` | UTC timestamp | server: Create, Own, Renew |
| `owned_count` | integer | 0 at Create / Consume; Own adds 1 |
| `creator` | identity name | server at Create / Consume; copied by Own |
| `owner` | `{identity, worker}` of the last Own, or null | server: Own |
| `data` | JSON object, at most 256 KiB serialized | caller |

**Lease.** A task is *available* when `lease_until <= now` and *held*
otherwise. "Now" is always the server's clock. Create posts a task with
`lease_until = now − 5 s`, so a new task is available immediately.

**ID rotation.** Own retires the task's ID and posts a copy under a new ID.
Consume and Delete retire it with no successor. Renew keeps the ID. IDs are
never reused. So the ID a worker holds works as a fencing token: once a lease
expires and another caller owns the task, the old ID is gone, and every
Renew, Consume or Delete with it fails with `not_found`. Only Own changes who
holds a task, so only Own needs a new ID.

**Lineage.** A task's lineage is the chain from its Create (or the Consume
that posted it), through any Owns, to the Consume or Delete that ends it. A
lineage ends at most once.

## 3. Identity and authorization

**Credentials.** Each machine, or each agent host, gets one bearer token at
provisioning. The service stores only the token's SHA-256, in an identity
record:

```json
{
  "name": "linux-1",
  "domains": ["github.com/provos/fluxshoteditor"],
  "scopes": {
    "create": ["verified/*", "built/*", "advised/*", "verify/linux/error"],
    "own":    ["verify/linux", "build/linux"],
    "read":   ["*"]
  },
  "operator": false
}
```

**Worker label.** Several workers can share one machine and one identity.
Each request carries an `X-Worker` header (for example `verify-a`). The label
is self-reported and not authenticated. It is recorded as part of `owner` and
of the poller list, and is used for diagnosis only.

**Scopes.** A scope is a list of topic globs, where `*` matches within one
segment and `**` matches across segments.

| Operation | Needs |
|---|---|
| Create | `create` on the topic |
| Own, Renew | `own` on the topic |
| Consume | `own` on the task's topic and `create` on every new task's topic |
| Read, Tasks List, Topic Pollers | `read` or `own` on the topic |
| Topics List | `read` (filtered to the topics that `read` matches) |
| Delete | see the holder rule |
| Blob put URL | a non-empty `create` list on the domain |
| Blob get URL | a non-empty `read` or `own` list on the domain |
| Topic Declare | operator on the domain |
| Identity Add / Revoke / Rotate / List | the admin token (§7.1), not an identity |

Blobs are scoped to a domain, not to topics: anyone who can read in a domain can
read its blobs.

**Holder rule.**

- Renew and Consume need the caller's identity to equal `owner.identity`.
- Delete is allowed for the owner, or for the creator when the task has never
  been owned (`owner` is null).
- An operator identity may do anything in its domains.

So a requester can cancel its own request until a worker owns it. After that
only the owner, or an operator, can end the lineage. "Never owned" is the
cancellation window, not "not yet consumed": after Own, the ID the requester
holds no longer exists.

## 4. Operations

Transport: `POST https://<host>/v1/<operation>` with a JSON body, an
`Authorization: Bearer <token>` header and an `X-Worker: <label>` header.
Identity operations (§4.13) take the admin token in place of an identity
token, and ignore `X-Worker`. This section is the whole wire protocol.

Success is `200 {"ok": true, ...}`. Failure is
`{"ok": false, "error": {"code": "...", "message": "..."}}` with this status:

| Code | HTTP | Meaning |
|---|---|---|
| `invalid` | 400 | malformed request, bad topic name, bad duration |
| `unauthenticated` | 401 | missing or unknown token |
| `forbidden` | 403 | scope or holder rule refused it |
| `not_found` | 404 | no task with this domain, topic and ID |
| `empty` | 404 | Own found no available task |
| `exists` | 409 | Identity Add for a name already in use |
| `too_large` | 413 | `data` over 256 KiB, more than 16 new tasks, or a blob over 2 GiB |

A refused operation changes nothing. Every check runs before the transaction
writes anything.

### 4.1 Create

`create {domain, topic, data}` → `{id}`

Posts a task with `lease_until = now − 5 s`, `owned_count = 0`,
`owner = null` and `creator` = the caller.

### 4.2 Own

`own {domain, topic, hold_seconds, wait_seconds?}` → `{task}`

Picks the available task in the topic with the oldest `created_at` (ties
broken by ID). A caller cannot choose which task it gets.

Atomically, it retires the task and posts a copy with a new ID,
`lease_until = now + hold_seconds`, `owned_count + 1` and
`owner = {caller, worker label}`. It returns the whole new task, data
included.

`hold_seconds` is between 1 and 86400. If no task is available it returns
`empty`.

Every Own, including one that returns `empty`, updates the caller's entry in
the topic's poller list (§4.9).

`wait_seconds` (0–60, default 0) asks the server to hold the request open
until a task arrives. The server may ignore it and answer at once (§7.3).

### 4.3 Renew

`renew {domain, topic, id, hold_seconds}` → `{task}`

Sets `lease_until = now + hold_seconds` on the live task `id` and returns the
task. The ID and every other field stay the same, so retrying a Renew whose
reply was lost is harmless. A task whose lease has run out can still be
renewed, as long as no one has owned it since, because its ID is still live.

Errors: `not_found` (another caller owned it, or it was consumed or deleted),
`forbidden` (the caller is not the owner).

### 4.4 Read

`read {domain, topic, id}` → `{task}`

Returns the whole task. It takes no lease and changes nothing. Error:
`not_found`.

### 4.5 Delete

`delete {domain, topic, id}` → `{}`

Retires the task with no successor, under the holder rule (§3). Errors:
`not_found`, `forbidden`.

### 4.6 Consume

`consume {domain, topic, id, tasks: [{topic, data}, ...]}` → `{ids: [...]}`

Atomically, it retires `id` and posts each new task as Create would, in the
same domain. Up to 16 new tasks. An empty array ends the lineage with no
follow-up, which is a Delete that respects the fence. The returned `ids` are
in the order of `tasks`.

Errors: `not_found`, `forbidden` (not the owner, or no `create` scope on a
target topic), `too_large`.

### 4.7 Topics List

`topics.list {domain?, glob?}` → `[{domain, topic, available, held, oldest_available_at, declared}]`

Lists topics that hold tasks, have pollers in the last 7 days, or were
declared at provisioning. Results are filtered by the caller's domains and
`read` scope.

### 4.8 Tasks List

`tasks.list {domain, topic, cursor?}` → `{tasks: [{id, state, created_at, lease_until, owned_count, owner, creator}], cursor?}`

Never returns `data`. `state` is `available` or `held`. Pages hold up to 500
tasks, ordered by `created_at`.

### 4.9 Topic Pollers

`topic.pollers {domain, topic}` → `[{identity, worker, last_polled_at, last_owned_at}]`

One entry per (identity, worker label) that has called Own on the topic in
the last 7 days. This answers "is anyone serving `verify/windows`?" without a
registration step. A worker that waits for local capacity before polling
(§6.1) only shows up while it is able to take work. Requesters also use it to
check who sent a reply (§6.3).

### 4.10 Blob Put URL

`blob.put_url {domain, name, size, sha256}` → `{key, url, expires_at}`

The server builds the key as `<D>/<yyyy-mm-dd>/<ULID>/<name>`, where `<D>`
is the domain name with each `/` written as `%2F`, so the domain is always
exactly one key segment. Without that encoding, domain `acme`'s prefix
`acme/` would also be the start of every key in domain `acme/pipeline`. The
caller never supplies a key, so it cannot write outside its domain. `url` is a
presigned R2 PUT, valid for 1 hour, with `size` signed as its
`Content-Length`. The uploader puts `sha256` into the task data next to the
key, and readers check it after download.

- `name` must match `[A-Za-z0-9._-]{1,128}` and not be `.` or `..`: one
  segment, no `/`.
- `size` is between 1 byte and 2 GiB. `sha256` is 64 hex characters.

Errors: `invalid`, `forbidden`, `too_large`.

### 4.11 Blob Get URL

`blob.get_url {domain, key}` → `{url, expires_at}`

Before it signs anything, the server checks that the key's first segment
equals the caller's encoded domain `<D>`, and that the rest has the shape
`blob.put_url` builds. A caller scoped to one domain
cannot sign a URL for another domain's key. `url` is a presigned R2 GET, valid
for 1 hour.

Errors: `invalid` (wrong prefix or shape), `forbidden`, `not_found` (no such
object).

### 4.12 Topic Declare

`topic.declare {domain, topic, declared: true|false}` → `{}`

Adds the topic to, or removes it from, the list Topics List always shows.
Declaring a topic grants nothing; scopes do that.

Errors: `invalid`, `forbidden`.

### 4.13 Identity Add, Revoke, Rotate, List

These operations need the admin token.

`identity.add {name, domains, scopes: {create, own, read}, operator}` → `{name, token}`

Creates an identity and returns its token. This is the only time the token
is shown, and the server keeps only its SHA-256. `name` matches
`[a-z0-9-]{1,64}`. For every glob in `own`, the server adds `<glob>/error` to
`create`, so a worker can always move a broken task out of the way (§6.1).

`identity.revoke {name}` → `{}`. The token stops working at once. Tasks it
owns keep their leases and come back when those expire.

`identity.rotate {name}` → `{token}`. Issues a new token and revokes the old
one.

`identity.list {}` → `[{name, domains, scopes, operator, created_at, last_seen_at}]`.
Never returns tokens or hashes.

Errors: `invalid`, `unauthenticated`, `exists`, `not_found`.

## 5. Delivery semantics

**A result is delivered once, but work may run twice.** Each lineage ends at
most once, because only the live ID can end it and only one caller holds the
live ID. Work runs more than once when a lease expires while the worker is
still busy or after it has crashed. The next owner starts again, and the
first worker's Renew or Consume fails with `not_found`.

**A lost reply, by operation:**

| Operation | What the caller sees | Effect |
|---|---|---|
| Own | timeout; never learns the new ID | the copy stays held until its lease expires, then becomes available with `owned_count` raised |
| Renew | timeout | retry with the same ID; Renew is idempotent |
| Consume | timeout; a retry returns `not_found` | if the retry came before the worker's lease deadline, its Consume succeeded. Otherwise another worker owned the task and will deliver. Either way, delivered once |
| Delete | timeout; a retry returns `not_found` | the task is gone either way |
| Create | timeout; a retry may post a second task | requesters put their own request ID in `data`, and every consumer must tolerate duplicate requests and replies |

**Lease deadline on the client.** A client works out its deadline as the
local monotonic time when it *sent* the Own or Renew, plus `hold_seconds`.
That deadline is always a little early compared with the server's, so
decisions based on it are safe.

**A timeout is not `not_found`.** A network error, a timeout or a 5xx says
nothing about who holds the task. A worker keeps retrying Renew and Consume
until its local lease deadline passes. Only `not_found` (or `forbidden`)
means the task is no longer its own, and only then does it stop working on
it.

**`owned_count`** counts how many times the task has been owned. The service
attaches no policy to it. A worker that sees a count above its limit (for
example 3) should Consume the task into the reply topic with an error, rather
than run it again.

## 6. Usage patterns

### 6.1 Worker loop

```text
LEASE = 600 s; RENEW_EVERY = 180 s; MAX_OWNED = 3
while not stopping:
    wait for local capacity                        # before Own, never after
    task = own(domain, topic, LEASE, wait_seconds=60)
    if empty: release capacity; sleep(poll interval); continue
    start renewer(task.id):
        every RENEW_EVERY: renew(task.id, LEASE)
            network error, timeout, 5xx: retry until the local lease deadline
            not_found or forbidden, or the deadline passed: mark the task lost
    if task.owned_count > MAX_OWNED:
        finish(task, status="abandoned")
    elif (request := validate(task.data)) is invalid:
        finish(task, status="rejected")
    else:
        result = do the work                       # stop early if the task is lost
        finish(task, result)
    stop renewer; release capacity

finish(task, outcome):
    if the task is lost: return                    # someone else owns it now
    if task.data has a well-formed reply topic:
        r = consume(task.id, [{topic: reply_topic, data: reply(task, outcome)}])
        if r is ok or r is not_found: return
        # forbidden, invalid or too_large: the reply topic cannot take it
    consume(task.id, [{topic: f"{topic}/error",
                       data: {original: task.data, outcome: outcome, error: r}}])
```

Because Renew keeps the ID, the renewer and the main loop share one ID that
never changes. They need no mutex.

**Every task ends.** Whatever is wrong with a task (data that does not parse,
a reply topic the worker may not write to, a reply that comes out too large),
`finish` puts it in `<topic>/error`, where the operator sees it. A task that
keeps failing never sits in the work topic forever. The error topic's data
wraps the original data, so it can go just over the limit when the original
is close to it. A worker that gets `too_large` on that Consume sends only the
first 64 KiB of the original, as a string. `tl identity add` gives every identity `create` on
`<topic>/error` for each topic it can `own` (§4.13), so this last Consume is
always allowed.

**Replies stay small.** A reply echoes the request's identifying fields only,
never the whole request data. A request near the size limit would otherwise
make every reply to it `too_large`.

**Capacity first.** A worker claims its local slot (a machine lock, a GPU, a
build lane) *before* calling Own. A worker that owns a task and then waits
for a slot holds the task back from other machines.

### 6.2 Requester

1. Pick a reply topic that no one else uses, for example
   `verified/<agent-id>`. Only this agent's CLI ever owns tasks there.
2. `create` the request. Its data includes the requester's own `request_id`
   and `reply_topic`.
3. The CLI drains the reply topic into a local store, and serves the agent's
   requests from that store (§6.3). The service never needs to know which
   reply belongs to which request.
4. To cancel before a worker has taken it: `delete` the request's task ID
   (allowed while it has never been owned). Once it has been owned,
   cancelling is the worker's decision. A profile may give workers a way to
   see that a request is stale (FluxShot: a PR head that has moved on).

### 6.3 Agent CLI

`tl` is one binary for agents, workers and operators. Agents use it as a
blocking command, and the harness runs it in the background and notifies the
agent when it exits:

```bash
tl request --domain github.com/provos/fluxshoteditor --topic verify/linux \
  --reply-topic verified/mac-a1b2 --data @request.json --wait --timeout 6h
```

It prints the reply's `data` as JSON on stdout. Exit codes: 0 reply
delivered, 3 timed out (the request was left in place, and its ID is printed),
1 service error.

`tl wait --reply-topic … --request-id …` picks up an earlier request.
`tl inbox --reply-topic …` lists every reply in the local store.

**Local reply store.** The CLI drains the agent's reply topic and keeps the
replies on the agent's machine, for example in
`~/.tasklease/replies/<agent-id>/`. Each pass:

1. `own` the reply topic with a short hold (60 s). On `empty`, stop.
2. Write a record to `<name>.json`:
   `{task_id, topic, creator, created_at, received_at, data}`. `creator` and
   `topic` come from the server's task fields, not from `data`.
   - `<name>` is `data.request_id` if it matches `[A-Za-z0-9._-]{1,128}` and
     is not `.` or `..`. Otherwise it is the task ID, and the record can
     never match a `wait`; `tl inbox` shows it as unmatched.
   - Write a temp file in the same directory and fsync it. Then publish it
     with a rename that never replaces an existing file (`link()` then
     `unlink()`, or `renameat2(RENAME_NOREPLACE)`). If the name is taken,
     this is a duplicate or a forgery: the first file stays, and the second
     goes to `<name>.<task_id>.dup.json`.
3. `delete` the task, using the ID that `own` returned.

The agent ID names the store directory, so it must match the same pattern.
`tl` refuses to start with any other agent ID.

The reply is safe on disk before the server copy goes away. If the CLI dies
between steps 2 and 3, the task's lease runs out, the next pass owns it
again, and step 2 finds the name taken. One file per reply, published
atomically, lets several CLI processes for the same agent drain and read at
once with no lock. `wait` checks the store for its `request_id` first, then
drains, then sleeps and repeats.

**Checking who sent a reply.** Any identity with `create` on the reply topic
can post a task carrying someone else's `request_id`. So before `wait`
returns a reply, it checks that the reply's `creator` appears in
`topic.pollers` for the topic the request was posted to. Only identities with
`own` on that topic can appear there. A `verify/macos` request then accepts
a reply only from a machine that serves `verify/macos`. A reply that fails
the check is kept as unmatched, and `wait` exits non-zero with the reason.
The requester needs `read` on its request topics for this check.

The reply's `data` is untrusted input to the agent, especially an adviser's
answer, which is model output. Agents treat it as information, not as
instructions.

Replies to abandoned requests now pile up in the local store, not in the
service. `tl inbox --prune 14d` removes them. The operator CLI cannot see
them.

## 7. Cloudflare implementation

### 7.1 Layout

- **Worker**: HTTP API, token check, scope check. It forwards each operation
  to the domain's Durable Object.
- **Domain Durable Object** (`idFromName(domain)`), with SQLite storage. Every
  operation is one `transactionSync`, and the object runs one request at a
  time, so Own, Renew and Consume are atomic with no further locking. Do not
  use KV: it is eventually consistent, so two workers could own the same task.
- **Registry Durable Object**: identities (token hashes, scopes), declared
  topics. Identity operations (§4.13) write it. They need the admin token,
  which is stored as a Worker secret and held only by the operator. The admin
  token is not an identity and cannot own, create or read tasks.

### 7.2 Storage

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, topic TEXT NOT NULL,
  created_at INTEGER NOT NULL, lease_until INTEGER NOT NULL,
  owned_count INTEGER NOT NULL, creator TEXT NOT NULL,
  owner_identity TEXT, owner_worker TEXT,
  data TEXT NOT NULL
);
CREATE INDEX tasks_pick ON tasks (topic, created_at, id);
CREATE TABLE pollers (
  topic TEXT, identity TEXT, worker TEXT,
  last_polled_at INTEGER, last_owned_at INTEGER,
  PRIMARY KEY (topic, identity, worker)
);
```

Own scans `tasks_pick` for the first row with `lease_until <= now`. Topics
hold tens of tasks, not millions, so a scan is fine.

A daily alarm drops poller rows older than 7 days. The service never deletes
tasks by itself; pruning is the operator's job (§9).

### 7.3 Polling cost

A request held open keeps the Durable Object awake, and it is billed for that
time. With about ten workers polling, it may never sleep. Version 1 answers
`wait_seconds` immediately, and workers poll every 30–120 s. Measure the cost
before turning on held requests.

## 8. Blobs (R2)

Large outputs (packages over 300 MB, verify reports, logs) go to R2. A task
carries the blob's key, never a URL, because URLs expire before a reader may
get to them.

- Bucket `tasklease-blobs`, with keys `<D>/<yyyy-mm-dd>/<ULID>/<name>`,
  always built by the server (§4.10). A lifecycle rule deletes objects after
  14 days.
- Worker request bodies are capped (100 MB on the Free and Pro plans), so
  bytes never pass through the API. Uploads and downloads use the presigned
  URLs from `blob.put_url` and `blob.get_url` (§4.10, §4.11). A single
  presigned PUT covers the 2 GiB limit. A failed upload is retried with a new
  `put_url`, and the half-written key is left for the lifecycle rule.
- Every blob key's first segment is its encoded domain, and `get_url` checks
  that segment, so one domain's tokens never sign URLs for another domain's
  blobs.
- What may be uploaded is a profile decision. FluxShot forbids footage of any
  kind.

## 9. Operator CLI

```bash
tl topics [--domain D] [--glob 'verify/**']
tl tasks D verify/linux
tl read D verify/linux t_01J…
tl delete D verify/linux t_01J…
tl pollers D verify/linux
tl prune D --topic 'verify/**' --lease-older-than 3d
tl move D verify/linux/error t_01J… --to verify/linux
tl identity add linux-1 --domain D --own 'verify/linux,build/linux' --create 'verified/*,built/*'
tl identity rotate linux-1
tl identity revoke linux-1
tl identity list
tl topic declare D verify/linux
```

- `prune` removes tasks whose lease ran out longer ago than the given age and
  which have been owned at least once: abandoned work. With `--all` it also
  removes never-owned tasks.
- `move` re-posts a task elsewhere: a Consume by the operator, who may
  consume any live task (§3), into the target topic with the same data.
- `identity` commands read the admin token from `TL_ADMIN_TOKEN`. Every
  other command uses the operator's identity token. `identity add` prints
  the new token once, and also lists the `<topic>/error` globs it added to
  `create` (§4.13).

## 10. Open questions

1. **Held requests** (§7.3): turn them on only after measuring the cost.
2. **Blob retention**: 14 days, or longer for release builds?
3. **Access**: bearer tokens only, or Cloudflare Access in front as well?
