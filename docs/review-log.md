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

Status: accepted; simplify and independent Astra high gates passed.
Live R2 acceptance subsequently passed; see phase 4 evidence below.

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

Status: accepted after simplify and clean independent Astra high review.

- Scope: capacity-before-Own library, deadline/renewal and fenced completion,
  bounded error routing, generic echo/plugin CLI and graceful stop controls.
- Portable stop files use the existing identifier hash rather than the raw
  label, preserving case distinctions and avoiding Windows reserved names and
  filename limits. The design records the offline stop command and hash.
- Clarified that a stable task ID requires no ID mutex, while deadline/lost
  state is synchronized and an independent watchdog fences blocked or late
  renewal responses.
- Supervisor review found a short-lease retry issue: after a failed Renew,
  sleeping the entire remaining subsecond lease left no time to retry. Retry
  delay now respects the renewal interval and a fraction of remaining time.
  Error-text normalization also prevents surrogate characters in handler
  exceptions from poisoning the bounded fallback.
- Four real packaged-Worker conformance checks passed: committed-but-lost
  Consume response yields exactly one reply; forbidden reply target routes to
  the automatic error grant; escaped-surrogate wire data drains using bounded
  ASCII JSON text; and a lost Renew response recovers while a one-second lease
  is renewed during active work.
- Native macOS core verification passed all 125 worker/client tests, including
  watchdog timing, renewal faults, bounded fallback and thread cleanup.
- An active-handler stop test reproduced a publication/removal race: the
  watcher could unlink a marker before durable publication's final flush.
  Stop markers now have an explicit transient, empty exclusive-create
  contract; they contain no task data and require no post-publication path
  access. The watcher retries Windows sharing violations. Immutable requester
  publication keeps its durability contract.
- Simplify: reuse, quality and efficiency passes clean. Quality independently
  ran 170 worker/client/CLI/control tests; efficiency ran 125 worker/client
  tests. Supervisor reviewed the final transient marker correction for reuse
  and efficiency; quality independently reviewed it too.
- Integrated verification passed: 647 Python tests; 176 Workers tests;
  144 conformance checks, with only the six phase-6 monitor deferrals;
  219 contract examples; generated drift, strict TS/mypy, lint/format and
  Svelte checks. A test module was renamed to avoid duplicate mypy module
  names before completing the Python/conformance gates.
- Native macOS passed 589 runtime tests, excluding only the Node/build and
  pinned-Wrangler deployment suites already verified on Linux. This includes
  actual first/second signal behavior, active-handler stop files, filesystem
  races, requester recovery and renewal/fencing. Native Windows remains
  pending the final-candidate agent run.
- Independent Astra high review: clean. Fresh `gpt-6-astra` at high effort
  independently passed 170 worker/client/CLI/control tests and reviewed the
  complete phase against spec/design, including timing and error boundaries.
  No material findings. Uncooperative custom transports can delay renewal
  thread join; the watchdog still marks loss and threads cannot accumulate.

## Phase 6 — monitor and operations

Status: accepted after simplify, independent Astra review and native verification.

- Scope: scoped consistent snapshots, read-only local server and local-store
  views, Svelte dashboard, packaged assets and platform startup/stop templates.
- Preparation found aggregate snapshots can exceed RPC's 32 MiB value limit,
  and SQL cursors across awaits have no snapshot isolation. Snapshot capture
  now spools bounded chunks inside one transaction for a byte-stream Response,
  with two-stream admission, expiry and cleanup. Wire schema is unchanged.
- Clarified hour windows as current bucket plus the previous `hours - 1`,
  retaining authorized historical statistics independently of live queues.
- Startup templates are canonical package resources with an offline CLI
  export command and instructions in `deploy/`. Native `plutil` parsed the
  launchd template; `systemd-analyze verify` accepted the user unit (unrelated
  host-unit warnings remain). XML/plist parsing passed. Checked current
  primary platform references and the Mac's installed launchd manual;
  bounded launchd unload timeout avoids an infinite shutdown wait, and
  systemd mixed kill mode preserves handler subprocesses during graceful stop.
