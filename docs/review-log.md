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

Status: implementation starting after phase 2 acceptance.

- Scope: typed client/configuration/errors, operator commands, pinned-Wrangler
  deploy/status, bundled Worker and wheel/sdist builds, local conformance.
- Read-only deployment preparation verified pinned CLI output shapes and
  lifecycle replacement behavior. Existing OAuth can read R2 metadata; S3
  signing credentials remain unavailable.
- Simplify: pending.
- Independent Astra high review: pending.
- Verification: pending.
