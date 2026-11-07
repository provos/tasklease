# FluxShot Editor profiles

Draft 3, 2026-11-07. These build on [spec.md](spec.md).

Domain: `github.com/provos/fluxshoteditor`. FluxShot uses its GitHub
repository as its tasklease domain. That is a naming choice only: the GitHub
repository the workers check out (§3.1) comes from each worker's own
configuration, never from the domain or the task.

## 1. Topics

| Topic | Who owns tasks there | Who creates them |
|---|---|---|
| `verify/linux`, `verify/macos`, `verify/windows` | verify workers on that OS | agents |
| `build/linux`, `build/macos`, `build/windows` | packaging workers on that OS | packaging agent |
| `advise` | adviser workers (one at first, more later) | agents |
| `verified/<agent-id>`, `built/<agent-id>`, `advised/<agent-id>` | the one requesting agent | workers, through Consume |
| `<topic>/error` | the operator | workers, for tasks they cannot answer (spec §6.1) |

**Agent ID.** Each agent session uses a stable ID, for example `mac-a1b2`
(hostname plus a short session hash). The `tl` CLI reads it from
`TL_AGENT_ID`. A session that ends with replies still waiting leaves them for
the operator to prune.

**Machines and workers.** Each machine has one identity. Each worker process
has its own label and its own checkout. A machine may run several verify
workers, several build workers, or both. They share the machine's lock lanes
(§3.2).

**Scopes.** A worker machine's identity gets `own` on its OS's topics
(`verify/linux`, `build/linux`, and `advise` on an adviser machine) and
`create` on the reply topics (`verified/*`, `built/*`, `advised/*`). The
`<topic>/error` grants are added automatically. An agent host's identity
gets `create` on `verify/*`, `build/*` and `advise`, `own` on its own reply
topics, and `read` on `verify/*`, `build/*` and `advise`. It needs that read
access so `tl request` can warn when no worker serves a topic (§7).

Every worker machine is trusted to answer any agent (spec §3, trust model).
Keep each OS's `own` grants on that OS's machines, so a `verify/macos`
request is only ever taken by a macOS machine.

## 2. Common envelope

Every request and reply carries these fields:

```json
{
  "schema": "fse.verify.request/1",
  "request_id": "mac-a1b2-20261107-0042",
  "reply_topic": "verified/mac-a1b2",
  "requested_by": "mac-a1b2",
  "requested_at": "2026-11-07T18:20:00Z"
}
```

Every reply also carries:

```json
{
  "schema": "fse.verify.result/1",
  "request_id": "…",
  "request": { "...": "the request's identifying fields only (see below)" },
  "status": "passed",
  "worker": { "identity": "linux-1", "label": "verify-a", "os": "linux", "arch": "x86_64" },
  "started_at": "…", "finished_at": "…",
  "message": "one line a person can read"
}
```

`request` echoes only the fields that identify the work: `schema`, `pr`,
`sha`, `tier`, `options` and `target`, whichever the request has. It never
echoes `question`, `context`, `history` or `attachments`. An advise request
near the 256 KiB limit would otherwise make every reply to it too large
(spec §6.1).

Statuses shared by all profiles:

| Status | Meaning |
|---|---|
| `rejected` | the request failed validation or a security check; never ran |
| `superseded` | the PR head moved past `sha` (§3.1) |
| `abandoned` | `owned_count` went over the worker's limit; earlier attempts died |
| `timed_out` | the work hit its wall-clock limit |
| `error` | the worker's infrastructure failed (fetch, `uv sync`, upload) |

`rejected` and `abandoned` go to the request's reply topic when it has a
well-formed one the worker may write to. Otherwise, as with any reply the
reply topic refuses, the task goes to `<topic>/error`, wrapped with its
outcome (spec §6.1). Either way, every task ends.

## 3. Code under test (verify and build)

```json
{ "pr": 1234, "sha": "0a0fbef7…40 hex", "allow_superseded": false }
```

