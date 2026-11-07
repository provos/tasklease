"""Checks for the installed package and command entry points."""

import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

from tasklease import __version__


def test_module_entrypoint_outside_checkout(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tasklease", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"tl {__version__}"
    assert not result.stderr


def test_installed_console_script(tmp_path: Path) -> None:
    scripts = distribution("tasklease").entry_points.select(
        group="console_scripts", name="tl"
    )
    assert len(scripts) == 1
    executable = Path(sys.executable).with_name(
        "tl.exe" if sys.platform == "win32" else "tl"
    )
    result = subprocess.run(
        [str(executable), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "A leased task board" in result.stdout
    assert not result.stderr