- Microsoft's published Task Scheduler XSD independently validated the Windows
  XML. Wheel checks now verify all four templates and exercise offline export
  from a clean installation with Node absent.
- Service agent passed 188 Workers tests, including a 36,080,125-byte actual
  RPC snapshot with 110,000 statistics rows, parsed incrementally. Schema 3
  adds only temporary snapshot tables; migration preserves task state.
- Dashboard strict Svelte/build checks passed. Added pinned
  `prettier-plugin-svelte` so the existing formatting gate covers Svelte files.
  Hourly charts retain each domain's capture time across hour boundaries.
- Integrated supervisor gate passed 714 Python tests, 188 Workers tests,
  all 150 conformance checks with no deferrals, 219 examples and every
  strict/static/drift check. Wheel and sdist built successfully. Native macOS
  passed 656 runtime tests, excluding Node-dependent build/deploy suites.
- Browser verification used an actual local server with fake service/store
  data: populated overview, expanded queue metadata, escaped script text in
  reply JSON, hourly charts/table, HTTP-502 stale warning and recovery all
  worked. No real credentials were used in the browser fixture.
- Simplify: reuse and quality clean. Efficiency reproduced a P2 where 100
  answered 250 KB requests produced a 25,050,938-byte monitor snapshot with
  no pending requests. The server now filters answered records before
  serialization; pending and unknown requests plus inbox replies remain.
  The large-input regression, readonly checks and all 135 focused tests pass;
  independent efficiency reran 63 monitor/CLI tests and quality reran 135.
- Independent Astra high review: clean after the missing-dashboard correction.
  Absent root/index assets now serve static HTML with HTTP 503 and actionable
  build/reinstall guidance. Host/path guards remain; two real-socket tests
  independently passed. Astra also independently passed Svelte checks and
  all 13 committed fixture-based chart/model Vitest tests. `make check`
  includes that suite, as required by design §7.4.
- A final Mac rerun exposed intermittent connection resets during eight
  concurrent refreshes. Controlled native comparison isolated the default
  listen backlog: five allowed only 5/8 successful connections in each of
  three rounds; 32 allowed 8/8 with one refresh. The server now uses a bounded
  backlog of 32. A deterministic regression queues eight complete requests
  before accept starts and fails with the old setting. Five repeated native
  concurrency pairs and all 138 focused Mac tests passed. Independent Astra
  focused re-review and its two Linux concurrency tests are clean.
- Final follow-up passed all 718 Python tests, strict mypy/Ruff and rebuilt
  wheel/sdist. The complete gate also passed 188 Workers tests, 13 monitor
  component/model tests, 150 conformance checks, 219 examples and generated
  drift/static checks. Runtime npm audit
  reports no advisories; existing low/moderate advisories are confined to
  development lint/build tooling and were not resolved by unsafe downgrades.

## Phase 7 — complete-system acceptance

Status: accepted locally after simplify and independent Astra whole-system
review; native Windows and public publication remain external checks.

- Scope: local release/native acceptance tooling, Windows agent handoff,
  clean package installation, live different-bundle upgrade and cross-machine
  request/reply flow, documentation and whole-system review.
- GitHub Actions remains excluded by design §7.4. Package publication is a
  separate explicit release action; no public upload has been performed.
- Actual different-bundle forward upgrade passed: installed phase-3 Worker
  bundle `855f7b17c197…` to completed bundle `6bef2328d14f…`. A held/renewed
  legacy task survived byte-for-byte, with identity/admin credentials and
  statistics intact. The old snapshot route was absent; the new route read
  the preserved task after schema migration. Post-upgrade Consume/reply and
  stale-ID fencing passed, as did deployment's actual R2 probe. All temporary
  test tasks were removed and the identity revoked. Helper:
  `/tmp/tasklease-acceptance-90d25ae.tYv2Oi/final_upgrade_check.py`.
- Cross-machine acceptance passed using clean installed wheels: Linux Store
  submitted, a macOS generic EchoHandler owned/consumed, and Linux durably
  saved/acknowledged the reply. A restarted Store read it offline; scoped
  snapshot/poller/statistics checks passed. Narrow temporary requester/worker
  identities were revoked and test tasks removed; SSH carried credentials
  through stdin only, with no values persisted or printed. Helpers:
  `/tmp/tasklease-acceptance-90d25ae.tYv2Oi/cross_machine_check.py` and
  `cross_worker.py` in that directory.
