"""Forward an allowlist of Home Assistant Companion App webhook commands."""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from ha_sensors_gateway import __version__

WEBHOOK_PATH = re.compile(r"^/api/webhook/([0-9a-f]{64})$")
ALLOWED_COMMANDS = frozenset(
    {
        "get_config",
        "get_zones",
        "register_sensor",
        "update_location",
        "update_sensor_states",
    }
)


def emit_log(event: str, **fields: object) -> None:
    """Write one secret-free structured event."""
    print(json.dumps({"event": event, **fields}, separators=(",", ":"), sort_keys=True), flush=True)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def read_positive_integer(
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def normalize_upstream_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("UPSTREAM_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("UPSTREAM_URL must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


class NoRedirectHandler(HTTPRedirectHandler):
    """Refuse to turn an upstream redirect into a request to another URL."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        status: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def build_upstream_opener() -> OpenerDirector:
    """Build an upstream client isolated from proxy environment variables."""
    return build_opener(ProxyHandler({}), NoRedirectHandler())


def load_capabilities(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as config_file:
        document = json.load(config_file, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(document, dict) or not document:
        raise ValueError("at least one mobile webhook must be configured")

    capabilities: ClassVar[dict[str, str]] = {}
    for webhook_id, metadata in document.items():
        if not isinstance(webhook_id, str) or not WEBHOOK_PATH.fullmatch(
            f"/api/webhook/{webhook_id}"
        ):
            raise ValueError("invalid mobile webhook identifier")
        if not isinstance(metadata, dict) or set(metadata) != {"device"}:
            raise ValueError("invalid mobile webhook metadata")
        device = metadata["device"]
        if not isinstance(device, str) or not re.fullmatch(r"[a-z0-9-]{1,32}", device):
            raise ValueError("invalid device alias")
        capabilities[webhook_id] = device
    return capabilities


@dataclass(frozen=True)
class Configuration:
    port: int
    webhook_config: str
    upstream_url: str
    rate_limit_per_minute: int
    max_request_bytes: int
    max_response_bytes: int
    max_concurrent_requests: int
    timeout_seconds: int

    @classmethod
    def from_environment(cls) -> Configuration:
        upstream_url = os.getenv("UPSTREAM_URL")
        if not upstream_url:
            raise ValueError("UPSTREAM_URL is required")
        return cls(
            port=read_positive_integer("PORT", 8080, maximum=65535),
            webhook_config=os.getenv("WEBHOOK_CONFIG", "/run/secrets/webhooks.json"),
            upstream_url=normalize_upstream_url(upstream_url),
            rate_limit_per_minute=read_positive_integer(
                "RATE_LIMIT_PER_MINUTE", 180, maximum=10_000
            ),
            max_request_bytes=read_positive_integer(
                "MAX_REQUEST_BYTES", 2 * 1024 * 1024, maximum=16 * 1024 * 1024
            ),
            max_response_bytes=read_positive_integer(
                "MAX_RESPONSE_BYTES", 1024 * 1024, maximum=16 * 1024 * 1024
            ),
            max_concurrent_requests=read_positive_integer(
                "MAX_CONCURRENT_REQUESTS", 64, maximum=1024
            ),
            timeout_seconds=read_positive_integer("TIMEOUT_SECONDS", 15, maximum=120),
        )


@dataclass
class RateLimiter:
    limit: int
    window_seconds: int = 60

    def __post_init__(self) -> None:
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests[key]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                return False
            requests.append(now)
            return True


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = f"HASensorsGateway/{__version__}"
    sys_version = ""
    capabilities: ClassVar[dict[str, str]] = {}
    upstream_url = ""
    upstream_opener: ClassVar[OpenerDirector] = build_upstream_opener()
    rate_limiter = RateLimiter(180)
    max_request_bytes = 2 * 1024 * 1024
    max_response_bytes = 1024 * 1024
    timeout_seconds = 15

    def setup(self) -> None:
        self.request.settimeout(self.timeout_seconds)
        super().setup()
        # Socket timeouts reset after successful reads; this timer bounds the
        # complete header-and-body phase even when a client sends a slow trickle.
        self._inbound_deadline_lock = threading.Lock()
        self._inbound_deadline_active = True
        self._inbound_timed_out = threading.Event()
        self._inbound_deadline_timer = threading.Timer(
            self.timeout_seconds,
            self._expire_inbound_request,
        )
        self._inbound_deadline_timer.daemon = True
        self._inbound_deadline_timer.start()

    def finish(self) -> None:
        self._complete_inbound_request()
        super().finish()

    def _complete_inbound_request(self) -> bool:
        with self._inbound_deadline_lock:
            self._inbound_deadline_active = False
        self._inbound_deadline_timer.cancel()
        # Do not release the request slot while a canceled deadline thread is
        # still alive; this keeps total worker and timer threads strictly bounded.
        if self._inbound_deadline_timer is not threading.current_thread():
            self._inbound_deadline_timer.join()
        return not self._inbound_timed_out.is_set()

    def _expire_inbound_request(self) -> None:
        with self._inbound_deadline_lock:
            if not self._inbound_deadline_active:
                return
            self._inbound_deadline_active = False
            self._inbound_timed_out.set()
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            return

    def _respond(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _not_found(self) -> None:
        self._respond(404, b'{"error":"not found"}')

    def _find_device(self, candidate: str) -> str | None:
        for webhook_id, device in self.capabilities.items():
            if hmac.compare_digest(candidate, webhook_id):
                return device
        return None

    def _reject(self, device: str, status: int, reason: str) -> None:
        emit_log("rejected", device=device, reason=reason, status=status)
        if status == 404:
            self._not_found()
        else:
            self._respond(status, b'{"error":"invalid request"}')

    def do_POST(self) -> None:
        if self._inbound_timed_out.is_set():
            return
        parsed_path = urlsplit(self.path)
        if parsed_path.query or parsed_path.fragment:
            self._not_found()
            return
        match = WEBHOOK_PATH.fullmatch(parsed_path.path)
        if not match:
            self._not_found()
            return

        device = self._find_device(match.group(1))
        if device is None:
            self._not_found()
            return
        if not self.rate_limiter.allow(match.group(1)):
            self._reject(device, 429, "rate-limit")
            return
        if self.headers.get("Transfer-Encoding"):
            self._reject(device, 400, "transfer-encoding")
            return
        if self.headers.get_content_type().lower() != "application/json":
            self._reject(device, 415, "content-type")
            return

        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) != 1:
            self._reject(device, 400, "content-length")
            return
        try:
            content_length = int(content_lengths[0])
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > self.max_request_bytes:
            self._reject(device, 413, "request-size")
            return

        try:
            body = self.rfile.read(content_length)
        except TimeoutError:
            self._reject(device, 408, "request-timeout")
            return
        if len(body) != content_length:
            if self._inbound_timed_out.is_set():
                emit_log("rejected", device=device, reason="request-timeout", status=408)
                return
            self._reject(device, 400, "incomplete-body")
            return
        if not self._complete_inbound_request():
            emit_log("rejected", device=device, reason="request-timeout", status=408)
            return

        try:
            document = json.loads(body, object_pairs_hook=reject_duplicate_keys)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._reject(device, 400, "json")
            return
        if not isinstance(document, dict):
            self._reject(device, 400, "json-shape")
            return
        command = document.get("type")
        if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
            self._reject(device, 404, "command")
            return
        request = Request(
            f"{self.upstream_url}{parsed_path.path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.upstream_opener.open(request, timeout=self.timeout_seconds) as response:
                self._forward_response(response, device, command)
        except HTTPError as error:
            with error:
                if 300 <= error.code < 400:
                    self._upstream_unavailable(device, command, "redirect-rejected")
                else:
                    self._forward_response(error, device, command)
        except (TimeoutError, URLError):
            self._upstream_unavailable(device, command, "upstream-unavailable")

    def _upstream_unavailable(self, device: str, command: str, status: str) -> None:
        emit_log("forwarded", command=command, device=device, status=status)
        self._respond(502, b'{"error":"upstream unavailable"}')

    def _forward_response(
        self,
        response: HTTPResponse | HTTPError,
        device: str,
        command: str,
    ) -> None:
        response_body = response.read(self.max_response_bytes + 1)
        if len(response_body) > self.max_response_bytes:
            emit_log(
                "forwarded",
                command=command,
                device=device,
                status="response-too-large",
            )
            self._respond(502, b'{"error":"upstream unavailable"}')
            return
        status = response.status
        content_type = response.headers.get("Content-Type", "application/json")
        if "\r" in content_type or "\n" in content_type:
            content_type = "application/octet-stream"
        emit_log("forwarded", command=command, device=device, status=status)
        self._respond(status, response_body, content_type)

    def do_GET(self) -> None:
        if not self._complete_inbound_request():
            return
        if urlsplit(self.path).path == "/healthz" and not urlsplit(self.path).query:
            self._respond(200, b'{"status":"ok"}')
            return
        self._not_found()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_PUT(self) -> None:
        if not self._complete_inbound_request():
            return
        self._not_found()

    do_PATCH = do_PUT
    do_DELETE = do_PUT
    do_OPTIONS = do_PUT

    def log_message(self, format: str, *args: object) -> None:
        return


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        max_concurrent_requests: int,
    ) -> None:
        self.max_concurrent_requests = max_concurrent_requests
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._request_count_lock = threading.Lock()
        self._active_requests = 0
        super().__init__(server_address, request_handler)

    @property
    def active_requests(self) -> int:
        with self._request_count_lock:
            return self._active_requests

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        # Acquire before ThreadingMixIn creates a worker so excess connections
        # cannot allocate additional request or deadline threads.
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._request_count_lock:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_request_slot()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_request_slot()

    def _release_request_slot(self) -> None:
        with self._request_count_lock:
            self._active_requests -= 1
        self._request_slots.release()


def create_server(
    address: tuple[str, int],
    config_path: str,
    upstream_url: str,
    rate_limit: int,
    *,
    max_request_bytes: int = 2 * 1024 * 1024,
    max_response_bytes: int = 1024 * 1024,
    max_concurrent_requests: int = 64,
    timeout_seconds: int = 15,
) -> GatewayServer:
    capabilities = load_capabilities(config_path)
    handler = type(
        "ConfiguredGatewayHandler",
        (GatewayHandler,),
        {
            "capabilities": capabilities,
            "upstream_url": normalize_upstream_url(upstream_url),
            "upstream_opener": build_upstream_opener(),
            "rate_limiter": RateLimiter(rate_limit),
            "max_request_bytes": max_request_bytes,
            "max_response_bytes": max_response_bytes,
            "timeout_seconds": timeout_seconds,
        },
    )
    return GatewayServer(address, handler, max_concurrent_requests)


def main() -> None:
    configuration = Configuration.from_environment()
    server = create_server(
        ("0.0.0.0", configuration.port),
        configuration.webhook_config,
        configuration.upstream_url,
        configuration.rate_limit_per_minute,
        max_request_bytes=configuration.max_request_bytes,
        max_response_bytes=configuration.max_response_bytes,
        max_concurrent_requests=configuration.max_concurrent_requests,
        timeout_seconds=configuration.timeout_seconds,
    )
    emit_log(
        "started",
        devices=len(server.RequestHandlerClass.capabilities),
        max_concurrent_requests=configuration.max_concurrent_requests,
        port=configuration.port,
        version=__version__,
    )
    server.serve_forever()
