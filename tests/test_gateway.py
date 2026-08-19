"""Security, configuration, and forwarding tests."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ha_sensors_gateway.gateway import Configuration, create_server

WEBHOOK_ID = "a" * 64


class UpstreamHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        document = json.loads(body)
        self.requests.append((self.path, document))
        response = json.dumps({"accepted": document["type"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        config_path = Path(cls.temp_directory.name) / "webhooks.json"
        config_path.write_text(
            json.dumps({WEBHOOK_ID: {"device": "test-phone"}}),
            encoding="utf-8",
        )
        cls.config_path = str(config_path)

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever,
            daemon=True,
        )
        cls.upstream_thread.start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"

        cls.gateway = create_server(
            ("127.0.0.1", 0),
            cls.config_path,
            upstream_url,
            rate_limit=2,
            max_request_bytes=2 * 1024 * 1024,
        )
        cls.gateway_thread = threading.Thread(
            target=cls.gateway.serve_forever,
            daemon=True,
        )
        cls.gateway_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.gateway.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.temp_directory.cleanup()

    def setUp(self) -> None:
        UpstreamHandler.requests.clear()
        self.gateway.RequestHandlerClass.rate_limiter._requests.clear()

    def request(self, method: str, path: str, document: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(document).encode() if document is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_health_endpoint(self) -> None:
        status, body = self.request("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, json.loads(body))

    def test_health_endpoint_rejects_non_get_methods(self) -> None:
        status, _ = self.request("PUT", "/healthz")
        self.assertEqual(404, status)

    def test_complete_sensor_batch_is_forwarded_unchanged(self) -> None:
        document = {
            "type": "update_sensor_states",
            "data": [
                {"unique_id": "battery_level", "state": "42"},
                {
                    "unique_id": "arbitrary_future_sensor",
                    "state": "enabled",
                    "attributes": {"nested": [1, 2, 3]},
                },
            ],
        }
        status, _ = self.request("POST", f"/api/webhook/{WEBHOOK_ID}", document)
        self.assertEqual(200, status)
        self.assertEqual(
            [(f"/api/webhook/{WEBHOOK_ID}", document)],
            UpstreamHandler.requests,
        )

    def test_control_command_is_hidden_and_not_forwarded(self) -> None:
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}",
            {"type": "call_service", "domain": "lock", "service": "unlock"},
        )
        self.assertEqual(404, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_registration_update_cannot_replace_push_destination(self) -> None:
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}",
            {
                "type": "update_registration",
                "data": {
                    "app_data": {
                        "push_token": "attacker-token",
                        "push_url": "https://attacker.example/collect",
                    },
                    "app_version": "1.0",
                    "device_name": "test-phone",
                    "manufacturer": "example",
                    "model": "example",
                },
            },
        )
        self.assertEqual(404, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_encrypted_envelope_is_rejected_because_it_cannot_be_allowlisted(self) -> None:
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}",
            {"type": "encrypted", "encrypted": True, "encrypted_data": "opaque"},
        )
        self.assertEqual(404, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_unknown_capability_is_hidden(self) -> None:
        status, _ = self.request(
            "POST",
            f"/api/webhook/{'b' * 64}",
            {"type": "update_location"},
        )
        self.assertEqual(404, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_query_string_is_rejected(self) -> None:
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}?debug=true",
            {"type": "update_location"},
        )
        self.assertEqual(404, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_authenticated_malformed_requests_are_rate_limited_before_body(self) -> None:
        statuses = []
        for _ in range(3):
            request = Request(
                f"{self.base_url}/api/webhook/{WEBHOOK_ID}",
                data=b"type=update_location",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            statuses.append(context.exception.code)

        self.assertEqual([415, 415, 429], statuses)
        self.assertEqual([], UpstreamHandler.requests)

    def test_duplicate_type_is_rejected(self) -> None:
        request = Request(
            f"{self.base_url}/api/webhook/{WEBHOOK_ID}",
            data=b'{"type":"get_config","type":"call_service"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(400, context.exception.code)
        self.assertEqual([], UpstreamHandler.requests)

    def test_rate_limit_is_per_capability(self) -> None:
        for _ in range(2):
            status, _ = self.request(
                "POST",
                f"/api/webhook/{WEBHOOK_ID}",
                {"type": "get_config"},
            )
            self.assertEqual(200, status)
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}",
            {"type": "get_config"},
        )
        self.assertEqual(429, status)
        self.assertEqual(2, len(UpstreamHandler.requests))

    def test_oversized_authenticated_batch_is_rejected(self) -> None:
        request = (
            f"POST /api/webhook/{WEBHOOK_ID} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.gateway.server_port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {2 * 1024 * 1024 + 1}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        with socket.create_connection(("127.0.0.1", self.gateway.server_port)) as client:
            client.sendall(request)
            response = client.recv(1024)

        self.assertIn(b" 413 ", response.partition(b"\r\n")[0])
        self.assertEqual([], UpstreamHandler.requests)

    def test_slow_requests_expire_and_release_bounded_request_slot(self) -> None:
        gateway = create_server(
            ("127.0.0.1", 0),
            self.config_path,
            f"http://127.0.0.1:{self.upstream.server_port}",
            rate_limit=2,
            max_concurrent_requests=1,
            timeout_seconds=1,
        )
        gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        gateway_thread.start()
        address = ("127.0.0.1", gateway.server_port)
        slow_client = socket.create_connection(address, timeout=2)
        try:
            slow_client.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost")
            deadline = time.monotonic() + 1
            while gateway.active_requests != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(1, gateway.active_requests)

            with socket.create_connection(address, timeout=2) as excess_client:
                excess_client.sendall(
                    b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                self.assertEqual(b"", excess_client.recv(1024))

            slow_client.settimeout(2)
            self.assertEqual(b"", slow_client.recv(1024))
            deadline = time.monotonic() + 1
            while gateway.active_requests and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(0, gateway.active_requests)

            with socket.create_connection(address, timeout=2) as slow_body_client:
                slow_body_client.sendall(
                    (
                        f"POST /api/webhook/{WEBHOOK_ID} HTTP/1.1\r\n"
                        "Host: localhost\r\n"
                        "Content-Type: application/json\r\n"
                        "Content-Length: 100\r\n"
                        "Connection: close\r\n\r\n"
                        "{"
                    ).encode()
                )
                deadline = time.monotonic() + 1
                while gateway.active_requests != 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(1, gateway.active_requests)
                slow_body_client.settimeout(2)
                self.assertEqual(b"", slow_body_client.recv(1024))

            deadline = time.monotonic() + 1
            while gateway.active_requests and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(0, gateway.active_requests)

            with urlopen(f"http://127.0.0.1:{gateway.server_port}/healthz") as response:
                self.assertEqual(200, response.status)
        finally:
            slow_client.close()
            gateway.shutdown()
            gateway.server_close()


class ConfigurationTest(unittest.TestCase):
    def test_upstream_url_is_required(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "UPSTREAM_URL is required"),
        ):
            Configuration.from_environment()

    def test_generic_environment_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "UPSTREAM_URL": "http://home-assistant:8123/",
                "PORT": "9090",
                "MAX_CONCURRENT_REQUESTS": "12",
                "MAX_REQUEST_BYTES": "4096",
            },
            clear=True,
        ):
            configuration = Configuration.from_environment()
        self.assertEqual("http://home-assistant:8123", configuration.upstream_url)
        self.assertEqual(9090, configuration.port)
        self.assertEqual(12, configuration.max_concurrent_requests)
        self.assertEqual(4096, configuration.max_request_bytes)

    def test_upstream_credentials_are_rejected(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"UPSTREAM_URL": "http://user:password@home-assistant:8123"},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "must not contain credentials"),
        ):
            Configuration.from_environment()


if __name__ == "__main__":
    unittest.main()
