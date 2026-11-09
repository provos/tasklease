"""Exercise the release artifact, including installation away from the checkout."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import cast

import pytest

from tasklease import __version__

PYTHON_ROOT = Path(__file__).resolve().parents[1]


def run_build(
    source: Path, output: Path, *, no_node: bool = False
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    if no_node:
        environment["PATH"] = ""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output),
            *(["--wheel"] if no_node else []),
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("release")
    result = run_build(PYTHON_ROOT, output)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def json_object(content: bytes) -> dict[str, object]:
    value: object = json.loads(content)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_wheel_contains_deployable_assets(artifacts: Path) -> None:
    with zipfile.ZipFile(next(artifacts.glob("*.whl"))) as wheel:
        files = set(wheel.namelist())
        assert "tasklease/monitor/static/index.html" in files
        assert any(
            name.startswith("tasklease/monitor/static/assets/") for name in files
        )
        assert {
            "tasklease/worker_templates/systemd.service",
            "tasklease/worker_templates/launchd.plist",
            "tasklease/worker_templates/windows.xml",
            "tasklease/worker_templates/windows-stop.ps1",
        } <= files
        prefix = "tasklease/service_bundle/"
        manifest = json_object(wheel.read(prefix + "manifest.json"))
        template = json_object(wheel.read(prefix + "wrangler.template.jsonc"))
        assert manifest["service_version"] == __version__
        service_package = json_object(
            (PYTHON_ROOT.parent / "service/package.json").read_bytes()
        )
        dependencies = service_package["devDependencies"]
        assert isinstance(dependencies, dict)
        assert manifest["wrangler_version"] == dependencies["wrangler"]
        assert template["no_bundle"] is True
        main = template["main"]
        assert isinstance(main, str) and main.endswith(".js")
        assert prefix + main in files
        notices = wheel.read(prefix + "THIRD_PARTY_NOTICES.md").decode()
        dashboard_notices = wheel.read(
            "tasklease/monitor/static/THIRD_PARTY_NOTICES.md"
        ).decode()
        for name, license_file in (
            ("ajv", "LICENSE"),
            ("aws4fetch", "LICENSE"),
            ("svelte", "LICENSE.md"),
            ("vite", "LICENSE.md"),
        ):
            upstream = PYTHON_ROOT.parent / "node_modules" / name / license_file
            assert upstream.read_text().strip() in notices
        assert "Svelte Contributors" in dashboard_notices
        metadata = BytesParser().parsebytes(
            wheel.read(f"tasklease-{__version__}.dist-info/METADATA")
        )
        assert metadata["License-Expression"] == "Apache-2.0"
        assert metadata["Description-Content-Type"] == "text/markdown"
        assert (PYTHON_ROOT / "README.md").read_text() in metadata.get_payload()
        license_text = (PYTHON_ROOT / "LICENSE").read_bytes()
        assert license_text == (PYTHON_ROOT.parent / "LICENSE").read_bytes()
        assert (
            wheel.read(f"tasklease-{__version__}.dist-info/licenses/LICENSE")
            == license_text
        )
        assert f'var version = "{__version__}"'.encode() in wheel.read(prefix + main)
        assert not any(
            name.endswith(".ts") for name in files if name.startswith(prefix)
        )


def unpack_sdist(artifacts: Path, destination: Path) -> Path:
    with tarfile.open(next(artifacts.glob("*.tar.gz"))) as archive:
        archive.extractall(destination, filter="data")
    return next(destination.iterdir())


def test_sdist_builds_without_node_and_installs_cleanly(
    artifacts: Path, tmp_path: Path
) -> None:
    source = unpack_sdist(artifacts, tmp_path / "unpacked")
    assert (source / "LICENSE").read_bytes() == (PYTHON_ROOT / "LICENSE").read_bytes()
    assert (source / "README.md").read_bytes() == (
        PYTHON_ROOT / "README.md"
    ).read_bytes()
    output = tmp_path / "wheel"
    result = run_build(source, output, no_node=True)
    assert result.returncode == 0, result.stdout + result.stderr
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PATH"] = ""
    installation = tmp_path / "installed"
    venv.EnvBuilder().create(installation)
    bin_directory = installation / ("Scripts" if sys.platform == "win32" else "bin")
    python = bin_directory / ("python.exe" if sys.platform == "win32" else "python")
    uv = shutil.which("uv")
    assert uv is not None, "The release installation check requires uv"
    install = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(next(output.glob("*.whl"))),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    check = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "from importlib.resources import files\n"
            "from tasklease.client import Client\n"
            "from tasklease import __version__\n"
            "import json\n"
            "p = files('tasklease')\n"
            "assert 'site-packages' in str(p)\n"
            "assert p.joinpath('monitor/static/index.html').is_file()\n"
            "text = p.joinpath('service_bundle/manifest.json').read_text()\n"
            "manifest = json.loads(text)\n"
            "assert manifest['service_version'] == __version__\n"
            "from tasklease.cli.service import DEPLOY_HELP\n"
            "login = 'wrangler@' + manifest['wrangler_version'] + ' login'\n"
            "assert login in DEPLOY_HELP\n"
            "Client('http://localhost:8787', 'token').close()\n"
            "import io, plistlib\n"
            "from contextlib import redirect_stdout\n"
            "from tasklease.cli import main\n"
            "out = io.StringIO()\n"
            "with redirect_stdout(out):\n"
            "    assert main(['worker', 'template', '--platform', 'launchd']) == 0\n"
            "assert plistlib.loads(out.getvalue().encode())['RunAtLoad'] is True\n",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    command = subprocess.run(
        [
            str(bin_directory / ("tl.exe" if sys.platform == "win32" else "tl")),
            "--version",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 0, command.stderr
    assert command.stdout.strip() == f"tl {__version__}"


@pytest.mark.parametrize("directory", ["monitor/static", "service_bundle"])
def test_sdist_without_third_party_notices_fails(
    artifacts: Path, tmp_path: Path, directory: str
) -> None:
    source = unpack_sdist(artifacts, tmp_path / "unpacked")
    (source / "src/tasklease" / directory / "THIRD_PARTY_NOTICES.md").unlink()
    result = run_build(source, tmp_path / "wheel", no_node=True)
    assert result.returncode != 0
    assert "bundled third-party notices are missing" in result.stdout + result.stderr


def test_incomplete_sdist_fails_clearly(artifacts: Path, tmp_path: Path) -> None:
    source = unpack_sdist(artifacts, tmp_path / "unpacked")
    shutil.rmtree(source / "src/tasklease/service_bundle")
    result = run_build(source, tmp_path / "wheel", no_node=True)
    assert result.returncode != 0
    assert (
        "tasklease release assets are missing or invalid"
        in result.stdout + result.stderr
    )
    environment = os.environ.copy()
    environment["PATH"] = ""
    editable = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hatchling.build import build_editable; build_editable('editable')",
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert editable.returncode == 0, editable.stdout + editable.stderr


def test_stale_sdist_assets_fail_clearly(artifacts: Path, tmp_path: Path) -> None:
    source = unpack_sdist(artifacts, tmp_path / "unpacked")
    project = source / "pyproject.toml"
    project.write_text(
        project.read_text().replace(f'version = "{__version__}"', 'version = "9.8.7"')
    )
    result = run_build(source, tmp_path / "wheel", no_node=True)
    assert result.returncode != 0
    assert "Worker bundle version differs" in result.stdout + result.stderr
