"""The shared wire-format name rules (spec §2)."""

from __future__ import annotations

import pytest

from tasklease.names import (
    error_topic,
    is_bearer_token,
    is_domain,
    is_segment,
    is_topic,
    is_worker_label,
)


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("github.com/provos/fluxshoteditor", True),
        ("a" * 256, True),
        ("a" * 257, False),
        (".", False),
        ("..", False),
        ("a/./b", True),
        ("Upper", False),
        ("a//b", False),
        ("", False),
        (None, False),
    ],
)
def test_domain(value: object, valid: bool) -> None:
    assert is_domain(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("verify/linux", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("x" * 128 + "/error", True),
        ("x" * 129 + "/error", False),
        ("x" * 122 + "/error/error", True),
        ("x" * 123 + "/error/error", False),
        ("verify/linux/", False),
        (3, False),
    ],
)
def test_topic(value: object, valid: bool) -> None:
    assert is_topic(value) is valid


def test_segment_and_labels() -> None:
    assert is_segment("mac-a1b2")
    assert not is_segment("mac/a1b2")
    assert not is_segment("x" * 129)
    assert is_worker_label("Verify-A.1")
    assert not is_worker_label("verify a")
    assert not is_worker_label("x" * 257)


def test_bearer_token() -> None:
    assert is_bearer_token("tl_abc-DEF")
    assert not is_bearer_token("")
    assert not is_bearer_token("has space")
    assert not is_bearer_token("café")


def test_error_topic_never_nests() -> None:
    assert error_topic("verify/linux") == "verify/linux/error"
    assert error_topic("verify/linux/error") == "verify/linux/error"
