"""The CLI can render every saved JSON string to a strict UTF-8 terminal."""

import io
import json
import sys

import pytest

from tasklease.cli.common import output


def test_output_preserves_unusual_json_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    terminal = io.TextIOWrapper(
        buffer, encoding="utf-8", errors="strict", write_through=True
    )
    monkeypatch.setattr(sys, "stdout", terminal)
    value = {"unpaired": "\ud800", "ordinary": "café 😃"}
    output(value)
    encoded = buffer.getvalue()
    assert encoded.isascii()
    assert json.loads(encoded) == value
