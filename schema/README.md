# Protocol contract

`common.json` defines reusable types. Each `ops/<operation>.json` defines
`$defs.Request` and `$defs.Response`; the response is a success body, while
all failures use `common.json#/$defs/ErrorResponse`. List operations return
an object with `ok: true` and a `topics`, `pollers`, or `identities` array.
`version` describes the unauthenticated `GET /v1/version` body and has no
`ok` field.

Examples in `examples/<operation>/{valid,invalid}/` use the fixture envelope
`{"kind": "request|response|error", "body": ...}`. Only `body` goes over the
wire. Both the JavaScript and Python suites validate every example against
JSON Schema 2020-12; JavaScript also checks the shipped standalone validators.

Run `make codegen` after changing schemas. It generates TypeScript types,
bundled Ajv standalone validators and their declarations, the request-validator
map, and Python TypedDicts. `make codegen-check` (or
`node scripts/codegen.mjs --check`) regenerates into temporary storage and
compares every artifact without changing files. Both commands also typecheck
real assignments so a generator change cannot silently emit unusable types.

Structural validation does not establish every protocol rule. The service
also checks UTF-8 byte size of serialized task data (256 KiB), decoded cursor
contents, blob key shape/domain ownership, scopes, holder rules, and current
storage state. Oversize task data, Consume lists, and blob sizes return
`too_large`, even though the schema also rejects structural size limits.
