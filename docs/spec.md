# tasklease specification

Draft 2, 2026-11-07.

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

**Repo.** Every task belongs to one repo, named by a normalized key such as
`github.com/provos/fluxshoteditor` (lowercase, no scheme, no `.git`). The repo
partitions the data and filters what each identity may see. Every operation
names exactly one repo.

**Topic.** A path-like name: segments of `[a-z0-9._-]+` joined by `/`, at
most 128 characters. Examples: `verify/linux`, `verified/mac-a1b2`. A topic
exists while it holds tasks or has recent pollers. Well-known topics declared
at provisioning are always listed, even when empty.

**Task.** An immutable record. Nothing about a posted task ever changes.
Every change retires the task and posts a new one.

| Field | Type | Set by |
|---|---|---|
| `id` | string, unique forever (`t_` + ULID) | server |
| `repo` | repo key | Create / Consume |
| `topic` | topic name | Create / Consume |
| `created_at` | UTC timestamp | server at Create / Consume; copied by Own and Renew |
| `lease_until` | UTC timestamp | server |
| `owned_count` | integer | 0 at Create / Consume; Own adds 1; Renew copies it |
| `creator` | identity name | server at Create / Consume; copied by Own and Renew |
| `owner` | `{identity, worker}` of the last Own, or null | server |
| `data` | JSON object, at most 256 KiB serialized | caller |

**Lease.** A task is *available* when `lease_until <= now` and *held*
otherwise. "Now" is always the server's clock. Create posts a task with
`lease_until = now − 5 s`, so a new task is available immediately.

**ID rotation.** Own and Renew retire the task's ID and post a copy under a
new ID. Consume and Delete retire it with no successor. IDs are never reused.
So the ID a worker holds works as a fencing token: once a lease expires and
another caller owns the task, the old ID is gone, and every Renew, Consume or
Delete with it fails with `not_found`.

**Lineage.** A task's lineage is the chain from its Create (or the Consume
that posted it), through any Owns and Renews, to the Consume or Delete that
ends it. A lineage ends at most once.

## 3. Identity and authorization

**Credentials.** Each machine, or each agent host, gets one bearer token at
provisioning. The service stores only the token's SHA-256, in an identity
record:

```json
{
  "name": "linux-1",
  "repos": ["github.com/provos/fluxshoteditor"],
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

**Holder rule.**

- Renew and Consume need the caller's identity to equal `owner.identity`.
- Delete is allowed for the owner, or for the creator when the task has never
  been owned (`owner` is null).
- An operator identity may do anything in its repos.

So a requester can cancel its own request until a worker owns it. After that
only the owner, or an operator, can end the lineage. "Never owned" is the
cancellation window, not "not yet consumed": after Own, the ID the requester
holds no longer exists.

## 4. Operations

Transport: `POST https://<host>/v1/<operation>` with a JSON body, an
`Authorization: Bearer <token>` header and an `X-Worker: <label>` header.

Success is `200 {"ok": true, ...}`. Failure is
`{"ok": false, "error": {"code": "...", "message": "..."}}` with this status:

| Code | HTTP | Meaning |
|---|---|---|
| `invalid` | 400 | malformed request, bad topic name, bad duration |
| `unauthenticated` | 401 | missing or unknown token |
| `forbidden` | 403 | scope or holder rule refused it |
| `not_found` | 404 | no task with this repo, topic and ID |
| `empty` | 404 | Own found no available task |
| `too_large` | 413 | `data` over 256 KiB, or more than 16 new tasks |

A refused operation changes nothing. Every check runs before the transaction
writes anything.

### 4.1 Create

`create {repo, topic, data}` → `{id}`

Posts a task with `lease_until = now − 5 s`, `owned_count = 0`,
`owner = null` and `creator` = the caller.

### 4.2 Own

`own {repo, topic, hold_seconds, wait_seconds?}` → `{task}`

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

`renew {repo, topic, id, hold_seconds}` → `{task}`

Atomically, it retires `id` and posts a copy with a new ID and
`lease_until = now + hold_seconds`. `owned_count`, `owner`, `creator`,
`created_at` and `data` are copied unchanged. A task whose lease has run out
can still be renewed, as long as no one has owned it since, because its ID is
still live.

Errors: `not_found` (another caller owned it, or it was deleted), `forbidden`
(the caller is not the owner).

### 4.4 Read

`read {repo, topic, id}` → `{task}`

Returns the whole task. It takes no lease and changes nothing. Error:
`not_found`.

### 4.5 Delete

`delete {repo, topic, id}` → `{}`

Retires the task with no successor, under the holder rule (§3). Errors:
`not_found`, `forbidden`.

### 4.6 Consume

`consume {repo, topic, id, tasks: [{topic, data}, ...]}` → `{ids: [...]}`

Atomically, it retires `id` and posts each new task as Create would, in the
same repo. Up to 16 new tasks. An empty array ends the lineage with no
follow-up, which is a Delete that respects the fence. The returned `ids` are
in the order of `tasks`.

Errors: `not_found`, `forbidden` (not the owner, or no `create` scope on a
target topic), `too_large`.

### 4.7 Topics List

`topics.list {repo?, glob?}` → `[{repo, topic, available, held, oldest_available_at, declared}]`

Lists topics that hold tasks, have pollers in the last 7 days, or were
declared at provisioning. Results are filtered by the caller's repos and
`read` scope.

