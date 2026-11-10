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

## PyPI trusted publisher setup — 2026-11-08

- Implemented in isolated `codex/pypi-trusted-publisher` worktree based on
  merged `811528d`, while the separate dashboard feature remains in its own
  checkout. Scope is the publishing workflow and release/design documentation.
- Matches the supplied pending publisher exactly: project `tasklease`, GitHub
  repository `provos/tasklease`, workflow filename `workflow.yml`, no required
  environment name. The file is `.github/workflows/workflow.yml`.
- Workflow dispatch is manual and limited to this repository's `main` branch.
  It accepts an existing stable version tag reachable from `main`, verifies
  the tagged package name/version, and checks/builds that exact commit. No
  routine push/PR/release-event jobs were introduced. Default `publish=false`
  saves artifacts only. An explicit `publish=true` permits the separate upload
  job after all build/check steps succeed.
- Reuses `make install`, `make check`, and `make build`; the build job has only
  read access. The publishing job has `id-token: write`, downloads that run's
  exact distributions, and uses the official PyPA action for OIDC, metadata
  verification and attestations. It has no repository checkout or build code.
  No PyPI token/secret or GitHub environment configuration is required for
  the provided registration. Official action versions are pinned to verified
  release commit SHAs; Node, Python and uv versions are explicit.
- Local actionlint 1.7.12 passed after its official binary checksum was
  verified. Workflow Prettier check and Git whitespace check passed. Ten
  disposable-Git tests exercised the workflow's exact validation script:
  annotated tag acceptance, missing/mismatched/invalid tags, package-name
  mismatch, main ancestry, and shell-injection rejection.
- Fresh install passed all 302 locked registry age/source checks and reported
  zero npm advisories. Release checks passed 799 Python/tooling tests with
  three expected native-only skips, 187 Workers tests, 13 dashboard tests,
  223 examples, all 13 dependency-policy tests and strict/static/drift checks.
- Initial full conformance reported one timing-sensitive failure in the
  existing one-second lost-renew-response case (no reply within its wait).
  The unchanged focused case then passed, and the full conformance rerun
  passed all 154 cases. No runtime/test behavior was changed for this setup.
  Final wheel/sdist builds passed. Logs are under
  `/tmp/tasklease-publisher-{install,check,conformance-focused,conformance-rerun,build}.log`.
- Independent fresh Astra high review, including simplify reuse/quality/
  efficiency, found no material issues. It checked the PyPA action source,
  permissions, ref/tag guards, artifact isolation, registration matching and
  release instructions. No GitHub workflow was dispatched, no OIDC exchange
  or PyPI upload was attempted, and no service was deployed. Authentication
  and first publication remain unverified until an authorized publish run.

## 0.1.0 release candidate — 2026-11-08

- Prepared from `4aa5c40`, retaining the initial `0.1.0` package version.
  Added the user-selected Apache-2.0 license to the repository, Python
  metadata/distributions and all first-party npm workspace manifests. Added
  a packaged long description and repository/changelog URLs. The changelog
  records the initial release candidate; PyPI publication remains pending.
- Asset builds now include full upstream license texts for Ajv's embedded
  validator runtime, aws4fetch, Vite's runtime helper and the dashboard's
  actual bundled dependencies (Svelte and clsx). Vite generates the dashboard
  report; the service bundle includes the combined `THIRD_PARTY_NOTICES.md`.
  Packaging tests verify upstream license bytes, Apache metadata and license
  inclusion, no-Node sdist rebuilding/installation, and rejection of either
  missing notice file. The first-release metadata checklist item is complete.
- `make install`, `make check` and `make build` passed. The final check run
  includes all 302 locked dependency age/source checks, zero npm advisories,
  all 13 dependency-policy tests, 223 contract examples, generated-code drift,
  formatting/linting, strict TypeScript/Svelte/Python checks, 191 Workers
  tests, 32 dashboard tests, 801 Python/tooling tests with three expected
  native-only skips, and all 154 local Worker conformance tests.
- Clean candidate export at `0a562deade46095a897b75ff9c5c7febeee83435` passed
  identical installed-wheel/no-Node acceptance on Linux, macOS and Windows.
  Linux and macOS each passed 708 runtime tests with three native-Windows
  skips; Windows passed 705 with six expected POSIX/platform skips. All had
  zero failures/errors, the same source/wheel/archive hashes, and successful
  packaged-resource probes. Windows passed both native Task Scheduler COM
  test cases and a separate exported-template parse without registering a
  task. Python was 3.12.12 on Linux/macOS and 3.12.11 on Windows.