- `make release` now runs checks/build and publishes only the current
  version's wheel/sdist when explicitly invoked. Only `make -n release` was
  used to verify the recipe; no package publication was executed.
- Added stdlib-only acceptance export, shared manifest/path integrity checks,
  an installed-wheel/no-Node native runner with JSON/JUnit/full logs, and
  Windows handoff/release instructions. Normal Python gates include the new
  tooling tests and lint/type-check all Python scripts.
- Simplify reuse and efficiency independently reproduced one P2: timeout
  cleanup could leave a SIGTERM-resistant descendant alive after its parent
  exited, and Windows taskkill failures skipped fallback termination. The
  standalone runner now follows the established Wrangler cleanup strategy:
  final POSIX group kill, Windows failure fallback and parent reaping. The
  resistant-child and three Windows failure regressions pass; all 27 tooling
  tests passed independently before the additional review fixes below.
- Final review found three further P2 gaps and authors fixed each: the
  documented monitor `--domain` flag now selects only a permitted service
  domain while preserving the all-domain default and local views; the uv
  version probe now uses the controlled process-tree runner; and native
  acceptance binds extracted bytes to the exact verified ZIP before any
  command runs. The original stale-extraction/new-ZIP false pass is covered
  by a failing-before/fixed-after regression. New archive member-integrity
  and resistant-wrapper regressions pass. Focused checks passed 153 monitor/
  CLI/store tests, 33 tooling tests and strict checks across all 52 files.
- Final independent Astra high whole-system review and focused re-review are
  clean. It independently ran 383 tests across critical production/tooling
  paths, then 186 focused acceptance/monitor/store tests, reproduced the
  original ZIP mismatch and verified rejection before command execution.
  Simplify quality independently reran all 33 tooling tests and accepted the
  controlled version probe and ZIP/extracted-file linkage. No material review
  findings remain; actual candidate native execution follows below.
- Initial phase-7 integrated gate passed 745 Python/tooling tests, 188 Workers
  tests, 13 dashboard tests, 150 conformance checks, 219 examples and all
  static/drift/build checks. Final correction verification, focused reviews
  and native bundle execution are recorded below.
- Final follow-up passed all 766 Python/tooling tests, strict checks across
  52 Python files and rebuilt wheel/sdist. Unchanged service/frontend gates
  passed 188 Workers tests, 13 dashboard tests, 150 conformance checks and
  219 shared examples with generated drift checks. All review findings are
  resolved; no package was published.
- Actual exported-candidate acceptance passed from clean installed wheels on
  both Linux and macOS: 675 runtime tests each, zero failed/skipped, plus the
  isolated no-Node/PATH-empty probe of package bytes, dashboard, Worker and
  all four startup exports. Both reports match archive, source-tree and wheel
  hashes. Preflight ZIP SHA-256:
  `fcdeb1e38105eb1ad36d8453abced5bf598a290fd9f9c8d0a8bb2c530aef82ac`.
  It was explicitly exported from the staged review candidate with dirty
  status recorded. Reports are at
  `/tmp/tasklease-acceptance-90d25ae.tYv2Oi/native-linux.json` and
  `native-macos.json` in that directory. The clean final Windows ZIP will
  differ only in acceptance-document evidence and commit metadata; package,
  tooling and runtime-test bytes are checked for identity before handoff.
- Windows handoff: [agent prompt](windows-agent-prompt.md), a source/wheel
  ZIP and its trusted SHA-256. It requires OS/tool versions, exact artifact
  identities, native test counts/skips/failures and full tracebacks, plus
  native Task Scheduler XML parsing without registering a task. Until that
  report arrives, Windows runtime behavior remains unverified.

## Native Windows acceptance correction — 2026-11-07

Status: accepted after corrections, independent reviews and native qualification.

