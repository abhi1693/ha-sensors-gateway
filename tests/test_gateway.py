"""Security, configuration, and forwarding tests."""

from __future__ import annotations

import hmac
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, urlopen

from ha_sensors_gateway.gateway import (
    INGEST_ONLY_COMMANDS,
    SUPPORTED_COMMANDS,
    Capability,
    Configuration,
    GatewayHandler,
    RateLimiter,
    build_upstream_opener,
    create_server,
    load_capabilities,
    normalize_upstream_url,
)
from ha_sensors_gateway.healthcheck import probe_health
from ha_sensors_gateway.settings import INTEGER_SETTINGS, MAX_REQUEST_BYTES

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


class RedirectTargetHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        self.requests.append((self.command, self.path))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, format: str, *args: object) -> None:
        return


class RedirectingUpstreamHandler(BaseHTTPRequestHandler):
    redirect_status: ClassVar[int] = 302
    redirect_url: ClassVar[str] = ""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(self.redirect_status)
        self.send_header("Location", self.redirect_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class FailingUpstreamHandler(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "disconnect"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.close_connection = True
        if self.mode == "disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.mode == "bad-status":
            self.wfile.write(b"NOT AN HTTP RESPONSE\r\n\r\n")
            self.wfile.flush()
            return
        if self.mode == "incomplete-chunk":
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
                b"A\r\n{}"
            )
            self.wfile.flush()
            return

        body = b'{"error":"maintenance"}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self.gateway.RequestHandlerClass.rate_limiter = RateLimiter(2)

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
            with error:
                return error.code, error.read()

    def raw_request(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.gateway.server_port)) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            return client.recv(4096)

    def test_health_endpoint(self) -> None:
        status, body = self.request("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, json.loads(body))
        self.assertTrue(probe_health(self.gateway.server_port))

    def test_health_endpoint_rejects_non_get_methods(self) -> None:
        status, _ = self.request("PUT", "/healthz")
        self.assertEqual(404, status)

    def test_head_health_and_unknown_get(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=2)
        try:
            connection.request("HEAD", "/healthz")
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertEqual(b"", response.read())
        finally:
            connection.close()

        status, body = self.request("GET", "/unknown")
        self.assertEqual(404, status)
        self.assertEqual({"error": "not found"}, json.loads(body))

    def test_unsupported_methods_are_hidden_without_a_server_banner(self) -> None:
        for method in ("TRACE", "CONNECT", "BREW"):
            with self.subTest(method=method):
                connection = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=2)
                try:
                    connection.request(method, "/healthz")
                    response = connection.getresponse()
                    self.assertEqual(404, response.status)
                    self.assertIsNone(response.getheader("Server"))
                    self.assertEqual({"error": "not found"}, json.loads(response.read()))
                finally:
                    connection.close()

    def test_successful_responses_do_not_expose_a_server_banner(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=2)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertIsNone(response.getheader("Server"))
            response.read()
        finally:
            connection.close()

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

    def test_every_supported_command_is_forwarded(self) -> None:
        self.gateway.RequestHandlerClass.rate_limiter = RateLimiter(len(SUPPORTED_COMMANDS))
        for command in sorted(SUPPORTED_COMMANDS):
            with self.subTest(command=command):
                status, _ = self.request(
                    "POST",
                    f"/api/webhook/{WEBHOOK_ID}",
                    {"type": command},
                )
                self.assertEqual(200, status)

        self.assertEqual(
            SUPPORTED_COMMANDS,
            {document["type"] for _, document in UpstreamHandler.requests},
        )

    def test_upstream_redirects_are_rejected_without_contacting_target(self) -> None:
        redirect_target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTargetHandler)
        redirect_target_thread = threading.Thread(
            target=redirect_target.serve_forever,
            daemon=True,
        )
        redirect_target_thread.start()
        RedirectTargetHandler.requests.clear()
        RedirectingUpstreamHandler.redirect_url = (
            f"http://127.0.0.1:{redirect_target.server_port}/redirect-target"
        )
        redirecting_upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RedirectingUpstreamHandler,
        )
        redirecting_upstream_thread = threading.Thread(
            target=redirecting_upstream.serve_forever,
            daemon=True,
        )
        redirecting_upstream_thread.start()
        gateway = create_server(
            ("127.0.0.1", 0),
            self.config_path,
            f"http://127.0.0.1:{redirecting_upstream.server_port}",
            rate_limit=10,
        )
        gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        gateway_thread.start()
        try:
            for redirect_status in (301, 302, 303, 307, 308):
                with self.subTest(redirect_status=redirect_status):
                    RedirectingUpstreamHandler.redirect_status = redirect_status
                    request = Request(
                        f"http://127.0.0.1:{gateway.server_port}/api/webhook/{WEBHOOK_ID}",
                        data=b'{"type":"get_config"}',
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(HTTPError) as context:
                        urlopen(request)
                    with context.exception:
                        self.assertEqual(502, context.exception.code)
            self.assertEqual([], RedirectTargetHandler.requests)
        finally:
            gateway.shutdown()
            gateway.server_close()
            redirecting_upstream.shutdown()
            redirecting_upstream.server_close()
            redirect_target.shutdown()
            redirect_target.server_close()

    def test_upstream_transport_failures_become_502_and_http_errors_are_preserved(self) -> None:
        failing_upstream = ThreadingHTTPServer(("127.0.0.1", 0), FailingUpstreamHandler)
        failing_upstream_thread = threading.Thread(
            target=failing_upstream.serve_forever,
            daemon=True,
        )
        failing_upstream_thread.start()
        gateway = create_server(
            ("127.0.0.1", 0),
            self.config_path,
            f"http://127.0.0.1:{failing_upstream.server_port}",
            rate_limit=10,
        )
        gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        gateway_thread.start()
        gateway_url = f"http://127.0.0.1:{gateway.server_port}/api/webhook/{WEBHOOK_ID}"
        try:
            for failure_mode in ("disconnect", "bad-status", "incomplete-chunk"):
                with self.subTest(failure_mode=failure_mode):
                    FailingUpstreamHandler.mode = failure_mode
                    request = Request(
                        gateway_url,
                        data=b'{"type":"get_config"}',
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(HTTPError) as context:
                        urlopen(request)
                    with context.exception:
                        self.assertEqual(502, context.exception.code)
                        self.assertEqual(
                            {"error": "upstream unavailable"},
                            json.loads(context.exception.read()),
                        )

            FailingUpstreamHandler.mode = "http-error"
            request = Request(
                gateway_url,
                data=b'{"type":"get_config"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            with context.exception:
                self.assertEqual(503, context.exception.code)
                self.assertEqual({"error": "maintenance"}, json.loads(context.exception.read()))
        finally:
            gateway.shutdown()
            gateway.server_close()
            failing_upstream.shutdown()
            failing_upstream.server_close()

    def test_upstream_socket_error_becomes_502(self) -> None:
        with patch.object(
            self.gateway.RequestHandlerClass.upstream_opener,
            "open",
            side_effect=ConnectionResetError("upstream reset"),
        ):
            status, body = self.request(
                "POST",
                f"/api/webhook/{WEBHOOK_ID}",
                {"type": "get_config"},
            )
        self.assertEqual(502, status)
        self.assertEqual({"error": "upstream unavailable"}, json.loads(body))
        self.assertEqual([], UpstreamHandler.requests)

    def test_upstream_timeout_becomes_502(self) -> None:
        with patch.object(
            self.gateway.RequestHandlerClass.upstream_opener,
            "open",
            side_effect=TimeoutError("upstream timed out"),
        ):
            status, body = self.request(
                "POST",
                f"/api/webhook/{WEBHOOK_ID}",
                {"type": "get_config"},
            )
        self.assertEqual(502, status)
        self.assertEqual({"error": "upstream unavailable"}, json.loads(body))

    def test_oversized_upstream_response_becomes_502(self) -> None:
        gateway = create_server(
            ("127.0.0.1", 0),
            self.config_path,
            f"http://127.0.0.1:{self.upstream.server_port}",
            max_response_bytes=4,
        )
        gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        gateway_thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{gateway.server_port}/api/webhook/{WEBHOOK_ID}",
                data=b'{"type":"get_config"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            with context.exception:
                self.assertEqual(502, context.exception.code)
                self.assertEqual(
                    {"error": "upstream unavailable"},
                    json.loads(context.exception.read()),
                )
        finally:
            gateway.shutdown()
            gateway.server_close()

    def test_upstream_response_validation(self) -> None:
        handler = object.__new__(GatewayHandler)
        handler.max_response_bytes = 16

        response = Mock()
        response.read.return_value = b"{}"
        response.status = None
        response.headers = {}
        with self.assertRaisesRegex(HTTPException, "no status"):
            handler._read_upstream_response(response)

        response.status = 200
        response.headers = {"Content-Type": "text/plain\r\nX-Injected: true"}
        buffered = handler._read_upstream_response(response)
        self.assertIsNotNone(buffered)
        self.assertEqual("application/octet-stream", buffered.content_type)

    def test_control_command_is_hidden_and_not_forwarded(self) -> None:
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}",
            {"type": "call_service", "domain": "lock", "service": "unlock"},
        )
        self.assertEqual(404, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_ingest_only_capability_rejects_read_commands(self) -> None:
        capability = Capability(device="test-phone", commands=INGEST_ONLY_COMMANDS)
        with patch.object(
            self.gateway.RequestHandlerClass,
            "capabilities",
            {WEBHOOK_ID: capability},
        ):
            status, _ = self.request(
                "POST",
                f"/api/webhook/{WEBHOOK_ID}",
                {"type": "get_config"},
            )
            self.assertEqual(404, status)
            self.assertEqual([], UpstreamHandler.requests)

            document = {"type": "update_location", "data": {"latitude": 1, "longitude": 2}}
            status, _ = self.request("POST", f"/api/webhook/{WEBHOOK_ID}", document)
            self.assertEqual(200, status)
            self.assertEqual([(f"/api/webhook/{WEBHOOK_ID}", document)], UpstreamHandler.requests)

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

    def test_capability_lookup_always_compares_every_configured_id(self) -> None:
        capabilities = {
            character * 64: Capability(device=f"phone-{character}", commands=INGEST_ONLY_COMMANDS)
            for character in ("a", "b", "c")
        }
        handler = object.__new__(self.gateway.RequestHandlerClass)
        with patch.object(handler, "capabilities", capabilities):
            for candidate, expected in (
                ("a" * 64, capabilities["a" * 64]),
                ("c" * 64, capabilities["c" * 64]),
                ("d" * 64, None),
            ):
                with (
                    self.subTest(candidate=candidate),
                    patch(
                        "ha_sensors_gateway.gateway.hmac.compare_digest",
                        wraps=hmac.compare_digest,
                    ) as compare_digest,
                ):
                    self.assertEqual(expected, handler._find_capability(candidate))
                    self.assertEqual(len(capabilities), compare_digest.call_count)

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
            with context.exception:
                statuses.append(context.exception.code)

        self.assertEqual([415, 415, 415], statuses)
        status, _ = self.request(
            "POST",
            f"/api/webhook/{WEBHOOK_ID}",
            {"type": "get_config"},
        )
        self.assertEqual(429, status)
        self.assertEqual([], UpstreamHandler.requests)

    def test_bad_content_type_does_not_reveal_capability_validity(self) -> None:
        statuses = []
        for webhook_id in (WEBHOOK_ID, "b" * 64):
            request = Request(
                f"{self.base_url}/api/webhook/{webhook_id}",
                data=b"type=update_location",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            with context.exception:
                statuses.append(context.exception.code)

        self.assertEqual([415, 415], statuses)
        self.assertEqual([], UpstreamHandler.requests)

    def test_transfer_encoding_and_malformed_lengths_are_rejected(self) -> None:
        requests = (
            (
                "Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n",
                400,
            ),
            ("Content-Type: application/json\r\n", 400),
            ("Content-Type: application/json\r\nContent-Length: nope\r\n", 413),
            ("Content-Type: application/json\r\nContent-Length: -1\r\n", 413),
            (
                "Content-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n",
                400,
            ),
        )
        self.gateway.RequestHandlerClass.rate_limiter = RateLimiter(len(requests))
        for headers, expected_status in requests:
            with self.subTest(headers=headers):
                request = (
                    f"POST /api/webhook/{WEBHOOK_ID} HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    f"{headers}"
                    "Connection: close\r\n\r\n"
                ).encode()
                response = self.raw_request(request)
                self.assertIn(
                    f" {expected_status} ".encode(),
                    response.partition(b"\r\n")[0],
                )
        self.assertEqual([], UpstreamHandler.requests)

    def test_incomplete_body_is_rejected(self) -> None:
        request = (
            f"POST /api/webhook/{WEBHOOK_ID} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 100\r\n"
            "Connection: close\r\n\r\n"
            "{}"
        ).encode()
        response = self.raw_request(request)

        self.assertIn(b" 400 ", response.partition(b"\r\n")[0])
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
        context.exception.close()
        self.assertEqual([], UpstreamHandler.requests)

    def test_invalid_json_shapes_are_rejected(self) -> None:
        self.gateway.RequestHandlerClass.rate_limiter = RateLimiter(3)
        statuses = []
        for body in (b"\xff", b"[]", b'{"type":null}'):
            with self.subTest(body=body):
                request = Request(
                    f"{self.base_url}/api/webhook/{WEBHOOK_ID}",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as context:
                    urlopen(request)
                with context.exception:
                    statuses.append(context.exception.code)
        self.assertEqual([400, 400, 404], statuses)
        self.assertEqual([], UpstreamHandler.requests)

    def test_excessively_nested_json_is_rejected(self) -> None:
        body = b"[" * 10_000 + b"]" * 10_000
        request = Request(
            f"{self.base_url}/api/webhook/{WEBHOOK_ID}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        with context.exception:
            self.assertEqual(400, context.exception.code)
            self.assertEqual(
                {"error": "invalid request"},
                json.loads(context.exception.read()),
            )
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

    def test_oversized_batch_does_not_reveal_capability_validity(self) -> None:
        for webhook_id in (WEBHOOK_ID, "b" * 64):
            with self.subTest(webhook_id=webhook_id):
                request = (
                    f"POST /api/webhook/{webhook_id} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.gateway.server_port}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {MAX_REQUEST_BYTES.default + 1}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                response = self.raw_request(request)

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

            self.assertFalse(probe_health(gateway.server_port))

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

            self.assertTrue(probe_health(gateway.server_port))
        finally:
            slow_client.close()
            gateway.shutdown()
            gateway.server_close()


class ConfigurationTest(unittest.TestCase):
    def load_capability_document(self, document: dict) -> dict[str, Capability]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "webhooks.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return load_capabilities(str(path))

    def load_capability_json(self, document: str) -> dict[str, Capability]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "webhooks.json"
            path.write_text(document, encoding="utf-8")
            return load_capabilities(str(path))

    def test_capabilities_support_legacy_ingest_only_and_explicit_commands(self) -> None:
        legacy_id = "a" * 64
        ingest_id = "b" * 64
        explicit_id = "c" * 64
        capabilities = self.load_capability_document(
            {
                legacy_id: {"device": "legacy-phone"},
                ingest_id: {"device": "ingest-phone", "profile": "ingest-only"},
                explicit_id: {
                    "device": "custom-phone",
                    "commands": ["update_sensor_states", "get_zones"],
                },
            }
        )

        self.assertEqual(SUPPORTED_COMMANDS, capabilities[legacy_id].commands)
        self.assertEqual(INGEST_ONLY_COMMANDS, capabilities[ingest_id].commands)
        self.assertEqual(
            frozenset({"update_sensor_states", "get_zones"}),
            capabilities[explicit_id].commands,
        )

    def test_example_capability_map_uses_ingest_only_profile(self) -> None:
        example_path = Path(__file__).parents[1] / "examples" / "webhooks.example.json"
        capabilities = load_capabilities(str(example_path))

        self.assertEqual(1, len(capabilities))
        self.assertEqual(INGEST_ONLY_COMMANDS, next(iter(capabilities.values())).commands)

    def test_invalid_capability_command_sets_fail_at_startup(self) -> None:
        invalid_metadata = (
            {"device": "test-phone", "profile": "standard"},
            {"device": "test-phone", "commands": []},
            {"device": "test-phone", "commands": "get_config"},
            {"device": "test-phone", "commands": [1]},
            {"device": "test-phone", "commands": ["call_service"]},
            {"device": "test-phone", "commands": ["get_config", "get_config"]},
            {
                "device": "test-phone",
                "profile": "ingest-only",
                "commands": ["update_location"],
            },
        )
        for metadata in invalid_metadata:
            with (
                self.subTest(metadata=metadata),
                self.assertRaisesRegex(ValueError, "mobile webhook"),
            ):
                self.load_capability_document({WEBHOOK_ID: metadata})

    def test_invalid_capability_documents_fail_at_startup(self) -> None:
        invalid_documents = (
            {},
            [],
            {"short-id": {"device": "test-phone"}},
            {WEBHOOK_ID: "test-phone"},
            {WEBHOOK_ID: {"device": "Test Phone"}},
            {WEBHOOK_ID: {"device": "test-phone", "unexpected": True}},
        )
        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.load_capability_document(document)

        duplicate = json.dumps({WEBHOOK_ID: {"device": "first-phone"}})
        duplicate = duplicate[:-1] + f',"{WEBHOOK_ID}":{{"device":"second-phone"}}}}'
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self.load_capability_json(duplicate)

    def test_upstream_opener_does_not_load_environment_proxies(self) -> None:
        with patch("urllib.request.getproxies", side_effect=AssertionError("proxy lookup")):
            opener = build_upstream_opener()
        proxy_handlers = [
            handler for handler in opener.handlers if isinstance(handler, ProxyHandler)
        ]
        self.assertEqual([], proxy_handlers)

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

    def test_invalid_integer_settings_fail_at_startup(self) -> None:
        for setting in INTEGER_SETTINGS:
            for value in ("invalid", "0", str(setting.maximum + 1)):
                with (
                    self.subTest(setting=setting.name, value=value),
                    patch.dict(
                        os.environ,
                        {"UPSTREAM_URL": "http://home-assistant", setting.name: value},
                        clear=True,
                    ),
                    self.assertRaisesRegex(ValueError, setting.name),
                ):
                    Configuration.from_environment()

    def test_valid_upstream_origins_are_normalized(self) -> None:
        origins = {
            "http://home-assistant:8123/": "http://home-assistant:8123",
            "https://home-assistant": "https://home-assistant",
            "http://127.0.0.1:8123/": "http://127.0.0.1:8123",
            "https://[2001:db8::1]:8123/": "https://[2001:db8::1]:8123",
        }
        for value, expected in origins.items():
            with self.subTest(value=value):
                self.assertEqual(expected, normalize_upstream_url(value))

    def test_invalid_upstream_origins_are_rejected_at_startup(self) -> None:
        invalid_origins = (
            "http://home-assistant:bad",
            "http://home-assistant:70000",
            "http://home-assistant:0",
            "http://home-assistant:",
            "http://home-assistant/base",
            "http://home-assistant/?",
            "http://home-assistant/#",
            "http://home-assistant?",
            "http://home-assistant#",
            "http://home assistant:8123",
            "http://home-assistant:8123\n",
            "http://home-assistant:8123\\suffix",
            "http://home-assistant\\evil",
            "http://[::1",
            "ftp://home-assistant",
        )
        for value in invalid_origins:
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"UPSTREAM_URL": value}, clear=True),
                self.assertRaisesRegex(ValueError, "UPSTREAM_URL"),
            ):
                Configuration.from_environment()

        with self.assertRaisesRegex(ValueError, "UPSTREAM_URL"):
            normalize_upstream_url("http://home-assistant:8123\x00")

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


class RateLimiterTest(unittest.TestCase):
    def test_expired_requests_are_removed(self) -> None:
        limiter = RateLimiter(limit=1, window_seconds=60)
        with patch("ha_sensors_gateway.gateway.time.monotonic", side_effect=(0.0, 61.0)):
            self.assertTrue(limiter.allow("capability"))
            self.assertTrue(limiter.allow("capability"))


if __name__ == "__main__":
    unittest.main()