- Candidate identities:
  - acceptance ZIP: `fbbaed45d184dd8ccf8096c9ee4d564ecb1f1cb63c78cb58999d55f3ed79a5fb`;
  - source tree: `efe8bd51d82da266760442a0cdf7f458ae183f1a22e261d31d5e783413b7cbe4`;
  - wheel: `d45fc55ebbbc8a2e2b2d24c38424ebf83a9cf1a7d16a7df2e954a4abbdb781ae`;
  - sdist: `358c560c5f1639c67bd0f503f8d836d0f7c271d973f8f96dadb3cdb4ee3a9ff7`.
- A fresh `tasklease-release-010-13d0327d` Worker in the acceptance account
  reused only the dedicated `tasklease-acceptance-90d25ae` test bucket. An
  installed earlier phase-3 wheel bootstrapped it; the installed candidate
  upgraded it. The different-bundle upgrade preserved the exact held task,
  identity/admin credentials and statistics, then passed snapshot migration,
  atomic reply and stale-ID fencing. Both deployments passed real task/R2
  probes. Wrong signed Content-Length returned 403 without creating an
  object; the correct upload/download and SHA-256 passed. The temporary
  identity was revoked and the new Worker was deleted after acceptance. The
  earlier acceptance Worker/bucket were not redeployed or deleted; probe
  objects use the existing seven-day lifecycle. No credentials were logged
  or saved in the candidate or service configuration.
- Full local logs, native JSON/JUnit/probe/pytest reports, the verified ZIP,
  redacted live deployment results and cleanup evidence are preserved under
  `/tmp/tasklease-release-010.KeK43m/`. This evidence-only documentation update
  does not change the qualified package, build inputs or tests.

### Main README revision

- Per user direction, removed the separately maintained `python/README.md`.
  The custom metadata hook now reads the repository's main `README.md`; the
  sdist build includes that exact snapshot as `README.md`, and metadata uses
  the packaged snapshot when building outside the checkout. Tests verify
  exact description bytes and successful no-Node sdist rebuilding/install.
- Repeated the complete `make check` and `make build` successfully, with the
  same final counts above. Repeated native acceptance on the rebuilt wheel:
  Linux/macOS each passed 708 tests/three expected skips and Windows passed
  705/six expected skips, including native Task Scheduler parsing. All three
  verified the same clean source snapshot at
  `cbb5299a899daec7707742ec812cf69a4fdfb614` and the same candidate hashes.
- These final distribution identities supersede the earlier candidate:
  - acceptance ZIP: `392bb7fc5c298f7a8a9a2262b2eb520a0c7e52ed803aad8a763e0915bde8a3de`;
  - source tree: `f2916b0f2b7de0054477b1eab97eb162b8d17230565ae52b87e420746c6d3bf2`;
  - wheel: `3d3b4acdf0800810bd0844ab92afb0fc34bd0892bc4090a01a60c4b9b7d8d03b`;
  - sdist: `be9d222522e0233616489968ef11d41e0d567b3061efa6d55764d7ba4ed2c309`.
- Python and executable dashboard/Worker bytes match the live-qualified
  candidate exactly. Besides distribution metadata, only Wrangler's generated
  build-timestamp README and debug source map differ. The live Cloudflare/R2
  results therefore cover the same Worker JavaScript. Final reports, full
  check/build logs and byte-comparison evidence are under
  `/tmp/tasklease-release-010.KeK43m/readme/`. PyPI still returned 404 for
  `tasklease/0.1.0` during preparation; the GitHub release remains a draft.

## First PyPI publication (2026-11-08)