- The Windows agent tested clean detached `e63dd871f40e49c5b62ef607c26e8e2743430715`:
  665 passed, four failed, six expected platform skips. Installed-wheel probe
  passed with Node absent. Original source and evidence were preserved.
- Windows 11 Pro x64 build 26300.9711; Python 3.12.14, uv 0.13.6,
  pytest 9.2.0, jsonschema 4.26.0, Node 24.22.1 and npm 11.21.1.
- Original acceptance ZIP SHA-256:
  `327b723b6b730161a883ca2f208af76f9161be0057c28d5d5bfd309965c65fb5`;
  source tree `1c33634ad33815e7479734457624becd786a8c64160c57b198881532f1ac60ff`;
  wheel `4d3bbe0a00e82c71a61ec7f4738543038ae220797be8761738dee780f2eda200`.
- Mounted evidence was read from
  `/mnt/c/Users/provo/AppData/Local/Temp/tasklease-native-ee69b803e201495180bfdc23c7164d77/`.
  Three concurrency failures share errno 13 at the CRT `r+b` publication-flush
  reopen. The fourth is a CP1252-written fixture contradicting the CLI's UTF-8
  input contract. Task Scheduler's real COM parser rejected the UTF-8 XML
  declaration in a Unicode BSTR with HRESULT `0x8004131A`.
- A native COM A/B test reproduced the original encoding failure and accepted
  the same XML without its encoding attribute. No task was registered or run.
- Security qualification also failed: nine development dependency advisories
  and eight installed locked packages newer than the seven-day cutoff, despite
  npm age settings. Independent lockfile age verification and qualified pins
  are being prepared; npm settings alone are not treated as proof.
- Native Windows interop is available through WSL with sandbox escalation, so
  corrected-candidate tests can be run directly in a fresh Windows directory.
- Corrected publication flushes and JSON reads use pointer-width-safe Win32
  handles with read/write/delete sharing, preserving real flush errors and
  closing handles on every path. A deterministic native DELETE-sharing test
  covers Unicode data larger than the read buffer. Five repeated native
  concurrency rounds passed all 20 critical cases; filesystem/store checks
  passed 111 tests with three expected skips.
- Explicit UTF-8 fixtures preserve the CLI contract; a CP1252 negative case
  verifies rejection before publication. Encoding-neutral XML passed both
  native COM Unicode-string and UTF-8-file parsing, including an accented
  working directory. No Task Scheduler task was registered or started.
- Corrected editable-source native Windows runtime: 678 passed, zero failed,
  six expected POSIX-only skips. Evidence:
  `/mnt/c/Users/provo/AppData/Local/Temp/tasklease-win-runtime-correction-b6mwfvbg/`.
  This is preliminary runtime evidence; clean installed-wheel qualification
  follows separately.
- Replaced young pins and the vulnerable minimatch graph; added a narrowly
  reviewed deepmerge backport because the primary advisory has no patched
  upstream release. Original tarball integrity, exact patch, license and
  runtime source hashes are recorded under
  `scripts/dependency-policy-vendor/deepmerge/`. Regression tests exercise
  prototype poisoning and merge/API compatibility. No advisory was suppressed.
- Added a built-ins-only gate before npm installation and before source asset
  builds load third-party code. It verifies all 302 locked registry versions,
  including optional packages for other OS/CPU combinations, against the
  seven-day cutoff and exact registry tarball/integrity. Installed mode checks
  hidden-lock parity and actual package versions/links. Lifecycle scripts are
  disabled; esbuild/workerd binaries and all build/test tools work without
  them. npm audit reports zero vulnerabilities, including dev dependencies.
  The supported Worker compatibility date is now `2026-10-30`, with regenerated
  runtime bindings and no use of newer APIs.
- Independent simplify reuse, quality and efficiency passes are clean. The
  reuse pass caught an eager TypeScript import before the asset-build guard;
  the import now happens after validation. Efficiency independently confirmed
  one registry request per package name (290 requests for 302 versions), at
  most eight concurrent requests, and no outstanding requests on success.
