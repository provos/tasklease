"""Shared, stdlib-only integrity checks for portable acceptance bundles."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

MANIFEST_NAME = "acceptance-manifest.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DEVICES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{index}"
    for prefix in ("com", "lpt")
    for index in (*range(1, 10), "¹", "²", "³")
}
_BLOCKED_PARTS = {
    ".git",
    ".aws",
    ".ssh",
    ".azure",
    ".wrangler",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}


class Manifest(TypedDict):
    format: int
    source_commit: str
    source_dirty: bool
    source_tree_sha256: str
    package_version: str
    wheel: str
    files: dict[str, str]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> str:
    """Reject nonportable paths and paths that can contain ambient credentials."""
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {".", ".."} for part in path.parts)
        or any(ord(character) < 32 or character in '<>"|?*' for character in name)
    ):
        raise ValueError("Acceptance paths must be portable relative file paths")
    for part in path.parts:
        lower = part.casefold()
        if (
            lower in _BLOCKED_PARTS
            or lower.startswith((".env", ".dev.vars", ".venv"))
            or lower in {"token", "credentials"}
            or lower.endswith((".pyc", ".pyo"))
            or part.endswith((".", " "))
            or lower.split(".", 1)[0] in _DEVICES
        ):
            raise ValueError(f"Refusing private or cached acceptance path: {name}")
    return name


def read_source(root: Path, name: str) -> bytes:
    safe_name(name)
    current = root
    for part in PurePosixPath(name).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Acceptance source must not contain symlinks: {name}")
    if not current.resolve().is_relative_to(root.resolve()) or not current.is_file():
        raise ValueError(f"Acceptance source is missing or outside its root: {name}")
    return current.read_bytes()


def tree_digest(files: dict[str, str]) -> str:
    source = {
        name: value for name, value in files.items() if name.startswith("source/")
    }
    return digest(json.dumps(source, sort_keys=True, separators=(",", ":")).encode())


def parse_manifest(contents: bytes) -> Manifest:
    value: object = json.loads(contents)
    if not isinstance(value, dict):
        raise ValueError("Invalid acceptance manifest")
    data = cast(dict[str, object], value)
    required = {
        "format",
        "source_commit",
        "source_dirty",
        "source_tree_sha256",
        "package_version",
        "wheel",
        "files",
    }
    if (
        set(data) != required
        or type(data.get("format")) is not int
        or data.get("format") != 1
        or not isinstance(data.get("source_commit"), str)
        or type(data.get("source_dirty")) is not bool
        or not isinstance(data.get("package_version"), str)
        or not isinstance(data.get("wheel"), str)
        or not isinstance(data.get("source_tree_sha256"), str)
        or not isinstance(data.get("files"), dict)
    ):
        raise ValueError("Invalid acceptance manifest fields")
    if not _COMMIT.fullmatch(cast(str, data["source_commit"])):
        raise ValueError("Invalid acceptance source commit")
    entries = cast(dict[object, object], data["files"])
    files: dict[str, str] = {}
    portable_names: set[str] = set()
    for name, value in entries.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not _DIGEST.fullmatch(value)
        ):
            raise ValueError("Invalid acceptance file digest")
        safe_name(name)
        if name.casefold() in portable_names:
            raise ValueError(
                "Acceptance paths collide on a case-insensitive filesystem"
            )
        portable_names.add(name.casefold())
        if not name.startswith(("source/", "artifacts/")):
            raise ValueError("Unexpected acceptance file root")
        files[name] = value
    wheel = cast(str, data["wheel"])
    if (
        not wheel.startswith("artifacts/")
        or not wheel.endswith(".whl")
        or wheel not in files
    ):
        raise ValueError("Acceptance wheel is missing from the manifest")
    if tree_digest(files) != data["source_tree_sha256"]:
        raise ValueError("Acceptance source-tree digest does not match")
    return cast(Manifest, data)


def verify_manifest(root: Path) -> Manifest:
    manifest = parse_manifest(read_source(root, MANIFEST_NAME))
    verify_files(root, manifest)
    return manifest


def verify_files(root: Path, manifest: Manifest) -> None:
    for name, expected in manifest["files"].items():
        if digest(read_source(root, name)) != expected:
            raise ValueError(f"Acceptance file digest does not match: {name}")


def verify_archive(
    root: Path, contents: bytes, *, expected_sha256: str | None = None
) -> Manifest:
    """Bind extracted bytes to the manifest in this exact verified ZIP."""
    if expected_sha256 is not None and digest(contents) != expected_sha256.lower():
        raise ValueError("Acceptance ZIP hash differs from the supplied expected hash")
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            infos = archive.infolist()
            names = [entry.filename for entry in infos]
            if len(names) != len(set(names)):
                raise ValueError("The acceptance ZIP contains duplicate members")
            for entry in infos:
                safe_name(entry.filename)
                if entry.is_dir() or stat.S_IFMT(entry.external_attr >> 16) not in {
                    0,
                    stat.S_IFREG,
                }:
                    raise ValueError("Acceptance ZIP members must be regular files")
            manifest_bytes = archive.read(MANIFEST_NAME)
            if manifest_bytes != read_source(root, MANIFEST_NAME):
                raise ValueError("Extracted manifest differs from the verified ZIP")
            manifest = parse_manifest(manifest_bytes)
            if set(names) != set(manifest["files"]) | {MANIFEST_NAME}:
                raise ValueError("Acceptance ZIP members differ from its manifest")
            for name, expected in manifest["files"].items():
                if digest(archive.read(name)) != expected:
                    raise ValueError(
                        f"Acceptance ZIP member digest does not match: {name}"
                    )
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("Invalid acceptance ZIP") from error
    verify_files(root, manifest)
    return manifest
