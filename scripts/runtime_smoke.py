"""Exercise the actual gateway process against a loopback-only read fixture."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from ha_sensors_gateway import __version__
from ha_sensors_gateway.gateway import SUPPORTED_COMMANDS

FIXTURE_ID = "a" * 64
FIXTURE_BODY = b'{"type":"get_zones"}'


class FixtureHandler(BaseHTTPRequestHandler):
    """Accept one specific read command; never contact a real upstream."""

    requests: ClassVar[list[tuple[str, bytes]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.requests.append((self.path, body))
        valid = (
            self.path == f"/api/webhook/{FIXTURE_ID}"
            and body == FIXTURE_BODY
            and self.headers.get_content_type() == "application/json"
        )
        response = b'{"zones":[]}' if valid else b'{"error":"fixture rejected"}'
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


def request(port: int, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        assert response.getheader("Server") is None, "Gateway exposed a version banner"
        return response.status, response.read()
    finally:
        connection.close()


def exercise_gateway() -> dict[str, object]:
    """Start the normal module entrypoint and prove health/filtering/forwarding."""
    FixtureHandler.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "webhooks.json")
            config.write_text(json.dumps({FIXTURE_ID: {"device": "ci", "commands": ["get_zones"]}}))
            environment = {
                **os.environ,
                "PORT": str(port),
                "WEBHOOK_CONFIG": str(config),
                "UPSTREAM_URL": f"http://127.0.0.1:{upstream.server_port}",
                "TIMEOUT_SECONDS": "2",
            }
            with tempfile.TemporaryFile() as logs:
                process = subprocess.Popen(
                    [sys.executable, "-m", "ha_sensors_gateway"],
                    env=environment,
                    stdout=logs,
                    stderr=logs,
                )
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        assert process.poll() is None, "Gateway exited during startup"
                        try:
                            if request(port, "GET", "/healthz") == (200, b'{"status":"ok"}'):
                                break
                        except OSError:
                            pass
                        assert time.monotonic() < deadline, "Gateway did not become healthy"
                        time.sleep(0.05)
                    subprocess.run(
                        [sys.executable, "-m", "ha_sensors_gateway.healthcheck"],
                        env=environment,
                        check=True,
                        timeout=5,
                    )
                    assert request(port, "GET", "/api/states")[0] == 404
                    path = f"/api/webhook/{FIXTURE_ID}"
                    assert request(port, "POST", path, FIXTURE_BODY) == (200, b'{"zones":[]}')
                    assert request(port, "POST", path, b'{"type":"call_service"}')[0] == 404
                    assert request(port, "POST", path, b'{"type":"get_config"}')[0] == 404
                    assert request(port, "POST", f"/api/webhook/{'b' * 64}", FIXTURE_BODY)[0] == 404
                    assert FixtureHandler.requests == [(path, FIXTURE_BODY)], (
                        "Rejected traffic reached the upstream fixture"
                    )
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                logs.seek(0)
                assert FIXTURE_ID.encode() not in logs.read(), "Gateway logged a capability"
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)
    return {"health": True, "healthcheck": True, "read_forwarded": 1, "blocked_forwarded": 0}


def main() -> None:
    assert os.getuid() == os.getgid() == 65532
    expected_arch = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}[
        os.environ["EXPECTED_PLATFORM"]
    ]
    assert platform.machine() == expected_arch
    assert platform.python_version() == os.environ["EXPECTED_PYTHON_VERSION"]
    with Path("/app/pyproject.toml").open("rb") as metadata:
        source_version = tomllib.load(metadata)["project"]["version"]
    assert __version__ == source_version == os.environ["EXPECTED_APP_VERSION"]
    assert importlib.util.find_spec("pip") is None
    assert not list(Path("/usr/local/bin").glob("pip*"))
    assert "call_service" not in SUPPORTED_COMMANDS
    proof = exercise_gateway()
    print(json.dumps({"version": __version__, "python": platform.python_version(), **proof}))


if __name__ == "__main__":
    main()
