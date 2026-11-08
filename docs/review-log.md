# Implementation review log

This log records phase gates, findings, dispositions, checks and material
limitations. An independent review uses a new `gpt-6-astra` agent at `high`
effort with no inherited implementation conversation. Reviewers inspect the
requirements, code and tests directly and make no edits.

## Phase 1 — contract and foundations

Status: accepted; simplify and independent Astra high review gates passed.

- Baseline: `b09f705`.
- Working branch: `codex/implement-tasklease`.
- Ownership: protocol/codegen, TS/tooling, Python/tooling in parallel;
  supervisor owns integration and this log.
- Clarification: list operations use named arrays inside the common success
  object; see implementation checklist.
- Environment: Node 24.13.1 and uv 0.12.8 available. The npm safety wrapper
  initially failed to bind its local scanner socket in the sandbox; package
  operations may need sandbox escalation. No safety checks have been disabled.
- Simplify: three independent review-only passes (reuse, quality, efficiency)
  found no material simplification or confirmed defect. Quality recommended
  exercising compiled validators in Workers, which was added and passed.
- Independent Astra high review (`phase1_astra`, fresh `gpt-6-astra`, high):
  independently reran the full checks and monitor build. Found P2 empty TS
  request interfaces accepting primitives/arrays and P3 root Node engines
  including unsupported Node releases. Supervisor independently reproduced
  both against the compiler and installed package metadata. Closed-object
  codegen now emits `Record<string, never>` with positive and nine negative
  assignment fixtures; Node range is now `^22.12.0 || >=24`, including lockfile
  metadata. Focused Astra high re-review independently reproduced rejection of
  invalid assignments, verified engines, passed codegen/examples/strict TS,
  and found both issues resolved with no new material findings.
- Verification: supervisor `make check` passed (208 examples against both
  schema and standalone validators; 211 Python tests; strict TS/mypy,
  Ruff/ESLint/Prettier and Svelte checks). Final codegen drift/type fixtures
  passed. Worker dry-run and monitor production build passed. After adding the
  runtime validator check, strict TS and all three Workers-runtime tests passed.
