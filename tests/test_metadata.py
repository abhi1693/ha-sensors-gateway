"""Tests that prevent duplicated project metadata from drifting."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from ha_sensors_gateway.settings import (
    DEFAULT_WEBHOOK_CONFIG,
    INTEGER_SETTINGS,
    PORT,
    WEBHOOK_CONFIG_NAME,
)

ROOT = Path(__file__).parents[1]


class MetadataConsistencyTest(unittest.TestCase):
    def test_development_tool_versions_have_one_source(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("requirements-dev.txt", readme)
        self.assertIn("requirements-dev.txt", workflow)
        for duplicated_version in ("coverage==", "mypy==", "ruff=="):
            self.assertNotIn(duplicated_version, readme)
            self.assertNotIn(duplicated_version, workflow)

    def test_documented_setting_defaults_and_limits_match_runtime(self) -> None:
        readme_lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        rows = {
            line.split("`")[1]: line
            for line in readme_lines
            if line.startswith("| `") and "` |" in line
        }

        for setting in INTEGER_SETTINGS:
            with self.subTest(setting=setting.name):
                row = rows[setting.name]
                self.assertIn(f"| `{setting.default}` |", row)
                self.assertIn(str(setting.maximum), row)
        self.assertIn(f"| `{DEFAULT_WEBHOOK_CONFIG}` |", rows[WEBHOOK_CONFIG_NAME])

    def test_image_examples_match_project_version(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as project_file:
            project_version = tomllib.load(project_file)["project"]["version"]
        image = f"ghcr.io/abhi1693/ha-sensors-gateway:{project_version}"

        self.assertIn(image, (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn(
            f"image: {image}",
            (ROOT / "examples" / "compose.yaml").read_text(encoding="utf-8"),
        )

    def test_container_port_metadata_matches_runtime_default(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "examples" / "compose.yaml").read_text(encoding="utf-8")
        compose_default = f"${{PORT:-{PORT.default}}}"

        self.assertIn(f"EXPOSE {PORT.default}", dockerfile)
        self.assertEqual(3, compose.count(compose_default))


if __name__ == "__main__":
    unittest.main()
