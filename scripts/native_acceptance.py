"""Verify a portable bundle against the current clean wheel installation."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from acceptance_bundle import digest, verify_archive, verify_manifest

_IGNORED = ("test_build.py", "test_deploy.py")
_VERSION_TIMEOUT = 15
_PROBE = r"""
import io, json, plistlib, sys, zipfile
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
import tasklease
from tasklease.cli import main
from tasklease.client import Client

root = Path(tasklease.__file__).resolve().parent
assert sys.prefix != sys.base_prefix, 'Use a clean virtual environment'
assert root.is_relative_to(Path(sys.prefix).resolve()), (
    'Editable/source import is not an installed-wheel check'
)
assert 'site-packages' in str(root).lower(), 'Use installed site-packages'
assert tasklease.__version__ == sys.argv[2], 'Installed version differs'
with zipfile.ZipFile(sys.argv[1]) as wheel:
    for name in wheel.namelist():
        if name.startswith('tasklease/') and not name.endswith('/'):
            relative = PurePosixPath(name).parts[1:]
            assert root.joinpath(*relative).read_bytes() == wheel.read(name), (
                'Installed package differs from wheel: ' + name
            )
p = files('tasklease')
html = p.joinpath('monitor/static/index.html').read_text()
assert '<html' in html and p.joinpath('monitor/static/assets').is_dir()
bundle = json.loads(p.joinpath('service_bundle/manifest.json').read_text())
template = json.loads(p.joinpath('service_bundle/wrangler.template.jsonc').read_text())
assert bundle['service_version'] == tasklease.__version__
assert template['no_bundle'] is True
assert p.joinpath('service_bundle', template['main']).read_bytes()
for target in ('systemd', 'launchd', 'windows', 'windows-stop'):
    output = io.StringIO()
    with redirect_stdout(output):
        assert main(['worker', 'template', '--platform', target]) == 0
    text = output.getvalue()
    assert text
    if target == 'launchd':
        assert plistlib.loads(text.encode())['RunAtLoad'] is True
    if target == 'windows':
        ElementTree.fromstring(text)
