# tasklease specification

Draft 3, 2026-11-07.

## 1. Purpose

Agents and workers on different machines need to hand work to each other and
get results back. Examples: an agent on macOS asks for a verification run on
Linux, a packaging agent asks for a Windows build, a developer agent asks an
adviser agent a question.

tasklease is a task board for this. It knows nothing about verification,
builds or advice. Those are *profiles*: conventions for topic names and for
the JSON a task carries, defined by each user of the service. FluxShot
Editor's profiles are in its own repository:
[tasklease-profiles.md](https://github.com/provos/fluxshoteditor/blob/master/docs/point-designs/tasklease-profiles.md).

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
`[a-z0-9._-]+` joined by `/`, at most 256 characters. The complete names `.`
and `..` are prohibited. The service gives the
name no meaning. A project on GitHub can use its repository
(`github.com/provos/fluxshoteditor`); a team or a home lab can use
`render-farm.example` or `acme/pipeline`. No operation reads or writes across
two domains. Every operation names exactly one domain, except the identity
operations (§4.13, §4.14), which name none. Identities are granted domains
(§3), and each domain has its own Durable Object (§7.1), which holds all of
that domain's state.

**Topic.** A path-like name: segments of `[a-z0-9._-]+` joined by `/`, at
most 128 characters, not counting a final `/error` segment. The error topic
of `t` is `t/error`, except that a topic already ending in `/error` is its
own error topic. So every valid topic has a valid error topic (§6.1), and
error topics never nest. Examples: `verify/linux`, `verified/mac-a1b2`. A topic
exists while it holds tasks or has recent pollers. Well-known topics declared
by an operator (§4.12) are always listed, even when empty.

**Task.** A record whose contents never change. `domain`, `topic`, `data`,
`creator`, `created_at`, `owner`, `owned_at` and `owned_count` are fixed for as long as
an ID lives. A change of owner retires the ID and posts a copy (Own). The
only field that changes in place is `lease_until`, and only the current owner
can change it (Renew).

| Field | Type | Set by |
|---|---|---|
| `id` | string, unique forever (`t_` + ULID) | server |
| `domain` | domain name | Create / Consume |
| `topic` | topic name | Create / Consume |
| `created_at` | timestamp | server at Create / Consume; copied by Own |
| `lease_until` | timestamp | server: Create, Own, Renew |
| `owned_count` | integer | 0 at Create / Consume; Own adds 1 |
| `creator` | identity name | server at Create / Consume; copied by Own |
| `owner` | `{identity, worker}` of the last Own, or null | server: Own |
| `owned_at` | timestamp of the last Own, or null | server: Own |
| `data` | JSON object, at most 256 KiB serialized | caller |

**Time.** Every timestamp, on the wire and in storage, is an integer number
of milliseconds since the Unix epoch (UTC). Durations in requests are whole
seconds and say so in their names (`hold_seconds`).

**IDs.** An ID is `t_` followed by a ULID: a millisecond time prefix and 80
random bits. IDs only need to be unique. Nothing depends on their order: the
time prefix is there to make logs easier to read. Where the service orders
tasks, it orders by `created_at` and breaks ties by ID, which gives the same
order every time but no meaningful order among tasks created in the same
millisecond. That includes the tasks one Consume posts to the same topic.

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
provisioning. Bearer tokens are the only access control in version 1; there
is no Cloudflare Access or other gateway in front of the API. The service
stores only the token's SHA-256, in an identity record. An identity has at
most 256 domains, and each scope list at most 256 globs:

```json
{
  "name": "linux-1",
  "domains": ["github.com/provos/fluxshoteditor"],
  "scopes": {
    "create": ["verified/*", "built/*", "advised/*",
               "verify/linux/error", "build/linux/error"],
    "own":    ["verify/linux", "build/linux"],
    "read":   ["**"]
  },
  "operator": false
}
```

Identity Add returns `invalid`, before storing an identity, if an automatic
error grant (§4.13) would exceed the 256-character glob limit or a scope
list's 256-entry limit.

**Worker label.** Several workers can share one machine and one identity.
Each request carries an `X-Worker` header (for example `verify-a`): 1 to 256
characters of `[A-Za-z0-9._-]`. The label is self-reported and not
authenticated. It is recorded as part of `owner` and
of the poller list, and is used for diagnosis only.

**Scopes.** A scope is a list of topic globs. A glob is matched against the
whole topic name, segment by segment:

- A literal segment matches only itself.
- `*` inside a segment matches any run of characters within that segment,
  including none: `mac-*` matches `mac-a1b2` and `mac-`. A segment that is
  only `*` matches exactly one whole segment.
- `**` must be a whole segment. It matches one or more whole segments, never
  zero.

So `verify/*` matches `verify/linux` but not `verify` or
`verify/linux/error`; `verify/**` matches both `verify/linux` and
`verify/linux/error` but not `verify`. A glob for a topic and its error
topic is therefore two globs: `verify/linux` and `verify/linux/error`.

A glob is 1 to 256 characters: segments of `[a-z0-9._-]` and `*`, separated
by `/`, with `**` only as a whole segment. A topic is at most 134 characters,
so the limit leaves room for any useful glob.

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
| Identity Whoami | any identity token |
| Monitor Snapshot | `read` or `own` (filtered to the topics they match) |

Blobs are scoped to a domain, not to topics: anyone who can read in a domain can
read its blobs.

**Trust model.** The service authenticates every caller, and the scopes are
the trust boundary. An identity with `create` on a topic is trusted to post
anything to that topic, and an identity with `own` on a topic is trusted to
answer what it takes from there. Clients do not check further who sent a
task. Anyone who needs tighter trust grants narrower scopes.

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
Identity Add, Revoke, Rotate and List (§4.13) take the admin token in place
of an identity token, and ignore `X-Worker`. One request needs no token:
`GET /v1/version` returns `{service_version, protocol}`, so a client can
check that it matches the service before it has credentials. This section is the whole wire protocol.

Success is `200 {"ok": true, ...}`. Failure is
`{"ok": false, "error": {"code": "...", "message": "..."}}` with this status:

| Code | HTTP | Meaning |
|---|---|---|
| `invalid` | 400 | malformed request, bad topic name, bad duration, a name or list over its limit |
| `unauthenticated` | 401 | missing or unknown token |
| `forbidden` | 403 | scope, holder rule, or conditional Delete refused it |
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

`own {domain, topic, hold_seconds}` → `{task}`

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

Own always answers at once. Workers poll (§7.3).

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

`delete {domain, topic, id, lease_before?}` → `{}`

Retires the task with no successor, under the holder rule (§3). Errors:
`not_found`, `forbidden`.

If `lease_before` is supplied (a timestamp), the stored lease must be strictly
older than it; otherwise Delete returns `forbidden` without changing anything.
The holder and cutoff checks share the deletion transaction. `tl prune` uses
this guard so a concurrent Renew cannot turn an abandoned task into live work
between listing and deletion and still have it deleted.

### 4.6 Consume

`consume {domain, topic, id, tasks: [{topic, data}, ...]}` → `{ids: [...]}`

Atomically, it retires `id` and posts each new task as Create would, in the
same domain. Up to 16 new tasks. An empty array ends the lineage with no
follow-up, which is a Delete that respects the fence. The returned `ids` are
in the order of `tasks`.

Errors: `not_found`, `forbidden` (not the owner, or no `create` scope on a
target topic), `too_large`.

### 4.7 Topics List

`topics.list {domain, glob?}` → `[{topic, available, held, oldest_available_at, declared}]`

Lists the topics in one domain that hold tasks, have pollers in the last 7
days, or are declared (§4.12). Results are filtered by the caller's `read`
scope. `domain` is required, so the call goes to exactly one Durable Object.
To cover several domains, a client calls it once per domain from
`identity.whoami` (§4.14).

### 4.8 Tasks List

`tasks.list {domain, topic, cursor?}` → `{tasks: [{id, state, created_at, lease_until, owned_count, owner, owned_at, creator}], cursor?}`

Never returns `data`. `state` is `available` or `held`. Pages hold up to 500
tasks, ordered by `(created_at, id)`.

**Cursor.** `cursor` is an opaque string. The server builds it from the last
task on the page, as base64url of `v1:<created_at>:<id>`, and the next page
returns the tasks that sort after that pair. A response without `cursor` is
the last page. A malformed cursor is `invalid`.

Paging is a best-effort view of a topic that keeps changing. Because Own
copies `created_at`, a task that is owned while a client is paging keeps its
place in the order, even though its ID changes. So a task is never skipped
or repeated, unless its `created_at` equals the cursor's own `created_at` to
the millisecond: then its new ID may sort before or after the cursor. A task
consumed or deleted between pages just disappears. A task created between
pages shows up on a later page.

### 4.9 Topic Pollers

`topic.pollers {domain, topic}` → `[{identity, worker, last_polled_at, last_owned_at}]`

One entry per (identity, worker label) that has called Own on the topic in
the last 7 days. This answers "is anyone serving `verify/windows`?" without a
registration step. A worker that waits for local capacity before polling
(§6.1) only shows up while it is able to take work. `tl request` uses it to
warn when no worker is serving the topic.

### 4.10 Blob Put URL

`blob.put_url {domain, name, size, sha256}` → `{key, url, expires_at}`

The server builds the key as `<D>/<yyyy-mm-dd>/<ULID>/<name>`, where `<D>`
is the domain name with each `/` written as `%2F`, so the domain is always
exactly one key segment. These percent signs are literal bytes in the R2 key
and are encoded again when constructing its S3 URL. Without that encoding,
domain `acme`'s prefix
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
Declaring a topic grants nothing; scopes do that. Declarations are stored in
the domain's own Durable Object, next to its tasks and pollers (§7.2).

Errors: `invalid`, `forbidden`.

### 4.13 Identity Add, Revoke, Rotate, List

These operations need the admin token.

`identity.add {name, domains, scopes: {create, own, read}, operator}` → `{name, token}`

Creates an identity and returns its token. This is the only time the token
is shown, and the server keeps only its SHA-256. `name` matches
`[a-z0-9-]{1,64}`. For every glob in `own`, the server adds `<glob>/error` to
`create`, so a worker can always move a broken task out of the way (§6.1).
This is an append-only expansion of each glob as written. Manually owning
an error topic does not also grant `create` on that topic; a manual Consume
into it needs an explicit create grant.

`identity.revoke {name}` → `{}`. The token stops working within 60 seconds:
the Worker keeps token lookups in memory for up to that long (§7.1). Tasks
the identity owns keep their leases and come back when those expire.

`identity.rotate {name}` → `{token}`. Issues a new token and revokes the old
one, on the same 60-second terms.

`identity.list {}` → `[{name, domains, scopes, operator, created_at, last_seen_at}]`.
Never returns tokens or hashes. `last_seen_at` is accurate to about 60
seconds (§7.1).

Errors: `invalid`, `unauthenticated`, `exists`, `not_found`.

### 4.14 Identity Whoami

`identity.whoami {}` → `{name, domains, scopes, operator}`

Called with an identity token (not the admin token), it returns the caller's
own identity record. The CLI uses it to loop over the caller's domains, and
to report a missing scope before an operation fails with `forbidden`.

Errors: `unauthenticated`.

### 4.15 Monitor Snapshot

`monitor.snapshot {domain, hours?}` → `{domain, generated_at, topics: [...], stats: [...]}`

Everything a dashboard needs about one domain, in one call to one Durable
Object. Only topics the caller may `read` or `own` are included.

- `topics`: for each topic, what Topics List returns for it, plus its
  pollers (as Topic Pollers) and the summaries of its first 100 tasks (as
  Tasks List, no `data`), with `truncated: true` when there are more.
- `stats`: the hourly statistics (below) for the last `hours` hours
  (default 24, at most 720), one row per topic and hour that had activity.
  This includes the current hourly bucket and the preceding `hours - 1`
  buckets. Authorized historical topics remain in statistics even when they
  no longer have tasks, recent pollers or a declaration.

Errors: `invalid`, `forbidden`.

**Hourly statistics.** Each domain keeps one row per topic per hour, the
hour being the timestamp rounded down to a multiple of 3,600,000. The rows
are updated inside the same transactions as the operations they count, so
they are exact, and kept for 30 days.

| Field | Counts |
|---|---|
| `created` | tasks posted to the topic, by Create or by a Consume |
| `owned` | successful Owns |
| `reowned` | Owns of a task that had been owned before (`owned_count` ≥ 1): a lease ran out |
| `empty_polls` | Owns that returned `empty` |
| `consumed`, `deleted` | lineages ended in the topic, by Consume or by Delete |
| `wait_ms_sum`, `wait_ms_max`, `wait_count` | for each first Own: `now − created_at`, how long a task waited for a worker |
| `run_ms_sum`, `run_ms_max`, `run_count` | for each Consume of an owned task: `now − owned_at`, how long the work took |

Averages are `sum / count`. The service keeps no percentiles.

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

Automated workers operate only on non-error work topics. Both the worker
library and `tl worker` refuse topics ending in `/error` before calling Own.
Error topics are handled manually by operators. A non-operator manually
owning an error topic needs an explicit `create` grant to Consume into that
same topic; the automatic glob expansion in §4.13 does not grant it.

```text
LEASE = 600 s; RENEW_EVERY = 180 s; MAX_OWNED = 3
while not stopping:
    wait for local capacity                        # before Own, never after
    task = own(domain, topic, LEASE)
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
    r2 = consume(task.id, [{topic: error_topic(topic),
                            data: {original: task.data, outcome: outcome, error: r}}])
    if r2 is too_large:
        consume(task.id, [{topic: error_topic(topic),
                           data: bounded(task.data, outcome, r)}])
```

Because Renew keeps the ID, the renewer and the main loop share one ID that
never changes; access to that ID needs no mutex. The local deadline and lost
state still need synchronization. A deadline watchdog marks the task lost
even while a network call is blocked, and a late Renew response cannot revive
a locally lost task.

**Every task ends.** Whatever is wrong with a task (data that does not parse,
a reply topic the worker may not write to, a reply that comes out too large),
`finish` puts it in the topic's error topic (`<topic>/error`, §2), where
the operator sees it. A task that
keeps failing never sits in the work topic forever. The error topic's data
wraps the original data and the outcome, so it can go over the limit. A
worker that gets `too_large` there sends the bounded form instead:
`{original, outcome, error, truncated: true}`, where `original` and
`outcome` are the start of each one's JSON text, as strings, cut so that the
whole payload serializes to at most 128 KiB. The cut is measured on the
serialized payload, not on the strings, because escaping in JSON can make a
string several times longer. So the bounded form always fits, whatever the
task or the outcome held. For each non-error work topic an identity can
`own`, the glob expansion in §4.13 grants `create` on `<topic>/error`, so
this automated worker's last Consume is always allowed.

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
   (allowed while it has never been owned). `tl cancel` looks that ID up in
   the local request record (§6.3). Once it has been owned, cancelling is the
   worker's decision. A profile may give workers a way to
   see that a request is stale (FluxShot: a PR head that has moved on).

### 6.3 Agent CLI

`tl` is one binary for agents, workers and operators. Agents use it as a
blocking command, and the harness runs it in the background and notifies the
agent when it exits:

```bash
tl request --domain github.com/provos/fluxshoteditor --topic verify/linux \
  --reply-topic verified/mac-a1b2 --data @request.json --wait --timeout 6h
```

It prints the reply's `data` as JSON on stdout, together with the reply's
`creator` (for example "answered by linux-1"). Exit codes: 0 reply delivered,
3 timed out (the request was left in place, and its `request_id` is printed),
1 service error.

`tl request` generates the `request_id` unless one is given, so request IDs
are always well-formed and unique for the agent.

- `tl wait --request-id …` picks up an earlier request.
- `tl cancel --request-id …` deletes a request no worker has taken yet.
- `tl inbox` lists every reply in the local store, including unmatched ones.

**File names.** The local stores live in the platform's user data folder.
Request IDs and agent IDs never become file or folder names directly: on
the default macOS and Windows file systems `Foo` and `foo` are the same
name, and Windows reserves names such as `CON`. Instead, a file or folder
is named by `h(x)`: the first 32 characters of the lowercase hex SHA-256 of
the ID's UTF-8 bytes. The original IDs are kept inside the records.

**Local request record.** `tl request` saves the record **before** it sends
Create, so a lost reply to Create, or a crash, never leaves a request in the
service that the CLI knows nothing about:

1. Publish `requests/h(agent)/h(request_id).json` with
   `{request_id, domain, topic, reply_topic, created_at, data}`, where
   `data` is the exact task payload sent as Create's `data`, including the
   chosen `request_id` and `reply_topic`.
2. Send Create.
3. Publish `requests/h(agent)/h(request_id).task.json` with `{task_id}`.

`tl wait` reads the reply topic from the record. `tl cancel` reads the
domain and topic from it and the task ID from the `.task.json` file it needs
for Delete. If Delete returns `not_found`, the task was taken or removed;
`tl cancel` cannot distinguish these cases and keeps the record for waiting.

**Local reply store.** The CLI drains the agent's reply topic and keeps the
replies on the agent's machine, in `replies/h(agent)/`. Each pass:

1. `own` the reply topic with a short hold (60 s). On `empty`, stop.
2. Save the record `{task_id, topic, creator, created_at, received_at, data}`.
   `creator` and `topic` come from the server's task fields.
   - If `data.request_id` is a string of 1 to 128 characters, the record is
     saved as `h(request_id).json`.
   - Otherwise the reply cannot be matched to any request, which only a buggy
     worker produces. It is saved as `unmatched/<task_id>.json` (task IDs are
     server-made and always safe as names), and `tl inbox` shows it for a
     person to look at.
   - Write it with `publish_new` (below). If the name is already taken, the
     reply is a duplicate (from a retried Create, §5): it is dropped, and the
     existing file stays.
3. `delete` the task, using the ID that `own` returned.

**`publish_new(path, bytes) → created | exists`.** Every immutable data file
the CLI and worker library publish for other processes to find (replies,
request records, profile caches) goes through this one operation, never
through direct file calls. Its contract, on every platform:

- **Atomic:** a reader sees either no file or the whole file, never part of
  it.
- **Durable:** when it returns `created`, the bytes are on disk.
- **No replace:** if `path` exists, it returns `exists` and changes nothing.
  Of two processes publishing the same path at once, exactly one gets
  `created`.

The implementation differs by platform (POSIX and Windows) and lives in one
module; nothing else in the code knows the difference.

Transient worker stop markers contain no task data: their existence is the
complete signal, and the worker removes them immediately. They use exclusive
empty-file creation without a later path-based flush, which could race that
removal. Stop markers do not promise power-loss persistence. Mutable
configuration uses the explicitly replacing atomic operation in design §5.2.

The agent ID is also part of the agent's reply topic (`verified/<agent-id>`),
so it must be a valid topic segment (§2). `tl` refuses to start with any
other agent ID.

The reply is safe on disk before the server copy goes away. If the CLI dies
between steps 2 and 3, the task's lease runs out, the next pass owns it
again, and step 2 finds the name taken. One file per reply, published
atomically, lets several CLI processes for the same agent drain and read at
once with no lock. `wait` checks the store for `h(request_id).json` first,
then drains, then sleeps and repeats.

The CLI does not second-guess who sent a reply. Any identity with `create` on
the reply topic is trusted to answer (§3). Workers' answers, and especially
an adviser's model output, are information for the agent to weigh, not
instructions to follow.

Replies to abandoned requests now pile up in the local store, not in the
service, and the operator CLI cannot see them. So the CLI prunes the local
store itself: request records, replies and unmatched replies older than 7
days are removed (the same retention as blobs, §8), at most once a day while
draining, or on demand with `tl inbox --prune`.

A request's state is read from the files:

- *submission unknown*: the record exists without its `.task.json` and
  without a reply. Create was sent, but whether it arrived is not known (or
  the CLI stopped before sending it). `tl wait` can still wait: if the
  request arrived, its reply comes to the reply topic like any other.
  `tl cancel` cannot cancel it, since it has no task ID, and says so.
  `tl request --resend <request_id>` posts the saved `data` again with the
  same `request_id`; if both arrive, the second reply is dropped as a
  duplicate. Resend is limited to this state; a pending request already has a
  known task ID, and an answered request needs no new submission.
- *pending*: the record and its `.task.json` exist, with no reply.
- *answered*: the reply exists.

`tl cancel` deletes the record and its `.task.json` after a successful
Delete.

## 7. Cloudflare implementation

### 7.1 Layout

- **Worker**: HTTP API, token check, scope check. It forwards each operation
  to the domain's Durable Object.
- **Domain Durable Object** (`idFromName(domain)`), with SQLite storage. It
  holds all of one domain's state: tasks, pollers and declared topics. Every
  operation is one `transactionSync`, and the object runs one request at a
  time, so Own, Renew and Consume are atomic with no further locking. Do not
  use KV: it is eventually consistent, so two workers could own the same task.
- **Registry Durable Object**: identities only (token hashes, domains,
  scopes), the one kind of state that spans domains. Identity operations
  (§4.13) write it. They need the admin token, which is stored as a Worker
  secret and held only by the operator. The admin token is compared directly
  and never cached. It is not an identity and cannot own, create or read
  tasks.
- **Token cache.** Every request needs its token resolved to an identity, and
  one Durable Object serves one request at a time, so the registry must not
  be asked on every call. Each Worker isolate keeps `sha256(token) → identity`
  in memory for up to 60 seconds. A revoked or rotated token therefore keeps
  working for up to 60 seconds (§4.13). An unknown token is not cached, so a
  newly added identity works at once.
- **`last_seen_at`.** A cache miss already calls the registry, so that same
  lookup sets the identity's `last_seen_at`. No other request touches the
  registry. For an identity in active use, `last_seen_at` is accurate to
  about 60 seconds.

### 7.2 Storage

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, topic TEXT NOT NULL,
  created_at INTEGER NOT NULL, lease_until INTEGER NOT NULL,
  owned_count INTEGER NOT NULL, creator TEXT NOT NULL,
  owner_identity TEXT, owner_worker TEXT, owned_at INTEGER,
  data TEXT NOT NULL
);
CREATE INDEX tasks_pick ON tasks (topic, created_at, id, lease_until);
CREATE TABLE pollers (
  topic TEXT, identity TEXT, worker TEXT,
  last_polled_at INTEGER, last_owned_at INTEGER,
  PRIMARY KEY (topic, identity, worker)
);
CREATE TABLE declared_topics (
  topic TEXT PRIMARY KEY, declared_by TEXT NOT NULL, declared_at INTEGER NOT NULL
);
CREATE TABLE stats_hourly (
  topic TEXT, hour INTEGER,
  created INTEGER, owned INTEGER, reowned INTEGER, empty_polls INTEGER,
  consumed INTEGER, deleted INTEGER,
  wait_ms_sum INTEGER, wait_ms_max INTEGER, wait_count INTEGER,
  run_ms_sum INTEGER, run_ms_max INTEGER, run_count INTEGER,
  PRIMARY KEY (topic, hour)
);
```

`topics.list` is one query over the first three tables in one database, and
`monitor.snapshot` reads all four.

Own scans `tasks_pick` for the first row with `lease_until <= now`. The index
includes the lease expiry so held tasks can be filtered before fetching task
payloads. The claim may examine many held entries before finding available
work. Topics List aggregates every task in the domain using the same index
and reuses that aggregate for live topic names. See the README's
[scale guidance](../README.md#scale-guidance) for local measurements and
capacity planning.

A daily alarm drops poller rows older than 7 days and statistics rows older
than 30 days. The service never deletes tasks by itself; pruning is the
operator's job (§9).

### 7.3 Polling cost

Version 1 has no held requests: Own answers at once, and an idle worker polls
every 30–120 seconds. Each poll is one short request, so the Durable Object
can sleep between them.

Holding an Own open until a task arrives would cut the delay before a worker
picks up a task. It is a possible future change, not part of version 1. The
cost to weigh: a request held open keeps the Durable Object awake and billed
for that time, and with about ten workers waiting it may never sleep.

## 8. Blobs (R2)

Large outputs (packages over 300 MB, verify reports, logs) go to R2. A task
carries the blob's key, never a URL, because URLs expire before a reader may
get to them.

- Bucket `tasklease-blobs`, with keys `<D>/<yyyy-mm-dd>/<ULID>/<name>`,
  always built by the server (§4.10). A lifecycle rule deletes objects after
  7 days. A blob is a hand-off between machines, not an archive: anything
  worth keeping longer (a release build, say) is copied elsewhere by whoever
  receives it.
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
tl identity add linux-1 --domain D --own 'verify/linux,build/linux' \
  --create 'verified/*,built/*' --read 'verify/**,build/**'
tl identity add mac-agents --domain D --own 'verified/mac-*,built/mac-*,advised/mac-*' \
  --create 'verify/*,build/*,advise' --read 'verify/*,build/*,advise'
tl identity rotate linux-1
tl identity revoke linux-1
tl identity list
tl whoami
tl monitor [--domain D] [--port 0] [--no-browser]
tl service deploy [--account-id ID] [--name NAME] [--dry-run] …
tl service status
tl topic declare D verify/linux
```

