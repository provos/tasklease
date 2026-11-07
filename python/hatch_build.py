"""Build release assets in a checkout; reuse them when building an sdist."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import cast

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface[BuilderConfig]):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return

        python_root = Path(self.root)
        checkout = python_root.parent
        source_files = (
            checkout / "scripts/build-assets.mjs",
            checkout / "monitor/package.json",
            checkout / "service/wrangler.jsonc",
            checkout / "package-lock.json",
        )
        if all(path.is_file() for path in source_files):
            if shutil.which("node") is None or shutil.which("npm") is None:
                raise RuntimeError(
                    "Building tasklease from a checkout requires Node.js and npm"
                )
            if not (checkout / "node_modules/.package-lock.json").is_file():
                subprocess.run(["npm", "ci"], cwd=checkout, check=True)
            subprocess.run(
                ["node", str(source_files[0]), str(python_root / "src/tasklease")],
                cwd=checkout,
                check=True,
            )

        self._check_assets(python_root)

    def _check_assets(self, python_root: Path) -> None:
        package = python_root / "src/tasklease"
        bundle = package / "service_bundle"
        try:
            if not (package / "monitor/static/index.html").is_file():
                raise ValueError("dashboard index.html is missing")
            manifest: object = json.loads((bundle / "manifest.json").read_text())
            template: object = json.loads(
                (bundle / "wrangler.template.jsonc").read_text()
            )
            if not isinstance(manifest, dict) or not isinstance(template, dict):
                raise ValueError("bundle metadata must contain JSON objects")
            manifest = cast(dict[str, object], manifest)
            template = cast(dict[str, object], template)
            if manifest.get("service_version") != self.metadata.version:
                raise ValueError(
                    "Worker bundle version differs from the package version"
                )
            wrangler_version = manifest.get("wrangler_version")
            if (
                not isinstance(wrangler_version, str)
                or re.fullmatch(
                    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", wrangler_version
                )
                is None
            ):
                raise ValueError("Worker manifest must pin an exact Wrangler version")
            main = template.get("main")
            if (
                not isinstance(main, str)
                or not main.endswith(".js")
                or Path(main).is_absolute()
                or ".." in Path(main).parts
                or not (bundle / main).is_file()
                or template.get("no_bundle") is not True
            ):
                raise ValueError("Worker template must point to its bundled JavaScript")
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "tasklease release assets are missing or invalid: "
                f"{error}. Build from the complete checkout with Node.js, or use "
                "an sdist containing the prebuilt dashboard and Worker."
            ) from error
