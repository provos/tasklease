# Local verification and releases

The release workflow runs locally; this project does not use GitHub Actions.
Node is needed to build/check a source checkout and to deploy the bundled
Worker. Installing the resulting wheel or sdist, running requesters/workers
and opening the monitor do not need Node.

## Verify and build

From the repository root:

```sh
make install
make check
make build
```

`make install` checks every locked npm registry version, including optional
packages for other platforms, before `npm ci`. The check verifies publication
at least seven days ago and the registry tarball/integrity pair. Do not replace
this step with plain `npm ci`: npm's age setting does not reliably reject young
versions already pinned in a lockfile. Registry failures fail the check.
`make check` also compares the installed dependency tree with the lockfile,
runs policy regression tests and requires a clean `npm audit` including dev
dependencies. Source asset builds repeat the installed-tree check before
loading build tools.

Repository `.npmrc` disables dependency lifecycle scripts. The pinned esbuild
and workerd optional native packages provide their binaries without those
scripts. The sole local dependency exception is a reviewed
[deepmerge security backport](../scripts/dependency-policy-vendor/deepmerge/README.md)
with pinned source hashes, upstream provenance, license and regression tests.
It fixes the advisory that currently has no upstream patched release; remove
the backport when an aged compatible fix becomes available. Updating dependency
pins requires rerunning these checks and the service/frontend build gates.

This policy qualifies the source checkout's locked build graph. Installed
`tl service deploy` uses `npx` with the Wrangler version from the bundled
manifest; npm resolves its transitive dependencies under the operator's npm
configuration, outside this checkout's lockfile.

The gates cover generated contract drift, shared examples, strict TypeScript
and Python checks, Workers tests, Svelte component/model tests, Python tests
and real local Worker conformance. The packaging tests build a wheel from an
sdist with Node absent, install it away from the checkout and verify the
dashboard, Worker bundle and startup templates. Artifacts are written to
`python/dist/`.

For a release, update the version in `python/pyproject.toml`, refresh the uv
lockfile, and describe any required service upgrade in release notes. The
service version is derived from that package version; `/v1/` is independent.
Keep only the intended version's wheel and sdist when reviewing publication.

## Export a native acceptance candidate

After checks, builds and the review checkpoint, create a ZIP outside the
checkout:

```sh
python scripts/prepare_acceptance.py \
  --wheel python/dist/tasklease-0.1.0-py3-none-any.whl \
  --output /tmp/tasklease-acceptance.zip
```

Use the current artifact version in the filename. The default requires a
clean Git checkout. `--allow-dirty` is available for an explicitly reviewed
candidate: it records dirty status and includes tracked/staged bytes, never
arbitrary untracked files. New files must be staged or committed first.

The exporter includes source, built resources and the matching wheel. It
rejects private/cache paths and symlinks, checks Windows-portable filenames,
and compares every packaged `tasklease/` member with source before exporting.
A same-version stale wheel is rejected. The manifest records the commit,
dirty status, source-tree hash and each file's SHA-256. The command prints the
ZIP's SHA-256 and writes a `.zip.sha256` sidecar. Share the expected ZIP hash
through the trusted handoff, separately from its extracted manifest.

The bundle contains private repository source when the repository is private.
Transfer it directly to the intended machine. No Cloudflare, R2, PyPI or other
credentials belong in the bundle or acceptance report.

## Native macOS and Windows checks

Use a fresh virtual environment outside the extracted source, install the
provided wheel plus `pytest>=8,<10` and `jsonschema>=4,<5`, then run:

```sh
PATH_TO_VENV_PYTHON PATH_TO_BUNDLE/source/scripts/native_acceptance.py \
  --bundle-root PATH_TO_BUNDLE \
  --archive PATH_TO_ZIP \
  --expected-sha256 EXPECTED_ZIP_SHA256 \
  --report PATH_OUTSIDE_BUNDLE/native-report.json
```

The runner binds the extracted manifest and files to the exact verified ZIP
before executing commands, so a new ZIP cannot certify an old extraction.
It removes ambient
tasklease/Cloudflare/AWS/R2 credentials, proxy settings and pytest/Python path
overrides from child environments. It probes the installed wheel outside the
checkout with isolated Python and an empty `PATH`, then runs runtime tests
against that installation. Only `test_build.py` and `test_deploy.py` are
omitted because their Node/build/deploy coverage runs on Linux. Native test
counts can differ by OS because platform-specific cases are skipped; the
JSON report lists every skip and failure. Full uv/probe/pytest logs and JUnit XML are saved
beside it. A failed probe, failed test run or zero collected tests cannot
produce a passing report.

Use [the Windows agent prompt](windows-agent-prompt.md) for exact PowerShell
steps and the required result. Do not weaken package security checks if a
new development dependency is blocked by a minimum-age policy; native runtime
acceptance does not need codegen dependencies. An age-qualified pytest and
JSON Schema environment is sufficient. Native results do not by themselves
prove physical power-loss behavior or unsupported network filesystems.

Before release, also run the isolated Cloudflare/R2 checks and upgrade
preservation flow in [the implementation checklist](implementation-checklist.md).
Use a separate Worker, bucket and configuration; never redeploy another
operator's service as a test.

## Publish explicitly

Public package publication needs explicit authorization and a project-scoped
PyPI token on the release machine. PyPI packages include source, even when
the Git repository is private. Supply the token through `UV_PUBLISH_TOKEN`,
never a command argument, source file or acceptance bundle.

`make release` runs the verification and build gates, then invokes `uv publish`
for the current version's exact wheel and sdist. It is a publication command;
`make check`, `make build` and acceptance export do not publish. Deploy the
service first when the release notes require a service change, then publish
only after the native and isolated upgrade checks are accepted. No publication
is part of preparing or reviewing these instructions.

After an authorized publication, verify installation from PyPI in a separate
environment and record the published version and checks. See uv's official
[build and publication guide](https://docs.astral.sh/uv/guides/package/) for
token and artifact handling.
