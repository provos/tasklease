"""Run the pinned TLC checker against the bounded lifecycle models.

TLC is supplied explicitly rather than downloaded during checks. All generated
state and trace files, including mutation controls, stay in a temporary directory.
"""

import argparse
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"


def check(jar: Path, module: Path, config: Path, output: Path) -> str:
    try:
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
    except OSError as error:
        raise RuntimeError(f"Cannot start TLC using java: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"TLC exceeded {error.timeout} seconds while checking {config.name}"
        ) from error
    log = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(log)
    for line in log.splitlines():
        if "states generated," in line or "depth of the complete" in line:
            print(f"{config.stem}: {line}", flush=True)
    return log


def replace_in_action(source: str, action: str, before: str, after: str) -> str:
    """Replace one anchor inside a named top-level TLA+ definition, or fail."""
    definitions = list(
        re.finditer(r"(?m)^([A-Za-z][A-Za-z0-9_]*)(?:\([^\n]*\))? ==", source)
    )
    matches = [i for i, definition in enumerate(definitions) if definition[1] == action]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {action} definition, found {len(matches)}")
    index = matches[0]
    start = definitions[index].start()
    end = (
        definitions[index + 1].start() if index + 1 < len(definitions) else len(source)
    )
    definition = source[start:end]
    count = definition.count(before)
    if count != 1:
        raise RuntimeError(f"Expected one mutation anchor in {action}, found {count}")
    return source[:start] + definition.replace(before, after) + source[end:]


def mutation_checks(jar: Path, temporary: Path) -> None:
    source = (ROOT / "formal/TaskLease.tla").read_text()
    mutations = (
        (
            "old-id-survives-own",
            "Own",
            "Invariant Fencing is violated",
            "Fanout.cfg",
            r"tasks' = (tasks \ {t}) \cup",
            r"tasks' = tasks \cup",
        ),
        (
            "forged-successor-origin",
            "Consume",
            "Invariant DescendantOrigins is violated",
            "TaskLease.cfg",
            "New(j, destinations[j], w, t.origin, FALSE)",
            "New(j, destinations[j], w, j, FALSE)",
        ),
        (
            "cancel-descendant",
            "Cancel",
            "Action property CancelPreservesDescendants is violated",
            "MultipleRoots.cfg",
            r"t.root /\ t.origin = origin",
            "t.origin = origin",
        ),
        (
            "claim-out-of-order",
            "Own",
            "Action property OwnSelectsOldest is violated",
            "MultipleRoots.cfg",
            r"/\ t \in Pick(selected)",
            r"/\ t \in Eligible(selected)",
        ),
        (
            "claim-oldest-only-within-one-topic",
            "Own",
            "Action property OwnSelectsOldest is violated",
            "MultiTopic.cfg",
            r"/\ t \in Pick(selected)",
            r"/\ t.topic \in selected /\ t \in Pick({t.topic})",
        ),
        (
            "require-work-on-every-topic",
            "Own",
            "Invariant OwnCanClaim is violated",
            "MultiTopic.cfg",
            r"/\ t \in Pick(selected)",
            r"/\ t \in Pick(selected) /\ "
            r"\A topic \in selected : Eligible({topic}) # {}",
        ),
    )
    for name, action, violation, config, before, after in mutations:
        directory = temporary / name
        directory.mkdir()
        module = directory / "TaskLease.tla"
        module.write_text(replace_in_action(source, action, before, after))
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
    try:
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    except OSError as error:
        parser.error(f"Cannot read TLC JAR {jar}: {error.strerror}")
    if digest != TLC_SHA256:
        parser.error("TLC JAR does not match the pinned 1.7.4 SHA-256")
    try:
        with tempfile.TemporaryDirectory(prefix="tasklease-model-") as directory:
            temporary = Path(directory)
            for config in (
                "Retention.cfg",
                "TaskLease.cfg",
                "Fanout.cfg",
                "MultipleRoots.cfg",
                "MultiTopic.cfg",
            ):
                check(
                    jar,
                    ROOT / "formal/TaskLease.tla",
                    ROOT / "formal" / config,
                    temporary / config,
                )
            if args.self_test:
                mutation_checks(jar, temporary)
    except RuntimeError as error:
        parser.exit(1, f"{parser.prog}: error: {error}\n")


if __name__ == "__main__":
    main()