- Published `tasklease 0.1.0` from the final `v0.1.0` tag at
  `c373fbf94da11d0f4ab3fdad20978100b4dfca99`, including the pipeline-worker
  change. The tag's annotated object is
  `c75b7ac4ec6189288310488da5c5e2d35024bc74` and remains unchanged after
  publication. The [trusted-publisher run](https://github.com/provos/tasklease/actions/runs/42573194865)
  completed both build and publishing jobs successfully, including metadata
  checks and attestations. It reran the full checks: 191 Workers, 32 dashboard,
  809 Python/tooling tests with three expected skips, 154 conformance tests,
  and the dependency, audit, generation, formatting, lint and type gates.
- Verified both files' SHA-256 hashes against PyPI's release JSON metadata
  and the publishing run's artifacts:
  - wheel: `3e62b9aa3a718d05ad1ff1e5f7e2f39b4ed57322422de22ec99d65fc7a80d9d7`;
  - sdist: `71f237ef37e4cdb1b978048866c32617d8f83b6b57b5e56198eb2fcaabc60015`.
  The main README description/sdist snapshot, Apache-2.0 metadata/license,
  full bundled third-party notices and exact tagged worker source passed
  artifact inspection. These published files supersede the earlier draft
  candidate distributions and hashes above.
- Installed `tasklease==0.1.0` from `https://pypi.org/simple` into fresh
  environments outside the checkout on all three platforms, without cached
  package resolution. `tl --version` returned `tl 0.1.0`; help and isolated
  installed-wheel/no-Node resource probes passed. Native runtime tests passed:
  Linux 716/three expected skips, macOS 716/three expected skips, and Windows
  713/six expected skips. Windows Task Scheduler parsing passed without
  registering a task. Every report binds the same published wheel and clean
  tagged source snapshot.
- macOS Safe-chain initially suppressed the newly published version under
  its minimum-age policy. The user approved a one-time exception solely for
  SHA-256-pinned `tasklease==0.1.0`, installed with `--no-deps`. Runtime and
  test dependencies were installed separately under their existing age
  checks; no persistent security setting was changed.
- A fresh `tasklease-pypi-010-4992c97a` test Worker, using only the existing
  dedicated acceptance bucket, bootstrapped from the earlier installed bundle
  and upgraded from the fresh PyPI installation. The exact held task,
  identity/admin credentials and statistics survived. Snapshot migration,
  atomic reply/fencing and real R2 signed-size rejection/upload/download
  checks passed. The temporary identity was revoked and the new Worker was
  deleted; the existing acceptance Worker/bucket were not redeployed or
  deleted. Logs redact credentials, and none were persisted in configuration.
- PyPI: [tasklease 0.1.0](https://pypi.org/project/tasklease/0.1.0/).
  GitHub: [v0.1.0 release](https://github.com/provos/tasklease/releases/tag/v0.1.0).
  The release's wheel/sdist match PyPI exactly, and its checks archive retains
  publishing logs, PyPI metadata/hash verification, native reports and
  redacted deployment/cleanup evidence. Local evidence is preserved under
  `/tmp/tasklease-publish-0.1.0.1Nl0Uq/`.
- Updated the main README's PyPI installation instructions, marked the
  first-release setup/publication acceptance complete, and finalized the
  changelog. This documentation follow-up does not modify the published tag
  or distributions.

## 0.2.0 candidate: offline service and blob helpers (2026-11-09)

- Prepared an uncommitted candidate based on
  `f2c22dc174957adf90ab26a9124ece2eed2e6569`. Added typed `FakeBoard`, streaming
  blob transfer helpers and public request-ID/scope predicates, with usage
  documentation and shared fake/Worker conformance. Optional runtime behavior
  changes from the handoff remain separate; corrected the documented
  `WorkerLoop` domain argument. No service upgrade is required for these APIs.
- Three implementation agents owned disjoint files. Three review-only
  simplify passes found no worthwhile reuse, quality or efficiency changes.
  Two independent `gpt-6-astra` high-effort reviews each reproduced nested-JSON
  drift against the actual Worker. Fixed validation and serialization with
  iterative traversal, removed redundant recursive data copying, and made
  snapshots stack-safe. Shared 500-level array/object lifecycle regressions
  now pass on both targets. Both reviewers rechecked the fix independently
  and reported no remaining material findings.
- Integration also checked JavaScript number formatting at data-size limits,
  upload mutation detection, raw-byte download verification, and blob-date
  boundary behavior. A seeded differential probe of 99,946 finite IEEE754
  values found no number-rendering mismatches with `JSON.stringify`.
- `make install`, `make check` and `make build` passed on macOS with Node
  `26.7.0`, Python `3.12.12` and uv `0.9.15`. The full check included 191
  Worker tests, 32 dashboard tests, 916 Python/tooling tests with three native
  Windows skips, and 386 conformance tests with 36 explicit capability skips.
  Conformance skips are 35 unsupported fake admin/operator/declaration/monitor
  cases and one local-Worker signed-transfer case. Dependency policy checked
  302 locked registry versions, the installed tree matched, and audit found
  zero vulnerabilities. Formatting, generation, schema, lint and strict type
  checks passed.
- Installed-wheel acceptance rebuilt the sdist without Node, installed away
  from the checkout, blocked socket connections and completed real-client task
  and blob-helper round trips. Two existing tests now use the injected package
  version rather than `0.1.0`; temporary test environments use POSIX symlinks
  so uv's macOS shared-library interpreter remains usable.
- Inspected both distributions: the new modules and `py.typed` are present,
  public module bytes match the working source, the bundled service reports
  `0.2.0`, and runtime dependencies are unchanged. The private handoff remains
  untracked and is absent from both artifacts. Artifact identities:
  - wheel: `f29e5e1eca7a38f4109953ac893895f7c7fb8f5aa25290760a12fc6c01bfcb85`;
  - sdist: `1fbe86a63778e4c941390157de8aa8864a7c720032238921c66168ea13d37119`.
- Local logs and artifact inspection are preserved at
  `/tmp/tasklease-020-check.log`, `/tmp/tasklease-020-build.log` and
  `/tmp/tasklease-020-artifacts.json`. This is a candidate, not a published
  release: native Linux/Windows acceptance, deployed R2 transfer/upgrade
  acceptance and the remaining clean-commit/tag/publishing steps from the
  release guide are outstanding. No FluxShot repository, deployment, release
  tag or published package was changed.

### PR #8 review fixes

- Follow-up to `b4ad4a2`: encoded, preconsumed blob responses are rejected
  before a partial file is created because HTTPX has discarded the original
  stored bytes. Encoded raw streams and unencoded preloaded responses retain
  support. Signing refusals preserve their typed API error as the explicit
  cause of `BlobTransferError`; HTTP transfer diagnostics remain sanitized.
- Shared `is_task_id` and `is_topic_glob` predicates now serve the store and
  fake. Reply correlation uses `is_request_id` directly. The fake derives
  request member sets from resolved generated annotations, including optional
  fields under Python 3.12, while preserving scalar validation and error
  precedence. Topics List groups tasks once and receives the caller's scopes;
  ordinary number formatting avoids unnecessary Decimal conversion.
- Conformance uses a session admin pool and revokes each target's identities
  during teardown, including after failed tests. Cleanup tolerates explicit
  revocation and continues across individual failures. Regression coverage
  checks resource lifetime, encoded-response handling, typed signing causes,
  shared predicates, optional fields, topic aggregation and numeric boundaries.
- Three simplify passes and two fresh, independent Astra high-effort reviews
  completed. The runtime review identified ambient proxy routing in the new
  pooled admin client. An explicit direct transport and a socket-free proxy
  routing regression fixed it; both Astra reviewers rechecked the correction
  and reported no remaining material findings. Two-pass upload integrity
  checks and stack-safe snapshots are retained. Full Python validator
  generation and a broader transfer framework remain outside these fixes.
- Final `make check` passed on macOS: 191 Worker tests, 32 dashboard tests,
  961 Python/tooling tests with three native Windows skips, and 393
  conformance tests with the same 36 explicit capability skips. Dependency
  policy, audit, generation, formatting, lint and strict type checks passed.
  `make build` passed; inspected wheel/sdist module and test bytes match the
  source, runtime dependencies are unchanged, and the handoff is absent.
- Updated `0.2.0` artifact identities supersede the earlier candidate hashes:
  - wheel: `d030eaf3119161eed3b5b25968f0bd9c6407f5fb7f82e39525027995fa9f7c06`;
  - sdist: `f4109ee80bd98d13e1d99371f4255aade0dd748f70e0bedf5300029f37e844a5`.
  Logs and inspection are in `/tmp/tasklease-pr8-review-fixes-check.log`,
  `/tmp/tasklease-pr8-review-fixes-build.log` and
  `/tmp/tasklease-pr8-review-fixes-artifacts.json`. Release acceptance and
  publication prerequisites remain as recorded above.

### Native and live acceptance of the committed candidate (2026-11-09)

- Qualified clean commit `d0191c4db4ecb7bee3a08e1abd200d7cc108a9ac` from an
  isolated checkout. `make install`, `make check` and `make build` passed there:
  191 Worker, 32 dashboard, 961 Python/tooling and 393 conformance tests, with
  the previously documented three native Windows and 36 capability skips.
  The untracked handoff and private acceptance credentials were excluded.
- Exported one clean, hash-bound native candidate shared by all three systems:
  - ZIP: `eb70ac4d12a96f43f06f2d6238c2764e5945ebfcf4c6b6cdd4f5fcba63dde79e`;
  - source tree: `28a3d045c577bbfb2622aedb784d5bef0dd7ca666d7d1f14423fa957d0c686e5`;
  - wheel: `b542c743a98d08f46c736fe7206e57ec12dc03d4d3fc270a84b1f3a7697790b9`;
  - sdist: `309148f6c2f4e4597533a815118d63614c033b2c59897c78a88f390978671cf8`.
  These artifacts supersede the earlier candidate builds. All native reports
  verify this exact ZIP, source commit/tree and wheel before executing tests.
- Native installed-wheel acceptance passed on macOS arm64/Python 3.12.12 and
  Ubuntu 24.04.4 aarch64/Python 3.12.3: each 868 passed and three expected
  Windows-only skips. Windows 11 Pro build 26200 ARM64, using Python 3.12.12
  x64 under emulation, passed 865 tests with six expected skips. Every
  installed-wheel/no-Node probe passed. Both Windows Task Scheduler parser
  modes preserved non-ASCII paths; no task was registered or worker left
  running. Prerequisites stayed in isolated temporary environments; Windows
  execution policy, registry and global PATH were unchanged.
- The first Windows full run recorded 862 passed, three timeouts and six
  expected skips: a one-second requester wait and both 30-second Scheduler
  subprocesses. Preserved that failed report and separate diagnostics. A full
  unchanged rerun with closed stdin after host servicing settled passed all
  871 cases, including the three earlier failures. ZIP, source, wheel, tests,
  exclusions and all time limits stayed unchanged. Host load and cache state
  also changed, so the evidence does not establish a single cause.
- Bootstrapped a new isolated Worker from the verified published `0.1.0`
  wheel, corroborated against PyPI release metadata, then upgraded it from
  the exact installed candidate. The full held task, admin/scoped credentials
  and hourly statistics survived. Snapshot, atomic reply and stale-ID fencing
  passed. Real R2 rejected wrong signed Content-Length with 403 and no object;
  correct PUT/GET, SHA-256 and a 1 MiB + 17-byte public-helper round trip passed.
- A macOS `Store` submitted a blob-backed request to a Linux `WorkerLoop`.
  Linux verified the input, uploaded the result and atomically replied. macOS
  verified the 262,198-byte result, reopened its durable inbox and confirmed
  server acknowledgement. The temporary Worker and run identities were
  removed. The dedicated test bucket and seven-day lifecycle remain unchanged;
  test objects are retained under that expiry rule. The Windows VM was
  returned to its original suspended state after all evidence was collected.
- A fresh independent Astra high-effort audit verified archive/source/wheel
  bindings, report/JUnit consistency, real upgrade and transfer assertions,
  cross-machine execution and retained failure history. Its initial Windows
  blocker was resolved by the successful full unchanged rerun; no actionable
  acceptance gaps remain. Retention safeguards and a recursive actual-secret
  scan excluded credentials, private state and the untracked handoff.
- Durable evidence is retained under
  `python/dist/acceptance-0.2.0-d0191c4/`, including an `evidence.zip` containing
  95 public evidence files plus their manifest. Its SHA-256 is
  `6444d8c9f6868b5001ed600944704458fc510d0625e8ea7750ea52403bc07635`.
  The tracked [acceptance summary](acceptance/0.2.0.json) records platform
  details, counts, skips, artifact identities, live checks and first-run
  failures. This evidence-only update does not alter qualified package/build
  inputs. Release-note finalization, merge/tag and publication remain separate.

### FluxShot Editor API review follow-up (2026-11-09)

- Addressed the [FluxShot review of PR #8](https://github.com/provos/tasklease/pull/8#issuecomment-6756255222).
  Blob helpers accept optional `total_timeout`, checked against monotonic time
  through upload hashing, signing response bodies and blob body chunks, and
  before committing downloads. Signing and transfer HTTP phases use the
  remaining budget. This is cooperative: trickled response headers can keep
  HTTPX inside a request indefinitely, and blocking filesystem calls/custom
  transports cannot be interrupted. Hard cutoffs require process supervision.
- Blob failures retain safe HTTP status, timeout category, or OS errno and
  standard system description; untrusted exception messages, filenames and
  transfer exception chains remain suppressed. Signing refusals retain their
  typed API cause. `FakeBoard` exposes a fresh `blob_transport()`, public
  `blob_base_url`, frozen `BlobPutGrant` records in detached read-only
  `put_grants`, and an explicit `overwrite_blob()` corruption hook. Documented
  mutable clock advancement needs no new setter. The Worker's 60-second empty
  poller update throttle remains unchanged.
- `WorkerControl(..., on_second_signal=cleanup)` invokes a fast optional
  callback at most once before forced exit. Exit still happens if cleanup
  raises; another signal during cleanup exits immediately. Regression coverage
  includes real POSIX signals as well as callback exceptions and reentry.
- Simplify reuse, quality and efficiency passes completed, including the
  revised signing path. Two fresh independent Astra high-effort reviews found
  that signing response bodies could trickle past the original deadline checks.
  Added streaming checks and a regression that stops after four of 80 chunks;
  both reviewers independently rechecked the correction and reported no
  remaining actionable findings. Focused client/blob tests: 167 passed.
- Final `make check` passed: 191 Worker, 32 dashboard, 1,005 Python/tooling
  and 393 conformance tests, with three native Windows and 36 capability skips.
  Dependency policy/audit, generation, formatting, lint and strict mypy passed.
  `make build` passed; wheel/sdist source parity was verified for all four
  changed runtime modules, with no handoff or private credentials packaged.
- A fresh macOS installed-wheel environment with locked dependencies passed
  912 runtime tests and three expected Windows-only skips, plus the no-Node
  package/resource probe. This precommit regression bundle records dirty source
  based on `16f1792`; it is not clean-commit release qualification. Build hashes:
  - wheel: `3d06c1a58926c9c2b36cecb2aa19854f4c5207adfd518ce4e38d679b117ed4e0`;
  - sdist: `322719724f83a651b848ed9d0f89bfdf1bbbcdb540426a02ee59191b55875263`.
  Logs, artifact inspection, exported source/wheel and macOS report/JUnit are
  retained under `python/dist/validation-0.2.0-fluxshot-review/` with SHA-256
  manifest. Earlier native Linux/Windows, live R2/upgrade and cross-machine
  acceptance remains bound to `d0191c4`. Refresh affected acceptance for the
  final release candidate; publication and dated release notes remain separate.

### Acceptance refreshed for `8e30da0` (2026-11-09)

- Qualified clean commit `8e30da0ab66c8a7416ae2ee3a49f1d9f805c2afd` in an
  isolated checkout with `make install`, `make check` and `make build`.
  Dependency policy/audit, generation, formatting, lint and strict types passed;
  191 Worker, 32 dashboard, 1,005 Python/tooling and 393 conformance tests passed,
  with the expected three Windows-only and 36 backend-capability skips.
- Bound all platforms and live checks to the same clean source and artifacts:
  - ZIP: `8f0c26ef16b526ceb7dcaa5c2d3858fbc2045e10a8c9f1f9edf94ed5f9b1f040`;
  - source tree: `2edb237c4c32429fb2cef4847b471b59c7e5a72b1fc5f459dda276a6cbda4dbd`;
  - wheel: `849ccd17851ee76141cec94396eed0fad2bde03670cff5ec3d55399475397a94`;
  - sdist: `c9461f58cf6393c70d8e67593431ceb7ef12fc7f59740fb937145c730707bd47`.
  Package/source parity, metadata, licenses and bundled assets were inspected.
- Fresh installed-wheel/no-Node acceptance passed on macOS arm64/Python 3.12.12
  and Ubuntu 24.04.4 aarch64/Python 3.12.3: 912 passed, three expected Windows-only
  skips each. Windows 11 Pro build 26200 ARM64 with Python 3.12.12 x64 emulation
  passed 907 tests with eight expected OS skips on its first full attempt.
  Both native Task Scheduler Unicode-string/UTF-8-file parsers and native file
  sharing tests passed. Test limits/exclusions were unchanged, no task was
  registered, PowerShell remained Restricted, and no candidate worker remained.
  The Windows VM was restored to its original suspended state.
- Verified the prior `0.1.0` wheel against fresh PyPI metadata, bootstrapped a
  unique guarded-absent Worker, then upgraded the same Worker using the exact
  installed candidate. Admin/scoped credentials, the complete held task and
  hourly statistics survived. Snapshot, atomic reply and stale-ID fencing passed.
  R2 rejected wrong signed Content-Length with 403 and no object; correct raw
  PUT/GET and SHA-256 passed. Public helpers round-tripped 1 MiB + 17 bytes with
  explicit 60-second total budgets.
- A macOS `Store` submitted a real blob-backed request to the Linux
  `WorkerLoop`; Linux verified the input, uploaded its result and atomically
  replied. macOS verified all 262,198 output bytes, reopened the durable inbox
  and confirmed server acknowledgement. Both machines used the native-verified
  installed wheel and explicit helper budgets. Matching request IDs and hashes
  bind requester/worker reports; the Linux scoped credential file was removed.
- Preserved an expired-OAuth 401 preflight and the initial Cloudflare 10007
  propagation error after upload. Ordinary session refresh and a retry of the
  same run-owned Worker succeeded. No alternate Worker was orphaned. Temporary
  identities were revoked and Worker deletion verified by API 404. Existing
  bucket/lifecycle rules remain unchanged; test objects retain seven-day expiry.
- Retained 78 public files plus their SHA-256 manifest in
  `python/dist/acceptance-0.2.0-8e30da0/evidence.zip`, including the source export,
  candidate wheel/sdist, prior published wheel/provenance, native reports/JUnit,
  commands, cross-machine scripts and failed attempts. Archive SHA-256:
  `ed94a8928e1f6492d72875c56afdd3aa362ccfb78bdd555c0a9d82f2d9dc21e5`.
  Recursive actual-secret scanning excluded credentials/private state; the
  handoff document is absent and was deleted from the working checkout as
  requested. The [acceptance summary](acceptance/0.2.0.json) now qualifies
  `8e30da0`; earlier evidence remains historical. This evidence update changes
  no qualified package/build inputs. Release-note finalization, merge/tag and
  publication remain separate.
- A fresh independent Astra high-effort audit verified all 419 source-export
  payload entries, package bytes/metadata, native JSON/JUnit and expected skips,
  live upgrade/R2 assertions, cross-machine execution, cleanup and final retained
  archive/summary bindings. It also reviewed the actual-secret scan safeguards
  without accessing private credentials. No outstanding acceptance findings
  remain; prior `d0191c4` results were not used to qualify this candidate.

### 0.2.0 publication (2026-11-09)

- Finalized the dated changelog, then squash-merged PR #8 as
  `a0bff8d815b489659a684f39f541f1c39f5f0a27` and created annotated tag `v0.2.0`.
  The merge tree matches the checked release-notes commit. Only documentation
  changed after accepted `8e30da0`; package, service, test, build and dependency
  sources are unchanged. Clean `make install`, `make check` and `make build`
  passed again after release-note finalization.
- The [build-only rehearsal](https://github.com/provos/tasklease/actions/runs/42745964694)
  succeeded; its distributions were inspected before dispatching the
  [successful trusted publication](https://github.com/provos/tasklease/actions/runs/42746347814).
  The publishing run independently passed dependency policy/audit, generation,
  formatting, lint/types, 191 Worker, 32 dashboard, 1,005 Python/tooling and
  393 conformance tests, with the expected three and 36 skips, before rebuilding
  and uploading through OIDC with attestations.
- Fresh PyPI metadata and downloads match that publishing run's distributions:
  - wheel: `3736e042da80adaca05fa68c8e269555fb2016fdb1a57b05c04c0af69b02e778`;
  - sdist: `f60662c5ebe7461c41ffdd99a8cc31e0a263f0384279b22f93193f8e27045f27`.
  Wheel/sdist package bytes and metadata were inspected. Compared with the
  accepted wheel, rebuilt differences are limited to generated bundle README
  timestamps, source-path comments/map paths and their RECORD hashes. Worker
  executable text after comment normalization, source-map contents/mappings,
  Python modules, dashboard, metadata, licenses and templates match acceptance.
- Exported clean tagged source with the verified publishing-run resources and
  actual downloaded PyPI wheel. Post-publication ZIP SHA-256 is
  `f3916a8cedb5d4ff5549b8b73330077c2ee1a46e36289ce96dfe165fc804f29a`;
  tree SHA-256 is `4df48c15b662269156a2cf4b5adaefad8827dec1c20d704792f80f78e1dc961d`.
  Fresh environments on all three platforms installed exact `tasklease==0.2.0`
  from PyPI, separately from dependencies retaining a seven-day age cutoff.
  `tl --version`, `tl --help`, isolated no-Node probes, all packaged byte checks
  and native runtime tests passed: macOS/Linux each 912 with three expected
  Windows-only skips, Windows 907 with eight expected OS skips. Both native
  Task Scheduler parser modes passed; no task was registered or worker left.
- Preserved macOS's initial resolver failure to find the newly published
  version. A fresh uncached run outside the checkout passed with unchanged tests
  and limits. Automatic Windows VM idle pauses delayed orchestration/transfers;
  its idle-pause optimization was temporarily disabled for retrieval, then
  restored to enabled and the VM returned to its original suspended state.
  PowerShell stayed Restricted. All native tests passed without weakened limits.
- Deployed a new guarded-absent isolated Worker from the fresh PyPI installation,
  verified exact installed package bytes and service version `0.2.0`, and passed
  task create/lease/consume, wrong signed-size 403/no-object rejection, raw R2
  SHA-256 and a 1 MiB + 17-byte helper round trip with explicit 60-second budgets.
  The temporary identity was revoked and Worker deletion verified by API 404;
  existing bucket/lifecycle rules remain unchanged, with test objects under
  seven-day expiry. No upgrade of another operator's service was required.
- Created the [GitHub Release](https://github.com/provos/tasklease/releases/tag/v0.2.0)
  after successful checks using the exact finalized changelog text. Retained
  98 public evidence files plus a SHA-256 manifest in
  `python/dist/publication-0.2.0-a0bff8d/evidence.zip`; archive SHA-256 is
  `0e16379f69af3763e7e0739db1e320b2053097413b78619e296c6cfdedb02f84`.
  Recursive actual-secret scanning excluded private credentials/state. The
  [publication summary](acceptance/0.2.0-publication.json) records tag/commit,
  workflow identities, published hashes, native results, diagnostics and cleanup.

## 0.3.0 acceptance and publication (2026-11-10)

- Release commit `cfaecf3a9747dd0704d8101d5d46b8faa483ba8e` includes the
  operator setup fixes from PR #10. Clean `make install`, `make check` and
  `make build` passed, including 191 Worker, 32 dashboard, 1,060 Python/tooling
  and 393 conformance tests. The Python and conformance suites had three and
  36 expected skips, respectively. Wheel and sdist inspection verified
  package metadata, licenses, notices, dashboard and Worker resources.
- The installed candidate wheel passed 967 native tests on macOS and Linux
  with three expected skips each, and 952 on Windows with 18 expected skips.
  Windows passed both native Task Scheduler parser modes and the file sharing
  test. The isolated upgrade from published 0.2.0 preserved the exact held
  task, identity credentials and statistics. Operator CLI setup, stale ID
  fencing, real R2 signed-size enforcement and the macOS-to-Linux durable
  blob request/reply passed. See the [candidate report](acceptance/0.3.0.json).
- The annotated `v0.3.0` tag identifies the accepted commit. Both the
  [build-only rehearsal](https://github.com/provos/tasklease/actions/runs/42898265420)
  and [trusted publishing run](https://github.com/provos/tasklease/actions/runs/42898779319)
  succeeded. PyPI's wheel and sdist matched the publishing artifacts byte for
  byte. Comparison with the candidate wheel accounted for generated
  timestamps, source paths and their RECORD entries.
- Fresh PyPI installations passed the same native test counts and the
  installed-package probes without Node. Initial package index lookups on
  Linux and Windows could not resolve 0.3.0. Both passed in fresh environments
  using the SHA-256-pinned wheel URL from PyPI. The published package also
  passed an isolated live deployment, task flow, signed-size rejection and
  real R2 file transfers.
- Temporary identities were revoked and both isolated Workers returned API
  404 after deletion. Test blobs expire under the existing seven-day lifecycle.
  The Windows VM was returned to its suspended state with idle pausing enabled.
  The [GitHub Release](https://github.com/provos/tasklease/releases/tag/v0.3.0)
  uses the finalized changelog text.
- The [publication report](acceptance/0.3.0-publication.json) records the
  published distribution hashes, native results and cleanup. The local archive
  `python/dist/publication-0.3.0-cfaecf3/evidence.zip` contains 156 files plus
  a SHA-256 manifest and passed recursive credential scanning. Its SHA-256 is
  `b233fe08a9d6e1d0bc1359cf92f2e56706c6fd20a66144ee785c497c749c1f67`.
