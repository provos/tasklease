"""The shared wire-format name rules (spec §2)."""

from __future__ import annotations

import pytest

from tasklease.names import (
    error_topic,
    is_bearer_token,
    is_domain,
    is_idempotency_key,
    is_segment,
    is_task_id,
    is_topic,
    is_topic_glob,
    is_worker_label,
    new_idempotency_key,
    topic_matches,
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "017f22e2-79b0-4cc3-98c4-dc0c0c07398f",
        "017f22e2-79b0-7cc3-78c4-dc0c0c07398f",
        "017F22E2-79B0-7CC3-98C4-DC0C0C07398F",
        "017f22e279b07cc398c4dc0c0c07398f",
        "017f22e2-79b0-7cc3-98c4-dc0c0c07398f\n",
    ],
)
def test_idempotency_key_rejects_noncanonical_values(value: object) -> None:
    assert not is_idempotency_key(value)


def test_idempotency_key_generator_matches_rfc9562_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = int("017f22e279b0", 16)
    monkeypatch.setattr("tasklease.names.time.time_ns", lambda: timestamp * 1_000_000)
    random_fields = iter([0xCC3, 0x18C4DC0C0C07398F])
    monkeypatch.setattr(
        "tasklease.names.secrets.randbits", lambda bits: next(random_fields)
    )
    value = new_idempotency_key()
    assert value == "017f22e2-79b0-7cc3-98c4-dc0c0c07398f"
    assert is_idempotency_key(value)


def test_idempotency_keys_sort_by_millisecond_and_remain_unique_within_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tasklease.names.time.time_ns", lambda: 1_800_000_000_000_000_000
    )
    values = {new_idempotency_key() for _ in range(100)}
    assert len(values) == 100 and all(is_idempotency_key(value) for value in values)
    monkeypatch.setattr(
        "tasklease.names.time.time_ns", lambda: 1_800_000_000_001_000_000
    )
    assert new_idempotency_key() > max(values)


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


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("t_00000000000000000000000000", True),
        ("t_7ZZZZZZZZZZZZZZZZZZZZZZZZZ", True),
        ("t_8ZZZZZZZZZZZZZZZZZZZZZZZZZ", False),
        ("t_0000000000000000000000000I", False),
        ("t_0000000000000000000000000a", False),
        ("t_00000000000000000000000000\n", False),
        ("00000000000000000000000000", False),
        ("t_0000000000000000000000000", False),
        (None, False),
    ],
)
def test_task_id(value: object, valid: bool) -> None:
    assert is_task_id(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("verify/**", True),
        ("mac-*", True),
        ("**/**", True),
        ("a" * 256, True),
        ("a" * 257, False),
        ("a**", False),
        ("***", False),
        ("verify//linux", False),
        ("verify/", False),
        ("UPPER", False),
        ("?", False),
        ("[a-z]", False),
        ("work\n", False),
        ("", False),
        (None, False),
    ],
)
def test_topic_glob(value: object, valid: bool) -> None:
    assert is_topic_glob(value) is valid


@pytest.mark.parametrize(
    ("glob", "topic", "expected"),
    [
        ("verify/linux", "verify/linux", True),
        ("verify/linux", "verify/linux/error", False),
        ("verify/*", "verify/linux", True),
        ("verify/*", "verify", False),
        ("verify/*", "verify/linux/error", False),
        ("verify/**", "verify", False),
        ("verify/**", "verify/linux", True),
        ("verify/**", "verify/linux/error", True),
        ("**", "verify/linux/error", True),
        ("*", "verify/linux", False),
        ("mac-*", "mac-", True),
        ("mac-*", "mac", False),
        ("a*b*c", "a-one-b-two-c", True),
        ("a*b*c", "a-one-b-two-d", False),
        ("a*b*c", "ac", False),
        ("a.b", "axb", False),
        ("a.b", "a.b", True),
        ("**/error", "error", False),
        ("**/error", "work/linux/error", True),
        ("**/work/**", "a/work/b", True),
        ("**/work/**", "work/b", False),
        ("**/work/**", "a/work", False),
        ("a/**/b/**/c", "a/x/b/y/c", True),
        ("a/**/b/**/c", "a/b/y/c", False),
        ("**/**", "one", False),
        ("**/**", "one/two", True),
        ("work/**/error", "work/error", False),
        ("work/**/error", "work/linux/mac/error", True),
        ("work", "other/work", False),
        ("work", "work/other", False),
        ("*", "", False),
        ("**", "a//b", False),
        ("a**", "abc", False),
        ("***", "abc", False),
        ("", "abc", False),
    ],
)
def test_scope_matching(glob: str, topic: str, expected: bool) -> None:
    assert topic_matches(glob, topic) is expected


def test_scope_matching_many_wildcard_segments() -> None:
    glob = "/".join(["**"] * 100)
    assert topic_matches(glob, "/".join(["a"] * 100))
    assert not topic_matches(glob, "/".join(["a"] * 99))
