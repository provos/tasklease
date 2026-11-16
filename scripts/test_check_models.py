"""Regression checks for mutation targeting and model-runner diagnostics."""

import errno
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import check_models
import pytest


def test_mutation_targets_named_action_after_reordering() -> None:
    source = (
        "Renew(i) ==\n  shared anchor\n\nOwn(i) ==\n  shared anchor\n\nNext == TRUE\n"
    )
    assert check_models.replace_in_action(source, "Own", "shared anchor", "mutant") == (
        "Renew(i) ==\n  shared anchor\n\nOwn(i) ==\n  mutant\n\nNext == TRUE\n"
    )


@pytest.mark.parametrize(
    "source, message",
    [
        ("Next == TRUE\n", "Expected one Own definition, found 0"),
        (
            "Own(i) ==\n  anchor\nOwn(j) ==\n  anchor\n",
            "Expected one Own definition, found 2",
        ),
        ("Own(i) ==\n  absent\n", "Expected one mutation anchor in Own, found 0"),
        (
            "Own(i) ==\n  anchor anchor\n",
            "Expected one mutation anchor in Own, found 2",
        ),
    ],
)
def test_mutation_rejects_missing_or_ambiguous_targets(
    source: str, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        check_models.replace_in_action(source, "Own", "anchor", "mutant")


def test_missing_jar_has_a_clean_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["check_models.py", "--jar", str(tmp_path / "missing.jar")]
    )
    with pytest.raises(SystemExit) as error:
        check_models.main()
    assert error.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "Cannot read TLC JAR" in diagnostic
    assert "Traceback" not in diagnostic


@pytest.mark.parametrize("failure", ["java", "timeout"])
def test_runner_environment_errors_have_clean_diagnostics(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jar = tmp_path / "test.jar"
    jar.write_bytes(b"test jar")
    monkeypatch.setattr(
        check_models, "TLC_SHA256", hashlib.sha256(jar.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(sys, "argv", ["check_models.py", "--jar", str(jar)])

    def fail_run(*_args: object, **_kwargs: object) -> NoReturn:
        if failure == "java":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", "java")
        raise subprocess.TimeoutExpired("java", 300)

    monkeypatch.setattr(subprocess, "run", fail_run)
    with pytest.raises(SystemExit) as error:
        check_models.main()
    assert error.value.code == 1
    diagnostic = capsys.readouterr().err
    assert (
        "Cannot start TLC using java"
        if failure == "java"
        else "TLC exceeded 300 seconds while checking Retention.cfg"
    ) in diagnostic
    assert "Traceback" not in diagnostic
