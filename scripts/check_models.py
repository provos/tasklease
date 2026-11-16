"""Run the pinned TLC checker against the bounded lifecycle models.

TLC is supplied explicitly rather than downloaded during checks. All generated
state and trace files, including mutation controls, stay in a temporary directory.
"""

import argparse
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"


def check(jar: Path, module: Path, config: Path, output: Path) -> str:
    result = subprocess.run(
        [
            "java",
            "-XX:+UseParallelGC",
            "-Xmx1g",
            "-cp",
            str(jar),
            "tlc2.TLC",
            "-workers",
            "1",
            "-seed",
            "1",
            "-metadir",
            str(output),
            "-config",
            str(config),
            str(module),
        ],
        cwd=module.parent,
        capture_output=True,
        text=True,
        timeout=300,
    )
    log = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(log)
    for line in log.splitlines():
        if "states generated," in line or "depth of the complete" in line:
            print(f"{config.stem}: {line}", flush=True)
    return log


def mutation_checks(jar: Path, temporary: Path) -> None:
    source = (ROOT / "formal/TaskLease.tla").read_text()
    mutations = (
        (
            "old-id-survives-own",
            "Invariant Fencing is violated",
            "Fanout.cfg",
            r"tasks' = (tasks \ {t}) \cup",
            r"tasks' = tasks \cup",
        ),
        (
            "forged-successor-origin",
            "Invariant DescendantOrigins is violated",
            "TaskLease.cfg",
            "New(j, destinations[j], w, t.origin, FALSE)",
            "New(j, destinations[j], w, j, FALSE)",
        ),
        (
            "cancel-descendant",
            "Action property CancelPreservesDescendants is violated",
            "MultipleRoots.cfg",
            r"t.root /\ t.origin = origin",
            "t.origin = origin",
        ),
        (
            "claim-out-of-order",
            "Action property OwnSelectsOldest is violated",
            "MultipleRoots.cfg",
            r"/\ Oldest(t, topic)",
            r"/\ TRUE",
        ),
    )
    for name, violation, config, before, after in mutations:
        assert before in source, f"Mutation target disappeared: {name}"
        directory = temporary / name
        directory.mkdir()
        module = directory / "TaskLease.tla"
        module.write_text(source.replace(before, after, 1))
        config_path = directory / config
        config_path.write_text((ROOT / "formal" / config).read_text())
        try:
            check(jar, module, config_path, directory / "states")
        except RuntimeError as error:
            if violation not in str(error):
                raise
            print(f"Mutation rejected: {name}", flush=True)
        else:
            raise RuntimeError(f"Mutation was not detected: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, default=os.environ.get("TLA2TOOLS_JAR"))
    parser.add_argument(
        "--self-test", action="store_true", help="Reject known bad model mutations"
    )
    args = parser.parse_args()
    if args.jar is None:
        parser.error(f"Set TLA2TOOLS_JAR or --jar to TLC 1.7.4 from {TLC_URL}")
    jar = Path(args.jar).resolve()
    if hashlib.sha256(jar.read_bytes()).hexdigest() != TLC_SHA256:
        parser.error("TLC JAR does not match the pinned 1.7.4 SHA-256")
    with tempfile.TemporaryDirectory(prefix="tasklease-model-") as directory:
        temporary = Path(directory)
        for config in (
            "Retention.cfg",
            "TaskLease.cfg",
            "Fanout.cfg",
            "MultipleRoots.cfg",
        ):
            check(
                jar,
                ROOT / "formal/TaskLease.tla",
                ROOT / "formal" / config,
                temporary / config,
            )
        if args.self_test:
            mutation_checks(jar, temporary)


if __name__ == "__main__":
    main()