### 4.8 Tasks List

`tasks.list {repo, topic, cursor?}` → `{tasks: [{id, state, created_at, lease_until, owned_count, owner, creator}], cursor?}`

Never returns `data`. `state` is `available` or `held`. Pages hold up to 500
tasks, ordered by `created_at`.

### 4.9 Topic Pollers

`topic.pollers {repo, topic}` → `[{identity, worker, last_polled_at, last_owned_at}]`

One entry per (identity, worker label) that has called Own on the topic in
the last 7 days. This answers "is anyone serving `verify/windows`?" without a
registration step. A worker that waits for local capacity before polling
(§6.1) only shows up while it is able to take work.

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
| Renew | timeout; its old ID is retired | as for Own: the worker has lost the task, which comes back when the lease expires |
| Consume | timeout; a retry returns `not_found` | if the retry came before the worker's lease deadline, its Consume succeeded. Otherwise another worker owned the task and will deliver. Either way, delivered once |
| Delete | timeout; a retry returns `not_found` | the task is gone either way |
| Create | timeout; a retry may post a second task | requesters put their own request ID in `data`, and every consumer must tolerate duplicate requests and replies |

**Lease deadline on the client.** A client works out its deadline as the
local monotonic time when it *sent* the Own or Renew, plus `hold_seconds`.
That deadline is always a little early compared with the server's, so
decisions based on it are safe.

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
    task = own(repo, topic, LEASE, wait_seconds=60)
    if empty: release capacity; sleep(poll interval); continue
    start renewer: every RENEW_EVERY, task = renew(task.id, LEASE)
                   on not_found: stop the work, it belongs to someone else now
    if task.owned_count > MAX_OWNED:
        consume(task.id, [reply_to(task, status="abandoned")]); continue
    request = validate(task.data)
    if invalid:
        consume(task.id, [reply_to(task, "rejected") if a reply topic is readable
                          else {topic: f"{topic}/error", data: task.data}]); continue
    result = do the work                           # stop if the renewer lost the task
    consume(task.id, [reply_to(task, result)])
    release capacity
```

The renewer and the main loop share the current ID under a mutex. Only the
latest ID is ever used.

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
tl request --repo github.com/provos/fluxshoteditor --topic verify/linux \
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
2. Write the task's data to `<request_id>.json`: write a temp file in the
   same directory, fsync it, then rename it into place. If the file already
   exists, this is a duplicate (§5), and the existing file is kept.
3. `delete` the task, using the ID that `own` returned.

The reply is safe on disk before the server copy goes away. If the CLI dies
between steps 2 and 3, the task's lease runs out, the next pass owns it
again, and step 2 sees the file already there. One file per reply, published
by an atomic rename, lets several CLI processes for the same agent drain and
read at once with no lock. `wait` checks the store for its `request_id`
first, then drains, then sleeps and repeats.

Replies to abandoned requests now pile up in the local store, not in the
service. `tl inbox --prune 14d` removes them. The operator CLI cannot see
them.

## 7. Cloudflare implementation

### 7.1 Layout

- **Worker**: HTTP API, token check, scope check. It forwards each operation
  to the repo's Durable Object.
- **Repo Durable Object** (`idFromName(repo key)`), with SQLite storage. Every
  operation is one `transactionSync`, and the object runs one request at a
  time, so Own, Renew and Consume are atomic with no further locking. Do not
  use KV: it is eventually consistent, so two workers could own the same task.
- **Registry Durable Object**: identities (token hashes, scopes), declared
  topics. The operator CLI writes it with an admin token stored as a Worker
  secret.

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

- Bucket `tasklease-blobs`, with keys `<repo>/<yyyy-mm-dd>/<ulid>/<name>`. A
  lifecycle rule deletes objects after 14 days.
- Worker request bodies are capped (100 MB on the Free and Pro plans), so
  uploads bypass the API. `blob.put_url {repo, name, size, sha256}` returns
  `{key, url}`: a presigned S3 PUT to R2, valid for 1 hour. Large files may
  use a multipart upload to recover from a broken connection.
- `blob.get_url {repo, key}` returns a presigned GET, valid for 1 hour.
- `put_url` needs `create` scope on some topic in the repo. `get_url` needs
  `read`.
- What may be uploaded is a profile decision. FluxShot forbids footage of any
  kind.

## 9. Operator CLI

```bash
tl topics [--repo R] [--glob 'verify/**']
tl tasks R verify/linux
tl read R verify/linux t_01J…
tl delete R verify/linux t_01J…
tl pollers R verify/linux
tl prune R --topic 'verify/**' --lease-older-than 3d
tl move R verify/linux/error t_01J… --to verify/linux
tl identity add linux-1 --repo R --own 'verify/linux,build/linux' --create 'verified/*,built/*'
tl identity revoke linux-1
tl topic declare R verify/linux
```

- `prune` removes tasks whose lease ran out longer ago than the given age and
  which have been owned at least once: abandoned work. With `--all` it also
  removes never-owned tasks.
- `move` re-posts a task elsewhere: an operator Own, then a Consume into the
  target topic.

## 10. Open questions

1. **Held requests** (§7.3): turn them on only after measuring the cost.
2. **Blob retention**: 14 days, or longer for release builds?
3. **Access**: bearer tokens only, or Cloudflare Access in front as well?
