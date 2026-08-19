"""Container health probe for the gateway HTTP handler."""

from __future__ import annotations

import os
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException

EXPECTED_BODY = b'{"status":"ok"}'
PROBE_TIMEOUT_SECONDS = 2


def probe_health(port: int) -> bool:
    connection = HTTPConnection("127.0.0.1", port, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        return response.status == HTTPStatus.OK and response.read() == EXPECTED_BODY
    except (HTTPException, OSError):
        return False
    finally:
        connection.close()


def main() -> None:
    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError:
        raise SystemExit(1) from None
    if not 1 <= port <= 65535 or not probe_health(port):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