- `topics` without `--domain` calls `identity.whoami` and lists each of the
  caller's domains in turn.
- `service deploy` creates or upgrades the whole service in the operator's
  Cloudflare account from the installed package: the R2 bucket and its
  lifecycle rule, the Worker with its Durable Objects, secrets and address.
  It runs a pinned version of Cloudflare's wrangler, so the deploying
  machine, and only that one, needs Node.js. Its help text says how to sign
  in and how to get each Cloudflare identifier and secret. It prints a new
  admin token once, as soon as it is set. `service status` compares
  the deployed version with the installed one. The implementation design
  describes both.
- `monitor` serves a dashboard of the caller's domains on `127.0.0.1` and
  opens it in the browser. The page shows topics, queued and held tasks,
  pollers, the hourly statistics, and this machine's own pending requests
  and inbox, and refreshes every 30 seconds. `tl` calls
  `monitor.snapshot` on the page's behalf, so the token never reaches the
  browser. The local server answers only requests whose `Host` is
  `127.0.0.1` or `localhost` on its port, serves nothing but the dashboard
  and its read-only snapshot, and reuses a snapshot for up to 25 seconds so
  several open tabs do not multiply the load.

- `prune` removes tasks whose lease ran out longer ago than the given age and
  which have been owned at least once: abandoned work. With `--all` it also
  removes never-owned tasks.
- `move` re-posts a task elsewhere: a Consume by the operator, who may
  consume any live task (§3), into the target topic with the same data.
- `identity add` takes one flag per scope (§3), each a comma-separated list
  of topic globs:
  - `--own`: topics the identity may take tasks from (Own, then Renew and
    Consume). A worker gets its work topics; an agent host gets its reply
    topics, so its CLI can drain them. Each `own` glob also adds `create` on
    `<glob>/error`.
  - `--create`: topics it may post to (Create, and the new tasks of a
    Consume). A worker gets the reply topics; an agent host gets the request
    topics.
  - `--read`: topics it may list and read without taking anything.
  - `--operator`: may do anything in its domains.
- `identity` commands read the admin token from `TL_ADMIN_TOKEN`. Every
  other command uses the operator's identity token. `identity add` prints
  the new token once, and also lists the `<topic>/error` globs it added to
  `create` (§4.13).

## 10. Open questions

None at the moment. Decided:

- **Held requests**: not in version 1; workers poll. A possible future change
  (§7.3).
- **Blob retention**: 7 days (§8).
- **Access**: bearer tokens only (§3).