- Fresh independent `gpt-6-astra` high review found a stale Wrangler login pin
  in first-use help. It now matches qualified Wrangler `4.156.0`; the existing
  installed-sdist/no-Node probe asserts that help and the generated bundle
  manifest agree. Focused Astra re-review is clean. It independently passed
  71 Python cases (three native-only skips), all 13 policy/backport cases,
  isolated CLI help and the vendor patch's exact upstream-hash reconstruction.
- Integrated local gate passed 772 Python/tooling tests with three expected
  native-only skips, 188 Workers tests, 13 dashboard tests, 150 conformance
  cases, 219 examples, 13 policy cases, strict/static/drift checks and wheel/
  sdist builds. Focused packaging/help revalidation and final native Windows
  clean-wheel/security execution follow below.
- The checkout policy does not lock transitive resolution performed by an
  operator's `npx` during deployment. That existing design boundary is now
  explicit in the release instructions.
- Focused review-fix verification passed all 30 packaging/help tests, Ruff and
  formatting, then rebuilt wheel/sdist. The vendored patch was recorded with
  zero context so Git's whitespace checker preserves the original code's tabs
  without treating unified-diff context markers as source indentation.
- Corrected reviewed candidate passed from fresh installed wheels on all three
  operating systems: Windows 678 passed/six expected skips; Linux and macOS
  each 681 passed/three Windows-only skips. All have zero failures/errors and
  passed the isolated package-byte/dashboard/Worker/template probe with Node
  absent from PATH. Both actual Windows Task Scheduler COM cases passed; no
  task was registered or started. Every report binds identical ZIP, source
  tree and wheel hashes:
  - ZIP `ec1a6c893278871496b09ad70df2adf55da8729c71761790a2d8e48bcd67540d`;
  - source `797b2a1a1cd082d1e32f6d02b28a2ca0654b144f3a05ed3463447e7a1282734d`;
  - wheel `b785007279ccfe0f63d2d01659ddec6107187d230ac5efa15320c4756185b949`.
  This preflight was explicitly exported from staged reviewed changes atop
  `e63dd87`, with dirty status recorded. Subsequent edits update acceptance
  documentation only; package, tests, build tools and dependency bytes match
  the qualified candidate.
- Windows evidence, including exact commands, JUnit, all skip reasons, probe
  and full pytest logs, is preserved at
  `/mnt/c/Users/provo/AppData/Local/Temp/tasklease-win-corrected-ihut5w5o/`.
  Windows qualification separately copied source so Node builds could not
  alter the verified runtime bundle: pre-install and installed-tree age/source
  gates passed at the stricter `2026-11-01T00:00:00Z` cutoff; fresh `npm ci`
  kept lifecycle scripts disabled; audit reports zero at every severity;
  policy/backport tests passed 13/13; Workers tests passed 188/188; complete
  Vite and bundled-Worker asset builds passed with native Windows binaries.
- Linux report: `/tmp/tasklease-linux-corrected-49zbeoi3/native-report.json`.
  macOS report: `/private/tmp/tasklease-corrected.r8h5eu/native-report.json` on
  `provos@macbook.localdomain`, copied to
  `/tmp/tasklease-macos-corrected-native-report.json`. Native Python versions
  were 3.12.14 on Windows and 3.12.12 on Linux/macOS. All original checkouts
  and initial failed-candidate evidence were left untouched.
- Phase 7 is closed. All seven implementation phases and their review gates
  are accepted; public package publication remains unperformed.

## Post-acceptance simplification: glob length cap

- Topic globs are capped at 256 characters in the schema and spec §3. Identity
  Add rejects an own glob whose `/error` grant would exceed the cap. This
  replaces the 7 MiB aggregate identity check, and Identity List pages are now
  64 records with no byte budget: a maximal legal identity serializes to about
  265 KB, so a page is about 17 MB.
- The token cache keeps its byte budget: 4096 maximal identities would still
  exceed an isolate's memory. Its oversized-entry guard stays only to keep
  eviction finite. Identity List keeps streaming, since the identity count is
  unbounded.
- Tests: new valid/invalid schema examples at the 256/257 boundary; Workers
  tests cover the expanded-grant boundary (250 accepted, 251 rejected, nothing
  written) and stream 130 maximal legal identities (over 32 MiB) through
  several internal pages.
