"""Small, version-specific Wrangler subprocess and output boundary."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit


class DeployError(Exception):
    """A failed step, or missing operator input."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise DeployError(f"Malformed {label}: expected an object.")
    return cast(dict[str, object], value)


def json_value(text: str, label: str) -> object:
    try:
        return cast(object, json.loads(text))
    except ValueError as exc:
        raise DeployError(f"Malformed {label}: expected JSON.") from exc


def string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DeployError(f"Malformed {label}: expected a string array.")
    return cast(list[str], value)


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop the launcher and its children before releasing its temporary project."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        try:
            # Kill the group even if npx has already exited and only Node remains.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        # A pipe inherited by a detached descendant must not make cleanup hang.
        if process.poll() is None:
            process.kill()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        process.wait(timeout=5)


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environ: Mapping[str, str],
    input_text: str = "",
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Use one process group for npx's wrapper and the actual Wrangler process."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environ,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except BaseException:
        _stop_process_tree(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class Wrangler:
    """Run the manifest pin; never put a secret in an argument or file."""

    def __init__(self, version: str, cwd: Path, environ: Mapping[str, str]) -> None:
        self.version = version
        self.cwd = cwd
        self.environ = dict(environ)
        self.redactions = [
            value
            for key, value in environ.items()
            if value
            and any(
                part in key for part in ("TOKEN", "SECRET", "ACCESS_KEY", "API_KEY")
            )
        ]
        for key in (
            "TL_TOKEN",
            "TL_ADMIN_TOKEN",
            "TL_R2_ACCESS_KEY_ID",
            "TL_R2_SECRET_ACCESS_KEY",
        ):
            self.environ.pop(key, None)
        # The generated top-level config selects the target. These inherited
        # Wrangler settings can otherwise redirect an upload before its output
        # reveals that the wrong Worker was changed.
        self.environ.pop("CLOUDFLARE_ENV", None)
        self.environ.pop("WRANGLER_CI_OVERRIDE_NAME", None)
        self.environ["NO_COLOR"] = "1"
        self.environ["FORCE_COLOR"] = "0"
        self.environ["CI"] = "true"
        self.environ["WRANGLER_SEND_METRICS"] = "false"
        # Wrangler's debug INIT record can contain the complete secret bulk body.
        # Override inherited debug settings to uphold the no-secret-files contract.
        self.environ["WRANGLER_LOG"] = "log"
        self.environ["WRANGLER_LOG_SANITIZE"] = "true"
        self.environ["WRANGLER_WRITE_LOGS"] = "false"
        self.environ["WRANGLER_LOG_PATH"] = str(cwd / "wrangler.log")
        # A caller's output destination must not receive deployment records.
        self.environ.pop("WRANGLER_OUTPUT_FILE_PATH", None)
        self.environ.pop("WRANGLER_OUTPUT_FILE_DIRECTORY", None)
        self.executable = shutil.which("npx", path=environ.get("PATH"))

    def redact(self, text: str) -> str:
        for value in sorted(self.redactions, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text

    def check_node(self) -> None:
        node = shutil.which("node", path=self.environ.get("PATH"))
        if node is None or self.executable is None:
            raise DeployError(
                "Install Node.js (current LTS, including npx) before deploying.", 4
            )
        try:
            result = _run_process(
                [node, "--version"],
                timeout=15,
                environ=self.environ,
                cwd=self.cwd,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeployError(
                "Cannot run Node.js; install a current LTS release.", 4
            ) from exc
        match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+\s*", result.stdout)
        if result.returncode or match is None:
            raise DeployError("Cannot determine the installed Node.js version.", 4)
        major, minor = int(match[1]), int(match[2])
        if not ((major == 22 and minor >= 12) or major >= 24):
            raise DeployError("Wrangler requires Node.js 22.12+ or 24+.", 4)

    def run(
        self,
        args: Sequence[str],
        *,
        secrets: Mapping[str, str] | None = None,
        output_file: Path | None = None,
        timeout: float = 180,
    ) -> str:
        if self.executable is None:
            raise DeployError("npx is unavailable; install Node.js current LTS.", 4)
        environment = self.environ.copy()
        if output_file is not None:
            environment["WRANGLER_OUTPUT_FILE_PATH"] = str(output_file)
        if secrets:
            self.redactions.extend(secrets.values())
        try:
            result = _run_process(
                [self.executable, "--yes", f"wrangler@{self.version}", *args],
                cwd=self.cwd,
                environ=environment,
                input_text=json.dumps(dict(secrets)) if secrets else "",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployError(f"Wrangler {' '.join(args[:3])} timed out.") from exc
        except OSError as exc:
            raise DeployError("Cannot start the pinned Wrangler through npx.") from exc
        if result.returncode:
            detail = self.redact((result.stderr + "\n" + result.stdout).strip())
            raise DeployError(f"Wrangler {' '.join(args[:3])} failed:\n{detail}")
        return result.stdout


def labelled_records(text: str, fields: set[str]) -> list[dict[str, str]]:
    """Parse only the pinned CLI's labelled records, rejecting partial records."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([a-z_]+):\s*(.*?)\s*$", line)
        if match is None or match[1] not in fields:
            continue
        key, value = match[1], match[2]
        if key == "name" and current:
            if current.keys() != fields:
                raise DeployError("Malformed Wrangler labelled record.")
            records.append(current)
            current = {}
        if key in current:
            raise DeployError("Duplicate field in Wrangler labelled record.")
        current[key] = value
    if current:
        if current.keys() != fields:
            raise DeployError("Incomplete Wrangler labelled record.")
        records.append(current)
    return records


def bucket_names(text: str) -> set[str]:
    records = labelled_records(text, {"name", "creation_date"})
    if not records and "Listing buckets..." not in text:
        raise DeployError("Unrecognized Wrangler bucket list output.")
    return {record["name"] for record in records}


def lifecycle_rules(text: str) -> list[dict[str, str]]:
    records = labelled_records(text, {"name", "enabled", "prefix", "action"})
    if not records and "no lifecycle" not in text.lower():
        raise DeployError("Unrecognized Wrangler lifecycle list output.")
    return records


def secret_names(text: str) -> set[str]:
    value = json_value(text, "Wrangler secret list")
    if not isinstance(value, list):
        raise DeployError("Malformed Wrangler secret list: expected an array.")
    names: set[str] = set()
    for entry in value:
        record = object_value(entry, "secret record")
        name = record.get("name")
        if not isinstance(name, str) or record.get("type") != "secret_text":
            raise DeployError("Malformed Wrangler secret record.")
        names.add(name)
    return names


def worker_url(target: str, name: str) -> str | None:
    try:
        parsed = urlsplit(target if "://" in target else f"https://{target}")
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(re.escape(name) + r"\.[a-z0-9-]+\.workers\.dev", host)
    ):
        return None
    return f"https://{host}"


def deployment_url(output_file: Path, name: str, override: str | None) -> str:
    if override:
        url = worker_url(override, name)
        if url is None:
            raise DeployError("--url must be this Worker's HTTPS workers.dev URL.", 4)
        return url
    candidates: set[str] = set()
    if output_file.exists():
        for line in output_file.read_text(encoding="utf-8").splitlines():
            entry = object_value(json_value(line, "Wrangler output event"), "event")
            if entry.get("type") != "deploy":
                continue
            if entry.get("version") != 1 or entry.get("worker_name") != name:
                raise DeployError("Unexpected Wrangler deploy event.")
            for target in string_list(entry.get("targets"), "deploy targets"):
                url = worker_url(target, name)
                if url:
                    candidates.add(url)
    if len(candidates) != 1:
        raise DeployError(
            "Wrangler did not report one workers.dev URL; rerun with --url URL.", 4
        )
    return candidates.pop()
