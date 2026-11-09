# Releases

Routine verification runs locally. A manually dispatched Linux-only GitHub
Actions workflow provides optional PyPI Trusted Publishing.
Node is needed to build/check a source checkout and to deploy the bundled
Worker. Installing the resulting wheel or sdist, running requesters/workers
and opening the monitor do not need Node.

## First-release setup

Complete these once, then use the checklist below for the first release and
every later release:

- [ ] Confirm the initial version. The checkout currently uses `0.1.0`; keep
  it for the first release unless there is a reason to choose another version.
  There is no need to increment an unpublished version just to publish it.
- [ ] Review the public metadata in `python/pyproject.toml` and the package
  description in `python/README.md`. Confirm the Apache-2.0 license text in
  `LICENSE` and `python/LICENSE` matches, and verify the metadata and license
  appear in both built distributions.
- [ ] Confirm the PyPI account's pending publisher matches project
  `tasklease`, owner `provos`, repository `tasklease`, workflow `workflow.yml`
  and no required GitHub environment. The repository records this setup, but
  only the account can confirm its current state. Ensure GitHub Actions is
  enabled and the workflow is on `main`. See
  [trusted publisher setup](#publish-through-the-trusted-publisher).
- [ ] Review the outstanding items in the
  [implementation checklist](implementation-checklist.md). Earlier acceptance
  results are recorded in [the review log](review-log.md); qualify the actual
  release candidate when package, service, tests, build tools or dependencies
  have changed since that acceptance.

The first successful trusted-publisher upload creates the PyPI project and
converts the pending publisher to a normal publisher. Later releases reuse
it without repeating registration. A pending publisher does not reserve the
package name; see [PyPI's pending-project documentation](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Checklist for every release

1. **Prepare the version and release notes.** Choose an unused
   `MAJOR.MINOR.PATCH` version in `python/pyproject.toml`, then run
   `uv lock --project python` and review the lockfile diff. Keep `0.1.0` for
   the first release if unchanged. Finalize the entries under `Unreleased` in
   [CHANGELOG.md](../CHANGELOG.md) into a version section with the intended
   release date (`YYYY-MM-DD`), leaving an empty `Unreleased` section above it.
   State whether a service upgrade is required, client/service compatibility,
   and any migration or breaking changes.
2. **Verify the candidate.** Commit the release changes, then run
   `make install`, `make check` and `make build` from a clean checkout. Inspect
   the current version's wheel and sdist, including public metadata, bundled
   dashboard and Worker. See [verification details](#verify-and-build).
3. **Complete acceptance.** Run the
   [native macOS and Windows checks](#native-macos-and-windows-checks), including
   the clean installed-wheel checks on Linux, and the isolated Cloudflare/R2
   checks from the implementation checklist. For subsequent releases, test
   upgrading from the previous published service bundle to this candidate
   while preserving state and credentials. For the first release, test fresh
   bootstrap and the earlier-bundle upgrade flow. Record the candidate commit,
   artifact hashes, commands, results and any expected skips in the review log;
   retain the reports with the release evidence.
4. **Merge and tag the reviewed commit.** Merge the release changes into
   `main` and create an annotated `vMAJOR.MINOR.PATCH` tag using the
   [commands below](#publish-through-the-trusted-publisher). If merging changes
   the candidate's package or build inputs, repeat the affected checks before
   tagging. Published tags identify fixed source; do not move them.
5. **Rehearse the publishing workflow.** Dispatch **Publish tasklease** on
   `main` with that tag and `publish=false` (the default). Confirm it succeeds
   and inspect its `python-distributions` artifact. This builds without
   uploading to PyPI. The artifact is retained for seven days; keep the
   acceptance evidence somewhere durable.
6. **Apply required service upgrades.** When the changelog requires one,
   deploy the accepted wheel's bundled service to services you operate before
   upgrading their clients. Include the same upgrade order in instructions
   for other operators. Every operator controls their own Cloudflare service.
7. **Publish deliberately.** Dispatch the workflow again with the same tag
   and `publish=true`. This reruns checks/build and uploads that run's wheel
   and sdist; it does not promote the earlier build-only artifact. Ordinary
   pushes and tags do not publish anything. See the exact
   [publication commands](#publish-through-the-trusted-publisher).
8. **Verify and announce.** Complete the
   [post-publication checks](#after-publication), then create a GitHub Release
   for the existing tag using the matching changelog entry as its notes.
   The workflow does not create a GitHub Release. Record the PyPI version,
   tag/commit, publishing run URL and verification results. After the first
   release, update the README's installation instructions to use PyPI and
   mark public publication complete in the implementation checklist.

For later releases, repeat this checklist; publisher registration is reused.

## Changelog and version policy

Keep [CHANGELOG.md](../CHANGELOG.md) as the user-facing history. Add meaningful
changes to `Unreleased` as they land, grouping entries under `Added`, `Changed`,
`Fixed`, `Removed` or `Security` as appropriate; omit empty groups. Each release
also needs compatibility and deployment notes, even when no service upgrade
is required. Describe user-visible behavior and required operator actions;
keep detailed test evidence in the review log. Use the same release entry for
GitHub Release notes rather than maintaining a second independent history.

Use patch releases for compatible fixes and minor releases for features.
While the package is `0.x`, put breaking changes in a new minor release and
document them explicitly. At `1.0` and later, breaking changes increment the
major version. The current build tooling and publishing workflow accept only
`MAJOR.MINOR.PATCH`; prereleases such as `0.2.0rc1` need tooling changes first.
The Worker version follows the package version, while protocol `/v1/` is
versioned independently.

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

## Publish through the trusted publisher

The pending PyPI publisher for `tasklease` is configured for GitHub repository
`provos/tasklease`, workflow filename `workflow.yml`, and any environment.
The matching workflow is `.github/workflows/workflow.yml`. No PyPI token or
GitHub secret is needed. The workflow deliberately omits an environment name
so it matches that registration without additional GitHub environment setup.

Merge the workflow into `main` before running it. After updating the package
version and uv lockfile, completing native/isolated upgrade acceptance, and
merging the release commit, create and push a matching tag:

```sh
git tag -a v0.1.0 RELEASE_COMMIT_SHA -m "tasklease 0.1.0"
git push origin v0.1.0
```

Use the intended version and reviewed commit. Tags and ordinary pushes do
not start this workflow. In GitHub's Actions tab choose **Publish tasklease**,
**Run workflow**, branch **main**, and tag `v0.1.0`. Leave **publish** disabled
for a build-only run. The equivalent CLI command is:

```sh
gh workflow run workflow.yml --repo provos/tasklease --ref main -f tag=v0.1.0
```

The workflow requires an existing `vMAJOR.MINOR.PATCH` tag reachable from
`main`, checks its package name/version, and builds that exact tagged commit.
It runs `make install`, `make check`, and `make build` on Linux, preserving the
dependency age/audit gates, then saves the wheel and sdist for seven days.
Actions and build tool versions are pinned. The build job has read-only
repository access and no OIDC permission.

To authorize publication, explicitly enable **publish**, or run:

```sh
gh workflow run workflow.yml --repo provos/tasklease --ref main \
  -f tag=v0.1.0 -f publish=true
```

This reruns the checks/build, then a separate job downloads only that run's
distributions and publishes them using PyPA's action with job-scoped
`id-token: write`. It checks package metadata and creates publishing
attestations. The publishing job does not check out or execute repository
build code. Existing PyPI files are not silently skipped on a repeated run.
The first successful upload creates the pending PyPI project and converts
its publisher to a normal trusted publisher. The pending entry does not
reserve the project name until that upload succeeds.

This manual workflow is the only GitHub Actions execution added here; native
acceptance and isolated service upgrade checks remain explicit release
prerequisites. Deploy the service first when release notes require a service
change. PyPI distributions expose their package source even when the GitHub
repository is private. Creating or merging this workflow does not publish.

See PyPI's [publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
and [pending-project behavior](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
If OIDC authentication fails, check the exact repository and workflow filename
in PyPI, and confirm that only the publishing job has `id-token: write`.

## Publish locally

The existing `make release` command remains available for an explicitly
requested local publication. It runs checks/build, then uploads only the
current version's exact wheel and sdist with `uv publish`. Local publication
outside an OIDC provider requires a project-scoped PyPI token supplied through
`UV_PUBLISH_TOKEN`, never a command argument, source file or acceptance bundle.
A pending GitHub publisher does not authenticate local commands.

## After publication

After either publication path:

- Confirm PyPI lists the expected version, wheel and sdist, and check their
  metadata and hashes against the publishing run's artifacts.
- Install the exact version from PyPI outside the checkout in a fresh
  environment. For example, on Linux or macOS (replace `0.1.0` throughout for
  later releases):

  ```sh
  cd /tmp
  uv run --no-project --isolated --python 3.12 \
    --default-index https://pypi.org/simple --refresh-package tasklease \
    --with tasklease==0.1.0 -- tl --version
  uv run --no-project --isolated --python 3.12 \
    --default-index https://pypi.org/simple --refresh-package tasklease \
    --with tasklease==0.1.0 -- tl --help
  ```

  Expect `tl 0.1.0` and successful help output. Repeat clean PyPI installation
  on the supported Linux, macOS and Windows machines; on Windows use a
  temporary directory appropriate to that machine. Probe the packaged
  dashboard and service bundle as in native acceptance, and verify an
  isolated deployment from the PyPI installation. See uv's official
  [build and publication guide](https://docs.astral.sh/uv/guides/package/).
- Record the published version, tag/commit, workflow run, distribution hashes
  and installation/deployment results in the release evidence. Publish the
  GitHub Release notes from the changelog after verification succeeds.

If an upload or post-publication check fails, inspect the workflow and PyPI
files before retrying: the upload may have partially succeeded. This workflow
does not skip existing files. [PyPI does not allow replacing a published file](https://docs.pypi.org/publishing/troubleshooting/)
with corrected bytes; fix the issue in a new version and tag rather than
moving the old tag. Consider yanking an unusable release and explain recovery
in the changelog; see [PyPI's yanking documentation](https://docs.pypi.org/project-management/yanking/).
