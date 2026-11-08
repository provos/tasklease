"""Exercise the native Task Scheduler parser without registering a task."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PARSE_TASK = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$path = $env:TASKLEASE_TEMPLATE_FILE
if ($env:TASKLEASE_TEMPLATE_MODE -eq 'utf8_file') {
    $document = New-Object System.Xml.XmlDocument
    $document.Load($path)
    $xml = $document.OuterXml
} else {
    $xml = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
}
$account = [Security.SecurityElement]::Escape(
    [Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$definition = $service.NewTask(0)
$definition.XmlText = $xml.Replace('USER_ID', $account)
if ($definition.Actions.Count -ne 1) { throw 'Expected one worker action' }
if ($definition.Actions.Item(1).WorkingDirectory -ne 'C:\tasklease-épreuve') {
    throw 'UTF-8 file and Unicode string must preserve the configured path'
}
[pscustomobject]@{
    parsed = $true
    actions = $definition.Actions.Count
    unicode_path = $true
    task_registered = $false
} | ConvertTo-Json -Compress
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Task Scheduler COM")
@pytest.mark.parametrize("mode", ["unicode_string", "utf8_file"])
def test_native_scheduler_parses_exported_template(tmp_path: Path, mode: str) -> None:
    system_root = os.environ.get("SystemRoot")
    if system_root is None:
        pytest.skip("Native Task Scheduler check requires Windows PowerShell")
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        pytest.skip("Native Task Scheduler check requires Windows PowerShell")
    exported = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "tasklease",
            "worker",
            "template",
            "--platform",
            "windows",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
    )
    # Exercise UTF-8 file decoding and Unicode COM input with non-ASCII data.
    task = tmp_path / "worker.xml"
    task.write_text(
        exported.stdout.replace(
            r"C:\absolute\path\to\checkout", r"C:\tasklease-épreuve"
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["TASKLEASE_TEMPLATE_FILE"] = str(task)
    environment["TASKLEASE_TEMPLATE_MODE"] = mode
    encoded_command = base64.b64encode(_PARSE_TASK.encode("utf-16-le")).decode("ascii")
    parsed = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded_command,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
    assert json.loads(parsed.stdout) == {
        "parsed": True,
        "actions": 1,
        "unicode_path": True,
        "task_registered": False,
    }
