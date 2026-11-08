"""Wire-format name rules (schema/common.json), shared by every module.

Each predicate only answers whether a value is valid; callers raise their own
error type, so configuration, store and protocol errors stay distinct.
"""

from __future__ import annotations

import re
from typing import TypeGuard

_PATH = re.compile(r"[a-z0-9._-]+(?:/[a-z0-9._-]+)*")
_SEGMENT = re.compile(r"[a-z0-9._-]{1,128}")
_WORKER_LABEL = re.compile(r"[A-Za-z0-9._-]{1,256}")
_ERROR = "/error"


def is_domain(value: object) -> TypeGuard[str]:
    """Spec §2: path segments, at most 256 characters, never `.` or `..`."""
    return (
        isinstance(value, str)
        and len(value) <= 256
        and value not in {".", ".."}
        and _PATH.fullmatch(value) is not None
    )


def is_topic(value: object) -> TypeGuard[str]:
    """Spec §2: at most 128 characters, not counting a final `/error`."""
    return (
        isinstance(value, str)
        and _PATH.fullmatch(value) is not None
        and (
            len(value) <= 128
            or (value.endswith(_ERROR) and len(value) <= 128 + len(_ERROR))
        )
    )


def is_segment(value: object) -> TypeGuard[str]:
    """One topic segment, as used for agent IDs."""
    return isinstance(value, str) and _SEGMENT.fullmatch(value) is not None


def is_worker_label(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _WORKER_LABEL.fullmatch(value) is not None


def is_bearer_token(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and all(33 <= ord(character) <= 126 for character in value)
    )


def error_topic(topic: str) -> str:
    """Spec §2: a topic already ending in `/error` is its own error topic."""
    return topic if topic.endswith(_ERROR) else topic + _ERROR
