"""Export redacted reports/logs for publication; keep original evidence private."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from acceptance_bundle import digest

MANIFEST = "public-evidence-manifest.json"
_TEXT_SUFFIXES = {
    ".json",
    ".jsonl",
    ".log",
    ".txt",
    ".md",
    ".xml",
    ".py",
    ".sh",
    ".ps1",
}
_FIELDS = {
    "account_id": "[ACCOUNT]",
    "r2_account_id": "[ACCOUNT]",
    "bucket": "[BUCKET]",
    "bucket_name": "[BUCKET]",
    "r2_bucket_name": "[BUCKET]",
    "worker": "[SERVICE]",
    "worker_name": "[SERVICE]",
    "service_name": "[SERVICE]",
    "target": "[SERVICE]",
    "fluxshot_service": "[SERVICE]",
    "hostname": "[HOST]",
    "host": "[HOST]",
    "normal_host": "[HOST]",
    "admin_token": "[CREDENTIAL]",
    "access_token": "[CREDENTIAL]",
    "refresh_token": "[CREDENTIAL]",
    "token": "[CREDENTIAL]",
    "r2_access_key_id": "[CREDENTIAL]",
    "r2_secret_access_key": "[CREDENTIAL]",
}
_WORKER_URL = re.compile(r"https?://([a-zA-Z0-9-]+)\.[a-zA-Z0-9.-]+\.workers\.dev")
_HOME = re.compile(
    r"/(?:Users|home)/[^/\n\r\"'<>]+|/mnt/c/Users/[^/\n\r\"'<>]+"
    r"|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\n\r\"'<>]+"
)
_TEMP = re.compile(
    r"/(?:private/)?var/folders/[^/\s]+/[^/\s]+/[TC]/[^/\s\"'<>`\])]+"
    r"|/(?:private/)?tmp/[^/\s\"'<>`\])]+"
    r"|\[HOME\][\\/]+AppData[\\/]+Local[\\/]+Temp[\\/]+[^\\/\s\"'<>`\])]+",
    re.IGNORECASE,
)
_HOST = re.compile(r"\b(?:[\w.-]+@)?[a-zA-Z0-9-]+\.(?:localdomain|local)\b")
_PRIVATE_IP = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>\b(?P<field>"
    + "|".join(_FIELDS)
    + r")[\"']?\s*[:=]\s*)(?P<quote>[\"'])(?P<value>[^\"'\r\n]+)(?P=quote)"
)
_RESULT_VALUES = {
    "passed",
    "failed",
    "skipped",
    "running",
    "success",
    "failure",
    "pending",
    "complete",
    "completed",
    "interrupted",
    "ok",
    "true",
    "false",
}
_RESULT_KEYS = {
    "status",
    "state",
    "passed",
    "failed",
    "skipped",
    "tests",
    "failures",
    "errors",
    "version",
    "package_version",
    "source_commit",
    "commit",
    "sha256",
}
_MAX_EXPANDED_BYTES = 64 * 1024 * 1024


class Redactor:
    """Discover structured identifiers before redacting every related log."""

    def __init__(self) -> None:
        self.replacements: dict[str, str] = {}

    def discover(self, text: str) -> None:
        for line in [text, *text.splitlines()]:
            try:
                value: object = json.loads(line)
            except ValueError:
                continue
            self._discover(value)
        for match in _WORKER_URL.finditer(text):
            self.replacements[match[1]] = "[SERVICE]"
        for match in _ASSIGNMENT.finditer(text):
            self.replacements[match["value"]] = _FIELDS[match["field"].casefold()]

    def _discover(self, value: object) -> None:
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            worker = bool(_WORKER_URL.search(str(record.get("url", ""))))
            for key, item in record.items():
                replacement = _FIELDS.get(key.casefold())
                if key == "name" and worker:
                    replacement = "[SERVICE]"
                if replacement and isinstance(item, str) and item:
                    self.replacements[item] = replacement
                self._discover(item)
        elif isinstance(value, list):
            for item in cast(list[object], value):
                self._discover(item)

    def text(self, text: str) -> str:
        text = _HOME.sub("[HOME]", text)
        text = _TEMP.sub("[TEMP]", text)
        text = _ASSIGNMENT.sub(
            lambda match: (
                match["prefix"]
                + match["quote"]
                + _FIELDS[match["field"].casefold()]
                + match["quote"]
            ),
            text,
        )
        text = _WORKER_URL.sub("https://service.example.invalid", text)
        for value in sorted(self.replacements, key=len, reverse=True):
            if value.casefold() in _RESULT_VALUES:
                continue
            text = re.sub(
                r"(?<![\w-])" + re.escape(value) + r"(?![\w-])",
                self.replacements[value],
                text,
            )
        text = _HOST.sub("[HOST]", text)
        return _PRIVATE_IP.sub("[PRIVATE_IP]", text)

    def value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            worker = bool(_WORKER_URL.search(str(record.get("url", ""))))
            result: dict[str, object] = {}
            for key, item in record.items():
                public_key = (
                    key if key in _RESULT_KEYS or key in _FIELDS else self.text(key)
                )
                if public_key in result:
                    raise ValueError("Redaction would merge report map keys")
                replacement = _FIELDS.get(key.casefold())
                if key == "name" and worker:
                    replacement = "[SERVICE]"
                result[public_key] = (
                    replacement
                    if replacement and isinstance(item, str)
                    else self.value(item)
                )
            return result
        if isinstance(value, list):
            return [self.value(item) for item in cast(list[object], value)]
        return value

    def document(self, text: str) -> str:
        try:
            value: object = json.loads(text)
        except ValueError:
            return self.text(text)
        return json.dumps(self.value(value), ensure_ascii=True, indent=2) + "\n"


def decode(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for offset, encoding in ((1, "utf-16-le"), (0, "utf-16-be")):
        if data and data[offset::2].count(0) > len(data) // 4:
            return data.decode(encoding), encoding
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("Evidence must use UTF-8 or UTF-16 text")
    return text, "utf-8"


@dataclass
class ArchiveBudget:
    expanded: int = 0


def archive_files(
    data: bytes, budget: ArchiveBudget, *, depth: int = 0
) -> dict[str, bytes]:
    if depth > 4:
        raise ValueError("Evidence archives are nested too deeply")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate evidence archive members")
        if any(
            PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or "\\" in name
            or ":" in name
            for name in names
        ):
            raise ValueError("Evidence members must use relative portable paths")
        budget.expanded += sum(info.file_size for info in archive.infolist())
        if budget.expanded > _MAX_EXPANDED_BYTES:
            raise ValueError("Evidence archive is too large")
        return {name: archive.read(name) for name in names if not name.endswith("/")}


def collect(
    name: str, data: bytes, redactor: Redactor, budget: ArchiveBudget, *, depth: int = 0
) -> None:
    suffix = Path(name).suffix.lower()
    if suffix == ".zip":
        for member, content in archive_files(data, budget, depth=depth).items():
            collect(member, content, redactor, budget, depth=depth + 1)
    elif suffix in _TEXT_SUFFIXES:
        redactor.discover(decode(data)[0])
    else:
        raise ValueError("Public evidence may contain only reports/logs and ZIPs")


def redact(
    name: str, data: bytes, redactor: Redactor, budget: ArchiveBudget, *, depth: int = 0
) -> bytes:
    if Path(name).suffix.lower() != ".zip":
        text, encoding = decode(data)
        return redactor.document(text).encode(encoding)
    files = {
        name: redact(name, content, redactor, budget, depth=depth + 1)
        for name, content in archive_files(data, budget, depth=depth).items()
        if name != MANIFEST
    }
    if any(redactor.text(name) != name for name in files):
        raise ValueError("Evidence member names contain private metadata; rename them")
    manifest = {
        "format": 1,
        "metadata_redacted": True,
        "source_sha256": digest(data),
        "original_digests": (
            "Existing reports describe original private evidence, not redacted copies"
        ),
        "files": {name: digest(content) for name, content in files.items()},
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2) + "\n")
    return output.getvalue()


def export(source: Path, output: Path) -> str:
    if source.resolve() == output.resolve():
        raise ValueError("Keep original evidence separate from its public copy")
    data = source.read_bytes()
    redactor = Redactor()
    collect(source.name, data, redactor, ArchiveBudget())
    result = redact(source.name, data, redactor, ArchiveBudget())
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".public-evidence-", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(result)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return digest(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        checksum = export(args.input, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Public evidence export failed: {error}\n")
    print(json.dumps({"sha256": checksum, "metadata_redacted": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
