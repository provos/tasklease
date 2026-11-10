"""Run the same contract checks against the Worker and in-process board."""

import json
import os
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest

from conformance.support import ADMIN_TOKEN, ServiceTarget
from tasklease.client import Client
from tasklease.testing import FakeBoard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def worker_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    temporary = tmp_path_factory.mktemp("tasklease-conformance")
    assets = temporary / "assets"
    environment = os.environ.copy()
    environment.update(WRANGLER_SEND_METRICS="false", CI="true", NO_COLOR="1")
    subprocess.run(
        ["node", str(ROOT / "scripts/build-assets.mjs"), str(assets)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    bundle = assets / "service_bundle"
    value: object = json.loads((bundle / "wrangler.template.jsonc").read_text())
    assert isinstance(value, dict)
    config = cast(dict[str, object], value)
    config.pop("account_id", None)
    config["name"] = "tasklease-conformance"
    config["vars"] = {
        "ADMIN_TOKEN": ADMIN_TOKEN,
        "R2_ACCESS_KEY_ID": "conformance-r2-fake-key",
        "R2_SECRET_ACCESS_KEY": "conformance-r2-fake-secret",
        "R2_ACCOUNT_ID": "local",
        "R2_BUCKET_NAME": "tasklease-conformance-blobs",
    }
    config["r2_buckets"] = [
        {
            "binding": "BLOBS",
            "bucket_name": "tasklease-conformance-blobs",
            "remote": False,
        }
    ]
    filename = bundle / "wrangler.json"
    filename.write_text(json.dumps(config))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            "node",
            str(ROOT / "node_modules/wrangler/bin/wrangler.js"),
            "dev",
            "--local",
            "--config",
            str(filename),
            "--ip",
            "127.0.0.1",
            "--port",
            str(port),
            "--inspector-port",
            "0",
            "--persist-to",
            str(temporary / "state"),
            "--show-interactive-dev-session=false",
        ],
        cwd=temporary,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=os.name != "nt",
    )
    assert process.stdout is not None
    output = process.stdout
    logs: deque[str] = deque(maxlen=200)

    def drain() -> None:
        for line in output:
            logs.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=1, trust_env=False) as http:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        "Local Wrangler stopped during startup:\n" + "".join(logs)
                    )
                try:
                    response = http.get(url + "/v1/version")
                    if response.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Local Wrangler startup timed out:\n" + "".join(logs))
        yield url
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=10)
        reader.join(timeout=10)
        output.close()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--backend",
        choices=("both", "worker", "fake"),
        default="both",
        help="Conformance target (fake runs without Node, Wrangler, or sockets)",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "service_backend" in metafunc.fixturenames:
        selected = metafunc.config.getoption("--backend")
        values = ["worker", "fake"] if selected == "both" else [selected]
        metafunc.parametrize("service_backend", values, scope="session")


@pytest.fixture
def target_factory(
    service_backend: str, request: pytest.FixtureRequest
) -> Iterator[Callable[[str], ServiceTarget]]:
    # Resolve the process fixture lazily: fake-only runs never execute the
    # Worker build, socket allocation, or Wrangler startup code above.
    url = ""
    admin: Client | None = None
    if service_backend == "worker":
        url = cast(str, request.getfixturevalue("worker_url"))
        admin = cast(Client, request.getfixturevalue("worker_admin"))
    with ExitStack() as cleanup:

        def create(domain: str) -> ServiceTarget:
            target = ServiceTarget(
                domain,
                url,
                board=FakeBoard(domain) if admin is None else None,
                admin=admin,
            )
            cleanup.callback(target.close)
            return target

        yield create


@pytest.fixture(scope="session")
def worker_admin(worker_url: str) -> Iterator[Client]:
    with Client(
        worker_url, ADMIN_TOKEN, transport=httpx.HTTPTransport(trust_env=False)
    ) as admin:
        yield admin


@pytest.fixture
def target(target_factory: Callable[[str], ServiceTarget]) -> ServiceTarget:
    return target_factory("conformance/" + uuid4().hex)


@pytest.fixture
def corpus_target(target_factory: Callable[[str], ServiceTarget]) -> ServiceTarget:
    return target_factory("acme/pipeline")


@pytest.fixture
def corpus_token(corpus_target: ServiceTarget) -> str:
    return corpus_target.identity()[1]
