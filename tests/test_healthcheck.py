"""Tests for the standalone container health probe."""

from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import patch

from ha_sensors_gateway.healthcheck import main, probe_health
from ha_sensors_gateway.settings import PORT


class HealthcheckTest(unittest.TestCase):
    def test_unreachable_gateway_is_unhealthy(self) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            unused_port = reservation.getsockname()[1]

        self.assertFalse(probe_health(unused_port))

    def test_main_uses_configured_port(self) -> None:
        with (
            patch.dict(os.environ, {PORT.name: "19090"}, clear=True),
            patch("ha_sensors_gateway.healthcheck.probe_health", return_value=True) as probe,
        ):
            main()
        probe.assert_called_once_with(19090)

    def test_main_rejects_invalid_ports_and_failed_probes(self) -> None:
        for value in ("invalid", "0", str(PORT.maximum + 1)):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {PORT.name: value}, clear=True),
                self.assertRaises(SystemExit) as context,
            ):
                main()
            self.assertEqual(1, context.exception.code)

        with (
            patch.dict(os.environ, {PORT.name: str(PORT.default)}, clear=True),
            patch("ha_sensors_gateway.healthcheck.probe_health", return_value=False),
            self.assertRaises(SystemExit) as context,
        ):
            main()
        self.assertEqual(1, context.exception.code)


if __name__ == "__main__":
    unittest.main()
