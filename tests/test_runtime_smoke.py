"""Run the image smoke's real entrypoint and local upstream checks in source CI."""

from __future__ import annotations

import runpy
import unittest
from pathlib import Path


class RuntimeSmokeTest(unittest.TestCase):
    def test_actual_gateway_with_read_only_local_upstream(self) -> None:
        smoke = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "runtime_smoke.py"))
        self.assertEqual(
            {"health": True, "healthcheck": True, "read_forwarded": 1, "blocked_forwarded": 0},
            smoke["exercise_gateway"](),
        )
