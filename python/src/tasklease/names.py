"""Wire-format name rules (schema/common.json), shared by every module.

Each predicate only answers whether a value is valid; callers raise their own
error type, so configuration, store and protocol errors stay distinct.
"""

from __future__ import annotations

import re
from typing import TypeGuard

_PATH = re.compile(r"[a-z0-9._-]+(?:/[a-z0-9._-]+)*")
_SEGMENT = re.compile(r"[a-z0-9._-]{1,128}")
_IDENTITY = re.compile(r"[a-z0-9-]{1,64}")
_WORKER_LABEL = re.compile(r"[A-Za-z0-9._-]{1,256}")
_TASK_ID = re.compile(r"t_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_TOPIC_GLOB = re.compile(r"[a-z0-9.*_-]+(?:/[a-z0-9.*_-]+)*")
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


def is_identity(value: object) -> TypeGuard[str]:
    """An identity name, as the service stamps it into a task's ``creator``."""
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def is_worker_label(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _WORKER_LABEL.fullmatch(value) is not None


def is_task_id(value: object) -> TypeGuard[str]:
    """A fencing ID: ``t_`` followed by a canonical ULID."""
    return isinstance(value, str) and _TASK_ID.fullmatch(value) is not None


def is_topic_glob(value: object) -> TypeGuard[str]:
    """A spec §3 scope glob, with ``**`` only as a complete segment."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and _TOPIC_GLOB.fullmatch(value) is not None
        and all(part == "**" or "**" not in part for part in value.split("/"))
    )


def is_bearer_token(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and all(33 <= ord(character) <= 126 for character in value)
    )


def is_request_id(value: object) -> TypeGuard[str]:
    """Whether the requester store can correlate this UTF-8 identifier.

    IDs contain 1 to 128 Unicode characters. They are opaque identifiers,
    rather than domain/topic names or filenames.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _segment_matches(pattern: str, value: str) -> bool:
    position = index = 0
    star = -1
    restart = 0
    while index < len(value):
        if position < len(pattern) and pattern[position] == "*":
            star = position
            position += 1
            restart = index
        elif position < len(pattern) and pattern[position] == value[index]:
            position += 1
            index += 1
        elif star >= 0:
            position = star + 1
            restart += 1
            index = restart
        else:
            return False
    return all(character == "*" for character in pattern[position:])


def topic_matches(glob: str, topic: str) -> bool:
    """Match a whole topic using the Worker's spec §3 scope glob rule.

    ``*`` matches within one segment; a whole ``**`` segment consumes one or
    more topic segments. Empty segments and embedded ``**`` never match.
    Name/length validation is separate from matching, as in the Worker.
    """
    segments = topic.split("/")
    if not all(segments):
        return False
    previous = [True] + [False] * len(segments)
    for pattern in glob.split("/"):
        if not pattern or (pattern != "**" and "**" in pattern):
            return False
        current = [False] * (len(segments) + 1)
        for index, segment in enumerate(segments, 1):
            current[index] = (
                previous[index - 1] or current[index - 1]
                if pattern == "**"
                else previous[index - 1] and _segment_matches(pattern, segment)
            )
        previous = current
    return previous[-1]


def error_topic(topic: str) -> str:
    """Spec §2: a topic already ending in `/error` is its own error topic."""
    return topic if topic.endswith(_ERROR) else topic + _ERROR
