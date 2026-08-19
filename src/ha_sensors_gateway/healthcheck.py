"""Container health probe for the gateway HTTP handler."""

from __future__ import annotations

import os
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException

from ha_sensors_gateway.settings import PORT

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
        port = int(os.getenv(PORT.name, str(PORT.default)))
    except ValueError:
        raise SystemExit(1) from None
    if not 1 <= port <= PORT.maximum or not probe_health(port):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