### 3.1 Worker checks, in this order

1. Validate the request against its schema. `sha` must be 40 hex characters,
   and `pr` a positive integer.
2. Read the PR from the GitHub API (`gh api repos/provos/fluxshoteditor/pulls/<pr>`):
   - the PR's base repo is the worker's configured repo (never one named in
     the task),
   - `head.repo.full_name` equals the base repo, so forks are refused
     (`rejected`),
   - `head.sha == sha`. If not, and `allow_superseded` is false, consume with
     `superseded`. Pushing again and asking again is how an agent cancels a
     stale verify.
3. `git fetch origin pull/<pr>/head`, then `git checkout --detach <sha>` in
   the worker's own worktree. Then `uv sync`.

The task's data never chooses the repo, a URL, a branch name or command-line
arguments. The worker maps a fixed set of options onto fixed flags.

### 3.2 Local capacity

FluxShot already serializes heavy work on each machine with
`tests.verify_lock`, which has three lanes: `default` for verify.py, `build`
for packaging, and `solve`. A worker claims its lane *before* calling Own, by
running each Own-and-work cycle under the lock:

```bash
uv run python -m tests.verify_lock run -- tl-fse-worker step verify
uv run python -m tests.verify_lock run --lane build -- tl-fse-worker step build
```

`step` calls Own once. On `empty` it exits at once, which releases the lane.
Otherwise it does the work and consumes. A verify.py started inside it is
nested, so it does not wait on its own parent (`HELD_ENV`). Local agents that
queued earlier go first, because the lock's queue is FIFO.

The lock's maximum hold (8 h default lane, 4 h build lane) is the outer
wall-clock limit. A worker killed by it leaves its task to expire and come
back with `owned_count` raised.

## 4. Verify

Topics `verify/<os>`, reply topics `verified/<agent-id>`.

### 4.1 Request: `fse.verify.request/1`

```json
{
  "...": "envelope",
  "pr": 1234, "sha": "…", "allow_superseded": false,
  "tier": "pr",
  "options": { "packaging": false, "display": false, "all_shots": false, "keep_going": false },
  "force": false
}
```

- `tier` is one of `quick`, `pr`, `full`, `live`, `windows-foundation`.
- `options` map one-to-one onto `--packaging`, `--display`, `--all-shots` and
  `--keep-going`. Nothing else reaches the command line.
- **Once per head.** Each machine keeps one result cache, shared by all of
  its verify workers, in a local folder such as
  `~/.tasklease/verify-cache/`. After sending a reply, the worker saves the
  reply there as `<sha>-<tier>-<options digest>.json`. The reply holds the PR,
  the SHA, the result and the report's R2 key, so nothing has to be looked up
  in R2. The worker writes it with the same atomic, no-overwrite save as the
  reply store (spec §6.3), so two workers finishing the same run keep the
  first reply.
  A new request whose SHA, tier and options match a saved reply gets that
  reply back with `"cached": true`, unless `force` is set or the report's
  blob has expired (14 days). This enforces CLAUDE.md's "run `--tier pr` once
  per head" for each OS, because each OS has one machine.

### 4.2 Reply: `fse.verify.result/1`

```json
{
  "...": "envelope",
  "status": "failed",
  "cached": false,
  "verdict": "…",
  "tiers": [ { "tier": "T1", "status": "failed", "counts": { "passed": 4100, "failed": 2 } } ],
  "summary_md": "the first 16 KiB of .verify/summary.md",
  "report_blob": "github.com%2Fprovos%2Ffluxshoteditor/2026-11-07/01J…/verify-report.tar.zst"
}
```

`verdict` and `tiers[*].status` / `counts` come from `.verify/report.json`.
The report blob holds `report.json`, `summary.md` and `logs/`, built from an
allowlist of text file types. Frames, plates and images are never uploaded:
licensed footage must never be redistributed.