Client('http://127.0.0.1:1', 'acceptance-fake-token').close()
print(json.dumps({
    'version': tasklease.__version__, 'package_path': str(root),
    'templates': 4, 'node_path': 'absent',
}))
"""


def clean_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep OS process requirements, dropping credentials and test overrides."""
    excluded = {
        "pythonpath",
        "pythonhome",
        "pytest_addopts",
        "pytest_plugins",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    prefixes = ("tl_", "cloudflare_", "aws_", "r2_", "wrangler_")
    return {
        name: value
        for name, value in environment.items()
        if name.casefold() not in excluded and not name.casefold().startswith(prefixes)
    }


def stop_process(process: subprocess.Popen[bytes], environment: dict[str, str]) -> None:
    if os.name == "nt":
        system_root = next(
            (
                value
                for name, value in environment.items()
                if name.casefold() == "systemroot"
            ),
            None,
        )
        taskkill = Path(system_root) / "System32/taskkill.exe" if system_root else None
        try:
            if taskkill is None or not taskkill.is_file():
                raise OSError("Windows taskkill is unavailable")
            result = subprocess.run(
                [str(taskkill), "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
            if result.returncode != 0:
                raise OSError("Windows taskkill could not stop the process tree")
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        try:
            # Failed acceptance runs must not leave descendants behind even
            # when their launcher already exited or they ignore SIGTERM.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)


def run_logged(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log: Path,
    timeout: int,
) -> dict[str, object]:
    started = time.monotonic()
    timed_out = False
    with log.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process(process, environment)
        except BaseException:
            stop_process(process, environment)
            raise
    return {
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 3),
        "log": str(log),
    }


def junit_summary(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError("Pytest did not produce test-suite results")
    counts = {
        name: sum(int(suite.get(name, "0")) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] <= 0:
        raise ValueError("No native runtime tests were collected")
    counts["passed"] = (
        counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    )
    skipped = []
    failures = []
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        for entry in case:
            if entry.tag == "skipped":
                skipped.append({"test": name, "reason": entry.get("message", "")})
            elif entry.tag in {"failure", "error"}:
                failures.append(
                    {
                        "test": name,
                        "message": entry.get("message", ""),
                        "traceback": entry.text or "",
                    }
                )
    return {**counts, "skipped_cases": skipped, "failed_cases": failures}


def write_report(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(
        prefix=".native-report-", suffix=".json", dir=path.parent
    )
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=True, sort_keys=True, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def accept(
    bundle_root: Path,
    report_path: Path,
    *,
    archive: Path | None = None,
    expected_sha256: str | None = None,
) -> tuple[int, dict[str, object]]:
    bundle_root, report_path = bundle_root.resolve(), report_path.resolve()
    report: dict[str, object] = {
        "status": "failed",
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "ignored_node_suites": list(_IGNORED),
    }
    try:
        if expected_sha256 is not None and archive is None:
            raise ValueError("--expected-sha256 requires --archive")
        if archive is not None:
            archive_bytes = archive.read_bytes()
            report["archive_sha256"] = digest(archive_bytes)
            manifest = verify_archive(
                bundle_root, archive_bytes, expected_sha256=expected_sha256
            )
        else:
            manifest = verify_manifest(bundle_root)
        report.update(
            source_commit=manifest["source_commit"],
            source_dirty=manifest["source_dirty"],
            source_tree_sha256=manifest["source_tree_sha256"],
            package_version=manifest["package_version"],
            wheel_sha256=manifest["files"][manifest["wheel"]],
        )
        source = bundle_root / "source"
        tests = source / "python/tests"
        if (
            not (tests / "test_fsio.py").is_file()
            or not (tests / "test_monitor_server.py").is_file()
        ):
            raise ValueError("The native runtime test source is missing")
        environment = clean_environment(dict(os.environ))
        report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="tasklease-native-work-") as temporary:
            working = Path(temporary)
            uv = shutil.which("uv", path=environment.get("PATH"))
            if uv is not None:
                version_log = report_path.with_suffix(".uv.log")
                version = run_logged(
                    [uv, "--version"],
                    cwd=working,
                    environment=environment,
                    log=version_log,
                    timeout=_VERSION_TIMEOUT,
                )
                with version_log.open(encoding="utf-8", errors="replace") as output:
                    version["version"] = output.read(1024).strip()
                report["uv"] = version
                if version["timed_out"] or version["exit_code"] != 0:
                    raise ValueError("uv version probe failed; see its log")
            else:
                report["uv"] = {"version": "not found; tests use the current Python"}
            probe_env = dict(environment)
            probe_env["PATH"] = ""
            probe = run_logged(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    _PROBE,
                    str(bundle_root / manifest["wheel"]),
                    manifest["package_version"],
                ],
                cwd=working,
                environment=probe_env,
                log=report_path.with_suffix(".probe.log"),
                timeout=60,
            )
            report["installed_wheel_probe"] = probe
            if probe["exit_code"] != 0 or probe["timed_out"]:
                raise ValueError(
                    "Clean installed-wheel/no-Node probe failed; see its log"
                )
            junit = report_path.with_suffix(".junit.xml")
            command = [
                sys.executable,
                "-I",
                "-m",
                "pytest",
                "-c",
                str(source / "python/pyproject.toml"),
                str(tests),
                *("--ignore=" + str(tests / name) for name in _IGNORED),
                "--junitxml=" + str(junit),
                "-q",
            ]
            runtime = run_logged(
                command,
                cwd=working,
                environment=environment,
                log=report_path.with_suffix(".pytest.log"),
                timeout=900,
            )
            report["runtime"] = runtime
            if runtime["timed_out"]:
                raise ValueError("Native runtime tests timed out; see the pytest log")
            summary = junit_summary(junit)
            report["tests"] = summary
            if (
                runtime["exit_code"] != 0
                or summary["failures"]
                or summary["errors"]
                or summary["passed"] == 0
            ):
                raise ValueError(
                    "Native runtime tests did not pass; "
                    "see the report and full pytest log"
                )
        report["status"] = "passed"
        code = 0
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = "Native acceptance was interrupted"
        code = 130
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        ET.ParseError,
        subprocess.SubprocessError,
    ) as error:
        report["error"] = str(error)
        code = 1
    write_report(report_path, report)
    return code, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    code, report = accept(
        args.bundle_root,
        args.report,
        archive=args.archive,
        expected_sha256=args.expected_sha256,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.report.resolve()),
                "tests": report.get("tests"),
                "error": report.get("error"),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
