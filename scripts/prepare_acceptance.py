"""Export reviewed source and its matching wheel without local credentials."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from acceptance_bundle import (
    MANIFEST_NAME,
    Manifest,
    digest,
    read_source,
    safe_name,
    tree_digest,
)

_ASSET_DIRS = (
    "python/src/tasklease/monitor/static",
    "python/src/tasklease/service_bundle",
)


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], stderr=subprocess.PIPE
    )


def matching_wheel(wheel: Path, source: dict[str, bytes], version: str) -> bytes:
    """Require exact source/resource parity, not just the same package version."""
    wheel_bytes = wheel.read_bytes()
    expected = {
        name.removeprefix("python/src/"): content
        for name, content in source.items()
        if name.startswith("python/src/tasklease/")
    }
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("The wheel contains duplicate members")
        members = {name for name in names if not name.endswith("/")}
        for name in members:
            safe_name(name)
            if not name.startswith(("tasklease/", f"tasklease-{version}.dist-info/")):
                raise ValueError(f"Unexpected wheel member: {name}")
        package = {name for name in members if name.startswith("tasklease/")}
        if package != set(expected):
            raise ValueError(
                "Wheel package members differ from the selected source; rebuild it"
            )
        for name in sorted(package):
            if archive.read(name) != expected[name]:
                raise ValueError(
                    f"Wheel differs from the selected source: {name}; rebuild it"
                )
        metadata = BytesParser().parsebytes(
            archive.read(f"tasklease-{version}.dist-info/METADATA")
        )
        if metadata.get("Name") != "tasklease" or metadata.get("Version") != version:
            raise ValueError("Wheel metadata differs from the source project")
    return wheel_bytes


def prepare(
    root: Path, wheel: Path, output: Path, *, allow_dirty: bool = False
) -> dict[str, object]:
    root, wheel, output = root.resolve(), wheel.resolve(), output.resolve()
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    if status and not allow_dirty:
        raise ValueError(
            "Acceptance export requires a clean checkout; "
            "commit first or use --allow-dirty"
        )
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    names = set(git(root, "ls-files", "--cached", "-z").decode().split("\0")) - {""}
    for directory in _ASSET_DIRS:
        path = root / directory
        if not path.is_dir() or path.is_symlink():
            raise ValueError(
                "Prebuilt acceptance assets are missing; run make build first"
            )
        names.update(
            file.relative_to(root).as_posix()
            for file in path.rglob("*")
            if file.is_file()
        )
    source = {name: read_source(root, name) for name in sorted(names)}
    project = tomllib.loads(source["python/pyproject.toml"].decode())
    version: object = project["project"]["version"]
    if not isinstance(version, str):
        raise ValueError("The project version must be text")
    artifact_name = safe_name("artifacts/" + wheel.name)
    if not artifact_name.endswith(".whl"):
        raise ValueError("Supply a built tasklease wheel")
    payload = {"source/" + name: content for name, content in source.items()}
    payload[artifact_name] = matching_wheel(wheel, source, version)
    if len({name.casefold() for name in payload}) != len(payload):
        raise ValueError("Acceptance paths collide on a case-insensitive filesystem")
    hashes = {name: digest(content) for name, content in payload.items()}
    manifest: Manifest = {
        "format": 1,
        "source_commit": commit,
        "source_dirty": bool(status),
        "source_tree_sha256": tree_digest(hashes),
        "package_version": version,
        "wheel": artifact_name,
        "files": hashes,
    }
    if (
        git(root, "rev-parse", "HEAD").decode().strip() != commit
        or git(root, "status", "--porcelain", "--untracked-files=all") != status
    ):
        raise ValueError(
            "Checkout changed during acceptance export; try again when it is stable"
        )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tasklease-acceptance-", suffix=".zip", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, content in sorted(payload.items()):
                archive.writestr(name, content)
            archive.writestr(
                MANIFEST_NAME, json.dumps(manifest, sort_keys=True, indent=2) + "\n"
            )
        archive_hash = digest(temporary.read_bytes())
        os.replace(temporary, output)
        output.with_suffix(output.suffix + ".sha256").write_text(
            archive_hash + "  " + output.name + "\n", encoding="ascii"
        )
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "archive": str(output),
        "sha256": archive_hash,
        "source_commit": commit,
        "source_dirty": bool(status),
        "files": len(hashes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Include tracked/staged candidate bytes and record dirty status",
    )
    args = parser.parse_args()
    try:
        result = prepare(
            args.root, args.wheel, args.output, allow_dirty=args.allow_dirty
        )
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(1, f"Acceptance export failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
