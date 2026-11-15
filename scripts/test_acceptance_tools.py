"""Local integrity and failure-boundary checks for native acceptance tools."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import venv
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from acceptance_bundle import (
    MANIFEST_NAME,
    digest,
    safe_name,
    tree_digest,
    verify_archive,
    verify_manifest,
)
from native_acceptance import (
    accept,
    clean_environment,
    junit_summary,
    run_logged,
    stop_process,
    write_report,
)
from prepare_acceptance import matching_wheel, prepare
from public_evidence import MANIFEST as PUBLIC_MANIFEST
from public_evidence import Redactor, export


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    root.mkdir()
    contents = {
        ".gitignore": (
            "python/src/tasklease/monitor/static/\n"
            "python/src/tasklease/service_bundle/\n"
        ),
        "README.md": "test project\n",
        "python/pyproject.toml": '[project]\nname="tasklease"\nversion="0.1.0"\n',
        "python/src/tasklease/__init__.py": '__version__="0.1.0"\n',
        "python/tests/test_fsio.py": "def test_placeholder(): pass\n",
        "python/tests/test_monitor_server.py": "def test_placeholder(): pass\n",
        "python/src/tasklease/monitor/static/index.html": "<html></html>\n",
        "python/src/tasklease/service_bundle/index.js": "export default {};\n",
    }
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Acceptance Test",
        "-c",
        "user.email=acceptance@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    wheel = tmp_path / "tasklease-0.1.0-py3-none-any.whl"
    package = root / "python/src"
    with zipfile.ZipFile(wheel, "w") as output:
        for path in package.rglob("*"):
            if path.is_file():
                output.write(path, path.relative_to(package).as_posix())
        output.writestr(
            "tasklease-0.1.0.dist-info/METADATA", "Name: tasklease\nVersion: 0.1.0\n"
        )
    return root, wheel


def unpack(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)


def test_export_tracks_exact_source_wheel_and_ignored_assets(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, wheel = project
    archive = tmp_path / "candidate.zip"
    result = prepare(root, wheel, archive)
    assert result["source_dirty"] is False
    assert result["sha256"] == digest(archive.read_bytes())
    assert (
        archive.with_suffix(".zip.sha256").read_text().startswith(str(result["sha256"]))
    )
    if os.name == "posix":
        assert archive.stat().st_mode & 0o777 == 0o600
    unpack(archive, tmp_path / "unpacked")
    manifest = verify_manifest(tmp_path / "unpacked")
    assert (
        verify_archive(
            tmp_path / "unpacked",
            archive.read_bytes(),
            expected_sha256=str(result["sha256"]),
        )
        == manifest
    )
    assert manifest["files"]["artifacts/" + wheel.name] == digest(wheel.read_bytes())
    assert "source/python/src/tasklease/monitor/static/index.html" in manifest["files"]
    assert "source/python/src/tasklease/service_bundle/index.js" in manifest["files"]
    assert not any(".git/" in name for name in manifest["files"])
    assert manifest["source_tree_sha256"] == tree_digest(manifest["files"])


def test_dirty_export_is_explicit_and_does_not_collect_untracked_files(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, wheel = project
    (root / "README.md").write_text("changed\n")
    (root / "notes.txt").write_text("untracked, do not export\n")
    with pytest.raises(ValueError, match="clean checkout"):
        prepare(root, wheel, tmp_path / "candidate.zip")
    prepare(root, wheel, tmp_path / "candidate.zip", allow_dirty=True)
    unpack(tmp_path / "candidate.zip", tmp_path / "unpacked")
    manifest = verify_manifest(tmp_path / "unpacked")
    assert manifest["source_dirty"] is True
    assert "source/notes.txt" not in manifest["files"]
    assert (tmp_path / "unpacked/source/README.md").read_text() == "changed\n"


@pytest.mark.parametrize(
    "name",
    [
        ".env.acceptance",
        ".dev.vars",
        ".aws/credentials",
        "token",
        "node_modules/key",
        ".venv/data",
    ],
)
def test_even_tracked_credentials_and_caches_cannot_be_exported(
    project: tuple[Path, Path], tmp_path: Path, name: str
) -> None:
    root, wheel = project
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must never be exported")
    git(root, "add", "-f", name)
    with pytest.raises(ValueError, match="private or cached"):
        prepare(root, wheel, tmp_path / "candidate.zip", allow_dirty=True)
    assert not (tmp_path / "candidate.zip").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        "source/../key",
        "source\\key",
        "C:/key",
        "source/CON.txt",
        "source/foo.",
    ],
)
def test_nonportable_manifest_paths_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        safe_name(name)


def test_export_rejects_stale_same_version_wheel(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, wheel = project
    (root / "python/src/tasklease/__init__.py").write_text("changed = True\n")
    with pytest.raises(ValueError, match="Wheel differs"):
        prepare(root, wheel, tmp_path / "candidate.zip", allow_dirty=True)


def test_wheel_cannot_add_unrecorded_code_or_top_level_install_hooks(
    project: tuple[Path, Path],
) -> None:
    root, wheel = project
    source = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "python/src").rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(wheel, "a") as output:
        output.writestr("unrecorded.pth", "import not_allowed\n")
    with pytest.raises(ValueError, match="Unexpected wheel member"):
        matching_wheel(wheel, source, "0.1.0")


@pytest.mark.skipif(
    os.name == "nt", reason="Windows symlink creation can require privilege"
)
def test_export_does_not_follow_source_symlinks(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, wheel = project
    link = root / "external.txt"
    link.symlink_to(tmp_path / "private.txt")
    (tmp_path / "private.txt").write_text("private")
    git(root, "add", "external.txt")
    with pytest.raises(ValueError, match="symlinks"):
        prepare(root, wheel, tmp_path / "candidate.zip", allow_dirty=True)


def test_manifest_detects_changed_bytes_and_path_collisions(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, wheel = project
    archive = tmp_path / "candidate.zip"
    prepare(root, wheel, archive)
    unpack(archive, tmp_path / "unpacked")
    bundle = tmp_path / "unpacked"
    (bundle / "source/README.md").write_text("tampered")
    with pytest.raises(ValueError, match="digest does not match"):
        verify_manifest(bundle)
    manifest = json.loads((bundle / MANIFEST_NAME).read_text())
    manifest["files"]["source/readme.md"] = manifest["files"]["source/README.md"]
    manifest["source_tree_sha256"] = tree_digest(manifest["files"])
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="case-insensitive"):
        verify_manifest(bundle)


def test_new_verified_zip_cannot_certify_old_extracted_source(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wheel = project
    old = tmp_path / "old.zip"
    prepare(root, wheel, old)
    bundle = tmp_path / "old-extracted"
    unpack(old, bundle)
    (root / "README.md").write_text("new candidate source\n")
    new = tmp_path / "new.zip"
    prepare(root, wheel, new, allow_dirty=True)

    def unexpected(*args: object, **kwargs: object) -> dict[str, object]:
        pytest.fail("A stale extraction must be rejected before any command runs")

    monkeypatch.setattr("native_acceptance.run_logged", unexpected)
    code, report = accept(
        bundle,
        tmp_path / "native.json",
        archive=new,
        expected_sha256=digest(new.read_bytes()),
    )
    assert code == 1 and report["status"] == "failed"
    assert "Extracted manifest differs from the verified ZIP" in str(report["error"])
    assert report["archive_sha256"] == digest(new.read_bytes())
    assert "installed_wheel_probe" not in report and "runtime" not in report


@pytest.mark.parametrize("extra", ["extra.txt", "../private", "source/README.md"])
def test_archive_rejects_undeclared_unsafe_and_duplicate_members(
    project: tuple[Path, Path], tmp_path: Path, extra: str
) -> None:
    root, wheel = project
    archive = tmp_path / "candidate.zip"
    prepare(root, wheel, archive)
    bundle = tmp_path / "extracted"
    unpack(archive, bundle)
    with zipfile.ZipFile(archive, "a") as output:
        if extra == "source/README.md":
            with pytest.warns(UserWarning, match="Duplicate"):
                output.writestr(extra, "unexpected")
        else:
            output.writestr(extra, "unexpected")
    with pytest.raises(ValueError):
        verify_archive(bundle, archive.read_bytes())


def test_archive_checks_member_bytes_against_its_bound_manifest(
    project: tuple[Path, Path], tmp_path: Path
) -> None:
    root, wheel = project
    original = tmp_path / "candidate.zip"
    prepare(root, wheel, original)
    bundle = tmp_path / "extracted"
    unpack(original, bundle)
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(changed, "w") as output:
        for name in source.namelist():
            content = b"changed" if name == "source/README.md" else source.read(name)
            output.writestr(name, content)
    with pytest.raises(ValueError, match="ZIP member digest does not match"):
        verify_archive(bundle, changed.read_bytes())


def test_subprocess_environment_excludes_ambient_credentials_and_overrides() -> None:
    environment = {
        "PATH": "tools",
        "SystemRoot": "Windows",
        "HOME": "preserved",
        "TL_TOKEN": "private",
        "cloudflare_api_token": "private",
        "AWS_ACCESS_KEY_ID": "private",
        "R2_SECRET_ACCESS_KEY": "private",
        "WRANGLER_CI_OVERRIDE_NAME": "private",
        "HTTPS_PROXY": "private",
        "PYTHONPATH": "untrusted",
        "PYTEST_ADDOPTS": "--collect-only",
    }
    assert clean_environment(environment) == {
        "PATH": "tools",
        "SystemRoot": "Windows",
        "HOME": "preserved",
    }


def test_junit_keeps_failures_and_platform_skip_reasons(tmp_path: Path) -> None:
    path = tmp_path / "report.xml"
    path.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1">'
        '<testcase classname="fsio" name="failure">'
        '<failure message="broken">full traceback</failure></testcase>'
        '<testcase classname="control" name="signal">'
        '<skipped message="POSIX only"/></testcase>'
        '<testcase classname="store" name="ok"/></testsuite></testsuites>'
    )
    result = junit_summary(path)
    assert result["tests"] == 3 and result["passed"] == 1
    assert result["failed_cases"] == [
        {"test": "fsio::failure", "message": "broken", "traceback": "full traceback"}
    ]
    assert result["skipped_cases"] == [
        {"test": "control::signal", "reason": "POSIX only"}
    ]
    path.write_text('<testsuites><testsuite tests="0"/></testsuites>')
    with pytest.raises(ValueError, match="No native"):
        junit_summary(path)


def test_missing_wheel_installation_produces_failure_report_not_a_false_pass(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wheel = project
    archive = tmp_path / "candidate.zip"
    prepare(root, wheel, archive)
    bundle = tmp_path / "bundle"
    unpack(archive, bundle)
    installation = tmp_path / "empty-environment"
    # Match the venv CLI's POSIX default so uv's shared-library Python works.
    venv.EnvBuilder(symlinks=os.name != "nt").create(installation)
    executable = installation / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr("native_acceptance.shutil.which", lambda *args, **kwargs: None)
    monkeypatch.setenv("TL_TOKEN", "ambient-secret-must-not-be-recorded")
    report_path = tmp_path / "native.json"
    code, report = accept(
        bundle,
        report_path,
        archive=archive,
        expected_sha256=digest(archive.read_bytes()),
    )
    assert code == 1 and report["status"] == "failed"
    assert "probe failed" in str(report["error"])
    assert "runtime" not in report
    assert "ambient-secret" not in report_path.read_text()
    assert (
        "No module named 'tasklease'"
        in report_path.with_suffix(".probe.log").read_text()
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
@pytest.mark.parametrize("resists_term", [False, True])
def test_timeout_stops_the_child_process_tree(
    tmp_path: Path, resists_term: bool
) -> None:
    marker = tmp_path / "orphan.txt"
    child = (
        "import time,signal; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if resists_term else "")
        + "time.sleep(2.5); Path("
        + repr(str(marker))
        + ").write_text('orphan')"
    )
    parent = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]); time.sleep(30)"
    )
    result = run_logged(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        environment=clean_environment(dict(os.environ)),
        log=tmp_path / "timeout.log",
        timeout=1,
    )
    assert result["timed_out"] is True and result["exit_code"] != 0
    time.sleep(2)
    assert not marker.exists()


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX executable wrapper and process group"
)
def test_uv_version_timeout_cleans_resistant_wrapper_descendant(
    project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, wheel = project
    archive = tmp_path / "candidate.zip"
    prepare(root, wheel, archive)
    bundle = tmp_path / "bundle"
    unpack(archive, bundle)
    marker = tmp_path / "uv-orphan.txt"
    child = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(2.5); Path("
        + repr(str(marker))
        + ").write_text('orphan')"
    )
    parent = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]); time.sleep(30)"
    )
    uv = tmp_path / "uv-wrapper"
    uv.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " -c "
        + shlex.quote(parent)
        + "\n"
    )
    uv.chmod(0o700)
    monkeypatch.setattr(
        "native_acceptance.shutil.which", lambda *args, **kwargs: str(uv)
    )
    monkeypatch.setattr("native_acceptance._VERSION_TIMEOUT", 1)
    code, report = accept(
        bundle,
        tmp_path / "native.json",
        archive=archive,
        expected_sha256=digest(archive.read_bytes()),
    )
    assert code == 1 and report["status"] == "failed"
    assert "uv version probe failed" in str(report["error"])
    assert "installed_wheel_probe" not in report
    time.sleep(2)
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["timeout", "oserror", "nonzero"])
def test_windows_taskkill_failure_still_kills_and_reaps_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class Process:
        pid = 12345
        killed = False
        reaped = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

        def wait(self, *, timeout: int) -> int:
            self.reaped = True
            return 0

    path = tmp_path / "System32/taskkill.exe"
    path.parent.mkdir()
    path.touch()

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 15)
        if failure == "oserror":
            raise OSError("Could not run taskkill")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr("native_acceptance.os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("native_acceptance.subprocess.run", run)
    process = Process()
    stop_process(cast(subprocess.Popen[bytes], process), {"SystemRoot": str(tmp_path)})
    assert process.killed and process.reaped


def test_public_report_redacts_paths_and_identifiers_without_changing_results(
    tmp_path: Path,
) -> None:
    original: dict[str, object] = {
        "status": "passed",
        "source_commit": "a" * 40,
        "wheel_sha256": "b" * 64,
        "tests": {"passed": 42, "skipped": 3},
        "python_executable": "/Users/Jane Doe/venv/bin/python",
        "log": r"C:\Users\Jörg Smith\AppData\Local\Temp\run-123\probe.log",
        "cwd": "/private/var/folders/ab/private-id/T/run-123",
        "cloudflare": {
            "account_id": "0123456789abcdef0123456789abcdef",
            "bucket": "private-evidence-bucket",
            "url": "https://private-worker.personal.workers.dev",
            "name": "private-worker",
        },
        "normal_host": "buildbox.localdomain",
        "message": "private-evidence-bucket on 192.168.1.12",
    }
    path = tmp_path / "native.json"
    write_report(path, original)
    result = json.loads(path.read_text())
    for field in ("status", "source_commit", "wheel_sha256", "tests"):
        assert result[field] == original[field]
    for private in (
        "Jane Doe",
        "Jörg Smith",
        "private-id",
        "run-123",
        "0123456789abcdef0123456789abcdef",
        "private-evidence-bucket",
        "private-worker",
        "personal.workers.dev",
        "buildbox.localdomain",
        "192.168.1.12",
    ):
        assert private not in path.read_text()
    assert original["python_executable"] == "/Users/Jane Doe/venv/bin/python"


def test_public_zip_redacts_nested_logs_and_keeps_verifiable_public_manifest(
    tmp_path: Path,
) -> None:
    account = "0123456789abcdef0123456789abcdef"
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr(
            "native.xml",
            '<testsuite hostname="buildbox.local" tests="42"/>'.encode("utf-16"),
        )
        archive.writestr("command.log", f"account {account}; bucket private-bucket")
    original = tmp_path / "checks.zip"
    with zipfile.ZipFile(original, "w") as archive:
        archive.write(nested, "logs.zip")
        archive.writestr(
            "live.log",
            json.dumps({"account_id": account, "bucket": "private-bucket"})
            + "\nsecond log line\n",
        )
        archive.writestr("summary.json", '{"passed":42,"wheel_sha256":"unchanged"}')
    before = original.read_bytes()
    output = tmp_path / "public.zip"
    assert export(original, output) == digest(output.read_bytes())
    assert original.read_bytes() == before
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read(PUBLIC_MANIFEST))
        assert manifest["source_sha256"] == digest(before)
        assert manifest["metadata_redacted"] is True
        assert json.loads(archive.read("summary.json"))["passed"] == 42
        for name, checksum in manifest["files"].items():
            assert digest(archive.read(name)) == checksum
        nested.write_bytes(archive.read("logs.zip"))
        assert account.encode() not in archive.read("live.log")
    with zipfile.ZipFile(nested) as archive:
        assert "buildbox.local" not in archive.read("native.xml").decode("utf-16")
        assert account.encode() not in archive.read("command.log")
        assert b"private-bucket" not in archive.read("command.log")


@pytest.mark.parametrize("name", ["package.whl", "private.png", "source.tar.gz"])
def test_public_export_rejects_packages_and_unreviewed_binary_files(
    tmp_path: Path, name: str
) -> None:
    source = tmp_path / "evidence.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(name, "not a report")
    output = tmp_path / "public.zip"
    with pytest.raises(ValueError, match="only reports/logs"):
        export(source, output)
    assert not output.exists()


@pytest.mark.parametrize("name", ["../private.log", "/private.log", "host.local.log"])
def test_public_export_rejects_unsafe_or_identifying_member_names(
    tmp_path: Path, name: str
) -> None:
    source = tmp_path / "evidence.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(name, "report")
    with pytest.raises(ValueError):
        export(source, tmp_path / "public.zip")


def test_public_export_preserves_original_and_failed_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.json"
    source.write_text('{"status":"passed"}')
    with pytest.raises(ValueError, match="original evidence"):
        export(source, source)
    assert source.read_text() == '{"status":"passed"}'
    invalid = tmp_path / "broken.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        export(invalid, source)
    assert source.read_text() == '{"status":"passed"}'


def test_redaction_keeps_markdown_quotes_and_public_digests() -> None:
    redactor = Redactor()
    text = "Log `/tmp/private-run/output.log`; Python '/home/person/bin/python'."
    assert redactor.text(text) == "Log `[TEMP]/output.log`; Python '[HOME]/bin/python'."
    assert redactor.text("a" * 64) == "a" * 64


def test_xml_and_shell_metadata_do_not_require_a_json_sibling() -> None:
    redactor = Redactor()
    text = (
        '<testsuite hostname="private-macbook" tests="42"/>'
        '\naccount_id="0123456789abcdef0123456789abcdef"'
        "\nbucket='private-bucket'"
        "\nprivate-macbook used private-bucket"
    )
    redactor.discover(text)
    result = redactor.text(text)
    for private in (
        "private-macbook",
        "0123456789abcdef0123456789abcdef",
        "private-bucket",
    ):
        assert private not in result
    assert 'tests="42"' in result


def test_metadata_collisions_do_not_change_status_or_counts() -> None:
    redactor = Redactor()
    text = '{"bucket":"passed","status":"passed","tests":{"passed":42}}'
    redactor.discover(text)
    assert json.loads(redactor.document(text)) == {
        "bucket": "[BUCKET]",
        "status": "passed",
        "tests": {"passed": 42},
    }


def test_private_map_keys_are_redacted_without_silently_merging_results() -> None:
    redactor = Redactor()
    assert redactor.value({"/Users/private/report.log": 42}) == {
        "[HOME]/report.log": 42
    }
    with pytest.raises(ValueError, match="merge report map keys"):
        redactor.value({"one.local": 42, "two.local": 43})


def test_nested_zip_expansion_uses_one_total_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("public_evidence._MAX_EXPANDED_BYTES", 1500)
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.log", "x" * 800)
    source = tmp_path / "original.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(nested, "one.zip")
        archive.write(nested, "two.zip")
    with pytest.raises(ValueError, match="too large"):
        export(source, tmp_path / "public.zip")