- Platform references retrieved for implementation:
  [SQLite transactionSync](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/),
  [current Vitest plugin setup](https://developers.cloudflare.com/workers/testing/vitest-integration/write-your-first-test/),
  [DO runtime tests](https://developers.cloudflare.com/durable-objects/examples/testing-with-durable-objects/),
  [R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).
- Cloudflare account/token and R2 credential environment variables are unset;
  an existing Wrangler login has not yet been checked. Live acceptance remains
  pending.

## Phase 2 — service

Status: accepted; simplify and independent Astra high review gates passed.

- Scope: DomainObject operations, registry/auth/scopes/router, R2 signing,
  statistics and alarms, plus runtime behavioral tests.
- Implementation discovered that standalone `.`/`..` domains normalize away
  in S3 URLs. User directed that these complete domain names be prohibited.
  Spec, common schema, generated validators and blob tests now reject them;
  no special dot encoding is needed. Other dotted names and dot segments in
  longer domains remain valid. Contract corpus expanded to 217 examples.
- Integration: Workers RPC's recursive conditional types exceed TypeScript's
  instantiation depth for the recursive JSON `Data` type. Domain own/read/renew
  keep the generated `Task` internally but expose a finite internal RPC DTO
  with `data: object`. Wire payloads stay unchanged and are validated against
  generated response schemas in HTTP tests. No type-error suppression or
  `unknown as` casts were used for this boundary.
- Supervisor corrected metadata paging to avoid loading up to 501 task payloads
  that are omitted from the response (potentially over 128 MiB). Queries now
  project only summary fields.
- Supervisor HTTP integration checks passed: full authenticated task/reply flow
  and forbidden-target Consume atomicity. Agent checks report 10 domain SQLite
  tests and 69 ID/blob runtime tests passing; integrated gate pending.
- Simplify: three review-only reuse/quality/efficiency passes found no material
  changes. Exact `/error` own-glob expansion follows spec §4.13; automated
  workers will refuse error topics in phase 5, leaving them to operators.
- Independent Astra high review (`phase2_astra`, fresh `gpt-6-astra`, high):
  independently ran all 159 Workers tests and confirmed two P2 findings.
  First, 4096 entries do not bound cache bytes: a legal 1-million-character
  scope was accepted, and 150 distinct cached identities retained 143.15 MiB.
  Added an 8 MiB conservative retained-byte budget and oversized-entry bypass;
  the same reproduction retains four charged entries within budget while all
  identities still authenticate. Second,
  unconditional error fallback wording conflicted with append-only grants for
  manual Own of error topics. Spec §6.1 now explicitly restricts automated
  workers to non-error topics, narrows the fallback guarantee, and describes
  manual create grants; design §5.4 requires the library restriction too.
  Astra accepted the finite RPC DTO boundary. Focused re-review pending.
  A further targeted probe confirmed that a 4 MiB own glob fits the request
  cap but expands beyond SQLite's 8 MB row limit and returned 500. A 7 MiB
  combined UTF-8 domain/expanded-scope serialization cap is now documented and
  enforced before writes; serialized strings are reused. Real HTTP tests pass
  for the exact 7 MiB boundary, one byte above it, the original expansion case,
  and no partial identity insertion.
- Verification: supervisor `make check` passed all 159 Workers-runtime tests,
  220 Python tests, 217 shared examples against both schema and standalone
  validators, codegen drift/type fixtures, strict TS/mypy, Ruff/ESLint/Prettier,
  and Svelte checks. Live R2 signed-size enforcement remains external.
  After all review fixes, supervisor reran `make check`: 167 Workers tests,
  220 Python tests, 217 examples and all strict/static/drift checks passed.
- Focused Astra high re-review accepted the cache, storage and contract fixes
  (23 auth tests; cache reproducer actual heap about 3.86 MiB). It confirmed
  one additional P2 aggregate boundary: five legal identities with 3.5-million
  character own globs make the former all-at-once Identity List exceed RPC's
  32 MiB limit. Internal bounded pages plus streaming of the unchanged HTTP
  envelope are implemented, with an over-32-MiB regression test. Metadata
  selection and a single bounded payload query share one transaction per
  page; subsequent pages respect backpressure and cancellation. Supervisor
  directly reviewed the small follow-up changes for reuse, quality and
  efficiency. Final supervisor `make check` passed 175 Workers tests, 220
  Python tests, 217 examples and every strict/static/drift check. Focused
  Astra re-review of the streaming fix is pending.
- Final focused Astra high re-review: clean. It independently ran 48 auth and
  router tests and the original reproducer now returns 200 with 35,000,796
  bytes of valid JSON, all five exact identities/scopes, and no token hashes.
  It reviewed paging, bounds, cancellation, backpressure, continuation failure
  and concurrent changes. All four confirmed P2 findings are resolved.

## Phase 3 — client, operator CLI, deployment and packaging

Status: accepted locally; simplify and independent Astra high gates passed.
Live R2 acceptance remains pending on requested signing credentials.

- Scope: typed client/configuration/errors, operator commands, pinned-Wrangler
  deploy/status, bundled Worker and wheel/sdist builds, local conformance.
- Read-only deployment preparation verified pinned CLI output shapes and
  lifecycle replacement behavior. Existing OAuth can read R2 metadata; S3
  signing credentials remain unavailable.
- Supervisor found a prune/Renew race: Renew preserves the ID, so an operator
  Delete by listed ID could remove recently renewed work. Added optional
  atomic `lease_before` to Delete, schema/type generation, valid/invalid
  examples and runtime regression coverage. Prune uses it; standard Delete
  is unchanged. Contract corpus now has 219 examples.
- Deployment checkpoints nonsecret coordinates after upload so expected
  setup stops resume the intended target. Unconfirmed admin candidates remain
  unconfirmed after propagation timeouts; no conclusion about the old token
  is inferred. Pinned deploy targets accept bare hostnames or HTTPS origins.
- Agent verification: 102 client/config tests, 26 CLI tests, 47 deployment
  tests, 4 build/install tests, and 138 local conformance checks (6 monitor
  examples deferred to phase 6). Supervisor integrated verification pending.
- Supervisor integrated verification passed: 176 Workers tests, 401 Python
  tests, 138 conformance checks (6 explicit monitor deferrals), 219 shared
  examples, generated drift/type checks and strict TS/mypy/lint/format/Svelte
  checks. Wheel and sdist built successfully, including wheel-from-sdist.
  Import classification was made explicit so Ruff is consistent across root
  and Python working directories.
- Simplify: reuse pass clean. Efficiency reproduced P2 descendant processes
  surviving a Wrangler timeout; quality reproduced P2 lost credential-update
  state after the early target checkpoint. Added isolated process-tree cleanup
  on timeout/interruption and a durable `r2_credentials_pending` flag cleared
  only after successful secret bulk. Fresh pairs are required while pending.
  Original failures reproduced, then 90 deploy/config regression tests passed
  with strict mypy and Ruff. Supervisor follow-up verification pending.
- First installed-wheel live attempt stopped at read-only whoami preflight
  because output was not JSON; no resources were created. The subsequent
  exact whoami command returned valid JSON. Live bootstrap will be retried
  using the corrected candidate.
- Supervisor follow-up passed 407 Python tests, strict mypy/Ruff, and rebuilt
  wheel/sdist; earlier unchanged service/conformance checks remain applicable.
- Installed-wheel live retry succeeded through bootstrap: created bucket and
  seven-day lifecycle, uploaded Worker, printed and authenticated a new admin
  candidate, then exited 4 for missing R2 keys as designed. Test admin token
  was redacted and not retained; future acceptance will explicitly rotate it.
  No secrets were written to the selected config.
  Worker: `https://tasklease-acceptance-90d25ae.provos.workers.dev`.
  Bucket/name: `tasklease-acceptance-90d25ae`.
  Temporary config: `/tmp/tasklease-acceptance-90d25ae.tYv2Oi/config.toml`.
  User was asked for bucket-scoped R2 credentials in an ignored local file;
  full upload/download and wrong-size checks remain pending.
- Independent Astra high review (`phase3_astra`, fresh `gpt-6-astra`, high)
  independently passed 181 client/config/CLI/deploy tests, 138 conformance
  checks (6 monitor deferrals), and 11 DomainObject tests. It accepted pending
  credential recovery, POSIX process cleanup and conditional Delete. One P2
  was found: inherited `CLOUDFLARE_ENV` and `WRANGLER_CI_OVERRIDE_NAME` can change
  the Worker target before upload. A real pinned local dry-run reproduced
  both overrides. Both variables are now removed at the subprocess boundary;
  an actual pinned normalization/dry-run regression proves the intended name
  is preserved with networking blocked. All 54 deploy tests, strict mypy and
  Ruff passed. Focused independent Astra re-review is pending.
- Final focused Astra high re-review: clean. Independently reran all 54
  deployment tests, including the real pinned target regression. Explicit
  account selection remains intact; no new material findings. All phase 3
  findings are resolved. Final wheel/sdist rebuild includes this correction.

## Phase 4 — durable requester

Status: accepted after simplify and clean independent Astra focused re-review.

- Scope: cross-platform durable no-replace filesystem operations, request and
  reply stores, request/wait/cancel/inbox/resend/prune and fault/race tests.
- Read-only filesystem preparation retrieved primary POSIX, Apple and Windows
  documentation. `exists` must also be a durability barrier; parent entries
  and Windows/macOS flush differences require explicit treatment.
- Initial verification and review evidence follows below.
- Additional isolated remote verification passed using the reviewed phase 3
  wheel: actual Create/Own/Renew, then redeployment preserving the exact held
  task plus identity token/admin secret, then atomic Consume/reply Own/Delete
  and stale-ID fencing. Temporary identity was revoked and test tasks removed;
  credentials existed only in the test process's memory. This proves redeploy
  preservation; a later different-bundle upgrade remains a final acceptance
  check. Test helper: `/tmp/tasklease-acceptance-90d25ae.tYv2Oi/remote_state_check.py`.
- Clarification: request-record `data` is the exact Create task payload after
  request/reply correlation fields are set; domain/topic are separate fields.
  Explicit resend applies to submission-unknown records, preserving immutable
  known task sidecars. Cancel `not_found` means taken or removed; it does not
  prove which happened, and waiting metadata is retained.
- Live R2 acceptance passed using the supplied ignored credential file:
  installed-wheel deploy returned exit 0 and `verified: true`; task/blob probe
  and cleanup passed; a 17-byte upload against a signed 16-byte Content-Length
  was rejected with 403 and created no object; correct-size upload/download
  and SHA-256 matched. No R2/admin values appeared in logs or config. File
  permissions were restricted to 0600. Blob samples expire under the seven-day
  lifecycle. Test helper: `/tmp/tasklease-acceptance-90d25ae.tYv2Oi/live_r2_check.py`.
- Supervisor added real bundled-Worker conformance: lost committed Create
  response recovers after Store restart, duplicate replies retain the first,
  local replies work offline, and lost committed Delete preserves the durable
  reply. Both integration tests passed.
- Supervisor found escaped isolated surrogate strings could wedge publication
  or UTF-8 stdout. Local JSON and CLI output now preserve them with JSON
  escapes; unencodable correlation IDs are stored unmatched. Added targeted
  serializer and raw-response tests; service protocol is unchanged.
- Integrated supervisor `make check` passed: 514 Python tests, 176 Workers
  tests, 140 conformance checks (6 monitor deferrals), 219 examples and all
  strict/static/drift checks. Linux/Darwin/Windows static typing passed in
  focused agent checks. Runtime process races, paused-winner durability,
  cleanup/crash points, store corruption and deadline cap tests passed on Linux.
  Native OS evidence follows below.
- Simplify: reuse and efficiency clean. Quality reproduced one P2: Python's
  permissive JSON parser accepted saved NaN/Infinity and overflowing floats,
  allowing a corrupt existing reply to acknowledge a valid duplicate. Strict
  float/constant hooks now reject nonfinite values before acknowledgement.
  Added parser cases and five Store regressions; 141 focused tests and strict
  Linux/Darwin/Windows typing passed. Supervisor `make check-python` then
  passed all 535 tests, Ruff and strict mypy.
- Native macOS: 257 current filesystem/store/config/client/requester/operator
  CLI/JSON/package tests passed on the supplied Mac in an isolated checkout
  at `/tmp/tasklease-native-04371b7.NoBqe3/repo`. Publication also passed under
  the actual default `~/Library/Application Support/tasklease` directory.
  The original checkout was left untouched. Safe-chain's minimum package age
  blocked new codegen dependencies, so native runtime checks used an
  age-qualified runtime/test environment; no guard was bypassed. Locked
  codegen/build checks run on Linux. Native Windows remains pending the
  final-candidate agent prompt requested by the user.
- Independent Astra high review: found one P2 in generated-ID recovery:
  after successful Create, failure or interruption during `--wait` omitted
  the request ID. Submission and wait now share the recovery boundary;
  errors print the saved state and wait command, and unknown submissions
  retain their explicit resend guidance. Seven regressions cover ordinary
  and resent submissions, transport/protocol failures and Ctrl-C. All 29
  requester CLI tests and strict mypy/Ruff passed; all 29 also passed natively
  on macOS. Focused independent Astra high re-review is clean, and independently
  ran 229 focused tests plus the final 29 CLI tests. No unresolved material
  findings remain.
- Review scope clarification: cancellation's intent-first removal protects
  ordinary process-crash ordering. Local removals do not promise persistence
  across power loss. A resurrected record never automatically reposts;
  resend always requires an explicit user action. The documented publication
  durability guarantee is unchanged.

## Phase 5 — worker runtime

Status: implementation starting.

- Scope: capacity-before-Own library, deadline/renewal and fenced completion,
  bounded error routing, generic echo/plugin CLI and graceful stop controls.
- Simplify: pending.
- Independent Astra high review: pending.
- Verification: pending.
