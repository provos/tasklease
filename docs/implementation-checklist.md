# Implementation checklist

Baseline: `b09f705`, spec Draft 3 and implementation design Draft 1. The spec
wins when documents disagree. This checklist tracks the generic tasklease
system; FluxShot profiles and integrations belong in the FluxShot repository.

## Phase gates

Every phase requires implementation and relevant checks, a simplify review
covering reuse/quality/efficiency, confirmed fixes and retesting, and a fresh
read-only `gpt-6-astra` review with `high` reasoning and no inherited author
conversation. Substantive review fixes receive a focused re-review. Findings
and verification evidence are recorded in [review-log.md](review-log.md).

- [x] **1. Contract and foundations:** complete JSON Schema contract and
  examples; deterministic TS validators/types and Python TypedDict generation;
  pinned toolchains; strict checks; Workers-runtime and Python test harnesses.
- [x] **2. Service:** registry/auth/scopes; task operations and SQLite
  migrations; fencing and atomicity; pollers, statistics and alarms; R2 signing.
- [x] **3. Client, operator CLI and deployment:** typed client and configuration;
  operator commands; deploy/status with recovery tests; bundled Worker;
  wheel/sdist; cross-language conformance and isolated real deployment.
- [x] **4. Durable requester:** atomic no-replace publication; request/reply
  stores; request/wait/cancel/inbox/resend/pruning; crash and duplicate tests.
- [x] **5. Worker runtime:** capacity-before-Own; renewal/deadlines; handler
  interface; retry and lost-lease handling; bounded error routing; graceful
  stop; generic CLI worker.
- [x] **6. Monitor and operations:** scoped snapshots and statistics;
  read-only local server; Svelte dashboard and packaged assets; platform
  startup/stop templates.
- [x] **7. Complete-system acceptance:** integrated fault conformance; clean
  wheel/sdist installation; packaged deployment and upgrade; OS validation;
  documentation and release tooling; independent whole-system review. Local
  gates and corrected-candidate native Windows/Linux/macOS checks passed after
  publication, fixture, template and dependency-policy corrections.

## Acceptance requiring external access

Keep these separate from local implementation completion. A missing credential
or machine leaves a check unverified, never passed by inference.

- [x] Isolated Cloudflare deployment with a separate name, bucket and config.
- [x] Actual R2 upload/download, signed Content-Length acceptance/rejection.
- [x] Upgrade preserves existing Durable Object state and credentials.
- [x] macOS filesystem/store/worker tests and clean package installation.
- [x] Windows filesystem/store/worker tests and clean package installation.
- [x] Cross-machine request, work, reply and durable inbox flow.
- [x] Public package publication and installation from PyPI: `tasklease 0.1.0`,
  verified on Linux, macOS and Windows.

## Contract clarifications

- Successful `topics.list`, `topic.pollers`, and `identity.list` responses are
  objects containing `ok: true` and, respectively, `topics`, `pollers`, and
  `identities` arrays. This makes the array shorthand in spec §4 consistent
  with its required success envelope. The schemas define the exact wire form.
- `/v1/version` returns `protocol: "v1"` and the package's service version,
  without the POST operations' `ok` envelope.
- `last_seen_at` and poller `last_owned_at` are `null` before their first event.
  Snapshot `truncated` is present as `true` only when its task list was capped.
- Identity Add deduplicates expanded create grants and rejects a final list
  longer than 256 before writing anything, preserving every required error
  grant rather than silently discarding some.
- Per user direction, the complete domain names `.` and `..` are prohibited.
  Other dotted names and dot segments within longer domains remain valid.
  Literal `%2F` in blob keys is encoded again to `%252F` in S3 URLs.
- Globs are at most 256 characters (spec §3). Identity Add rejects an own
  glob whose `/error` grant would exceed that with `invalid`, before writing,
  so a legal identity serializes to at most about 265 KB.
- Delete accepts an optional `lease_before` cutoff, checked atomically with
  deletion. Prune uses it so Renew keeping the ID cannot race the operator
  into deleting active work. Ordinary Delete behavior is unchanged.

## Current state

Phase 1 accepted and checkpointed at `776d43d` after simplify, independent
Astra high review, both confirmed review fixes, and a clean focused re-review.
Phase 2 is accepted after simplify, independent Astra high review, resource
boundary fixes and clean focused re-reviews. Phase 3 implementation is
accepted after simplify, recovery fixes and a clean independent Astra
re-review. Phase 4 is accepted after simplify, independent Astra high review
and its clean focused re-review. Phase 5 is accepted after clean simplify and
independent Astra high review. Phase 6 is accepted after simplify, native Mac
connection-admission correction and clean independent Astra focused review.
Phase 7 is accepted after correcting the initial native Windows failures,
three simplify passes, fresh independent Astra high review and a clean focused
re-review. The same reviewed source/wheel candidate passed clean installed-wheel
acceptance on Windows (678 passed, six expected skips), Linux and macOS (681
passed and three Windows-only skips each), including no-Node probes. Native
Windows also passed both Task Scheduler COM parsers, all 188 Workers tests,
asset builds, a clean npm audit and all 302 locked dependency age/source checks.
The installed wheel
bootstrapped the isolated
`tasklease-acceptance-90d25ae` Worker and bucket. Live R2 round trip, SHA-256
and signed Content-Length acceptance/rejection passed with the provided
bucket-scoped credentials. Different-bundle upgrade, Linux/macOS clean-wheel
native checks and the cross-machine request/reply flow passed. Corrected native
results and artifact identities are recorded in the review log.
`0.1.0` is published on PyPI from `v0.1.0` at `c373fbf`. Fresh PyPI
installations passed CLI/resource probes and native runtime tests on Linux,
macOS and Windows. Deployment from the PyPI installation and an isolated
state-preserving upgrade passed real Cloudflare/R2 checks. See
[the publication evidence](review-log.md#first-pypi-publication-2026-11-08).

The `0.2.0` candidate at `8e30da0` passed clean install/check/build
gates and native macOS/Linux/Windows installed-wheel acceptance, including the
FluxShot review fixes for cooperative blob deadlines, safe diagnostics, public
fake controls and second-signal cleanup. macOS and Linux each passed 912 tests
with three expected skips; Windows passed 907 with eight expected OS skips on
its first full attempt, including both native Task Scheduler parser modes.
The same candidate passed an isolated upgrade from published `0.1.0`, real R2
transfers with explicit helper budgets and a macOS-to-Linux durable request/reply
flow. The [acceptance summary](acceptance/0.2.0.json) supersedes the earlier
`d0191c4` qualification and records exact artifacts, platform details, skips,
preserved Cloudflare preflight/deployment failures and cleanup. The temporary
Worker and identities were removed; the existing bucket lifecycle was retained,
and Windows was restored to its original suspended state.

`0.2.0` is published on PyPI from `v0.2.0` at squash merge `a0bff8d` of PR #8,
with finalized release notes and a matching GitHub Release. Clean release gates,
build-only rehearsal and trusted publication succeeded. Fresh PyPI installations
passed CLI/resource/no-Node probes and native runtime suites on macOS and Linux
(912 passed, three expected skips each) and Windows (907 passed, eight expected
skips). The exact PyPI-installed package passed isolated Cloudflare deployment,
task operations, signed-size rejection and real R2/public-helper transfers;
temporary resources were cleaned up. The Windows idle-pause setting and suspended
state were restored. Published artifact hashes and workflow/run evidence are in
[the publication summary](acceptance/0.2.0-publication.json) and
[review log](review-log.md#020-publication-2026-11-09).