Status values add `passed` and `failed` to the shared ones.

### 4.3 Lease

`LEASE = 600 s`, renewed every 180 s, `MAX_OWNED = 3`.

## 5. Build

Topics `build/<os>`, reply topics `built/<agent-id>`.

### 5.1 Request: `fse.build.request/1`

```json
{ "...": "envelope", "pr": 1234, "sha": "…", "allow_superseded": false, "target": "macos-arm64" }
```

`target` is one of `macos-arm64`, `linux-x86_64`, `windows-x86_64`. Today
`scripts/package.py` has only `macos`. The other targets are part of this
contract, and the worker answers `rejected` until they exist.

### 5.2 Reply: `fse.build.result/1`

```json
{
  "...": "envelope",
  "status": "built",
  "artifact": { "key": "…/FluxShot-Editor-macos-arm64-0a0fbef7.zip", "size": 318000000,
                "sha256": "…", "expires_at": "2026-11-21T00:00:00Z" },
  "log_blob": "…/build-log.txt.zst",
  "log_tail": "last 8 KiB of the build log"
}
```

`status` is `built` or `failed`, plus the shared values. On `failed` there is
no artifact, but `log_blob` and `log_tail` are still set. Artifacts are
unsigned for now.

Lease: `LEASE = 600 s`, renewed every 180 s, `MAX_OWNED = 2` (a build that
kills its worker twice will not do better a third time).

## 6. Advise

Topic `advise`, reply topics `advised/<agent-id>`.

### 6.1 Request: `fse.advise.request/1`

```json
{
  "...": "envelope",
  "question": "…",
  "context": { "pr": 1234, "sha": "…", "files": ["src/…"], "notes": "…" },
  "history": [
    { "from": "agent",   "identity": "mac-a1b2", "at": "…", "text": "…" },
    { "from": "adviser", "identity": "linux-1/adviser-a", "at": "…", "text": "…" }
  ],
  "attachments": ["<blob key>"]
}
```

`history` carries the conversation so far, so any adviser can answer a
follow-up. Advisers keep no state between tasks. A conversation is continued
by sending a new request with the last reply appended to `history`.

### 6.2 Reply: `fse.advise.result/1`

```json
{ "...": "envelope", "status": "answered", "answer": "…", "adviser": "linux-1/adviser-a" }
```

### 6.3 Adviser worker

The adviser worker owns a task from `advise`. If `sha` is given, it checks
out that commit read-only. It runs `claude -p` with the question, context and
history, using read-only tools. Then it consumes the task with the answer.
Same lease settings as verify. An adviser never starts a paid job.

## 7. Agent use

From a Claude Code session, the CLI runs in the background, exactly like
`verify.py` today:

```bash
tl request --topic verify/linux --reply-topic verified/$TL_AGENT_ID \
  --data @verify-linux.json --wait --timeout 6h
```

Before posting, `tl request` warns if `topic.pollers` shows no worker that
polled within the last 10 minutes.

## 8. Open questions

1. **Windows lock.** `tests.verify_lock` uses `fcntl.flock`, which does not
   exist on Windows. The Windows workers need a matching lock (for example
   `msvcrt.locking` or a named mutex) before §3.2 works there.
2. **Verifying master.** Verifying master with no PR (a nightly-style run)
   needs a second form of code reference: `{"branch": "master", "sha": …}`,
   allowed when `sha` is reachable from `origin/master`.
3. **Worker code location.** Does `tl-fse-worker` live in tasklease (as a
   generic runner with FluxShot hooks) or in the FluxShot repo (under
   `tools/`)? The lock and the verify flags are FluxShot's, which argues for
   FluxShot.
4. **Posting to the PR.** Should a verify worker also post its result as a
   PR comment? That would need a GitHub token with write access on every
   worker machine.
5. **A second machine for one OS** would keep its own result cache (§4.1),
   so both machines could run the same SHA and tier once each.
