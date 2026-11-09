# Repository Guidelines

## Project Structure & Module Organization

- `service/src/`: TypeScript Cloudflare Worker, SQLite Durable Objects, authentication, and R2 integration; tests live in `service/test/`.
- `monitor/src/`: Svelte dashboard, colocated tests, and mock scenarios under `mock/`.
- `python/src/tasklease/`: typed client, requester, worker runtime, `tl` CLI, and local monitor server; tests live in `python/tests/`.
- `schema/`: shared JSON Schema contract and valid/invalid examples; `conformance/`: cross-language integration tests.
- `scripts/`: build and acceptance tooling; `docs/`: specification, design, and release instructions.

## Build, Test, and Development Commands

Use Node.js `^22.12.0 || >=24`, Python 3.12+, and uv. Run from the repository root:

- `make install`: install locked dependencies and check dependency policy.
- `make check`: run dependency/generation checks, formatting, linting, type checks, unit tests, and conformance tests.
- `make test-service`, `make test-python`, `npm run test:monitor`: run focused test suites.
- `make conformance`: test against an isolated local Worker with fake secrets.
- `npm run dev --workspace service`: start the local Worker.
- `npm run monitor:mock`: start the dashboard with mock data and hot reload.
- `make build`: build the Python wheel and sdist, including dashboard/service assets, into `python/dist/`.
- `make format`: apply Prettier and Ruff formatting.

## Coding Style & Naming Conventions

Use two-space indentation for TypeScript/Svelte and four spaces for Python. Prettier uses double quotes and trailing commas; Ruff uses an 88-character line limit. Follow existing camelCase TypeScript functions, PascalCase components/types, and snake_case Python modules/functions. Keep TypeScript and mypy strict checks passing; avoid explicit `any`/`Any`.

For protocol changes, edit `schema/`, add examples, run `make codegen`, and commit generated artifacts. Verify with `make codegen-check`; regenerate files under `generated/` rather than editing them manually.

## Testing Guidelines

Use Vitest (`*.test.ts`) for service/dashboard tests and pytest (`test_*.py`) for Python/conformance tests. Add regression tests for changed behavior and valid/invalid fixtures for contract changes. No numeric coverage threshold is configured. Run `make check` before submitting.

## Commit & Pull Request Guidelines

History uses short, descriptive subjects, often imperative, with optional prefixes such as `docs:` or `Design:`. Keep commits focused. PRs should explain the problem and resulting behavior, link relevant issues, list validation commands/results, and include screenshots for dashboard changes.

Agents must never add a `Co-authored-by:` line to commit messages.

## Security & Configuration Tips

Keep tokens and R2 credentials out of Git and `service.toml`; use environment variables or private token files. Consult `docs/releasing.md` before publication; `make release` uploads packages.
