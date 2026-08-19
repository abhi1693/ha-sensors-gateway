"""Single source of truth for gateway environment settings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntegerSetting:
    name: str
    default: int
    maximum: int


PORT = IntegerSetting("PORT", 8080, 65535)
RATE_LIMIT_PER_MINUTE = IntegerSetting("RATE_LIMIT_PER_MINUTE", 180, 10_000)
MAX_REQUEST_BYTES = IntegerSetting("MAX_REQUEST_BYTES", 2 * 1024 * 1024, 16 * 1024 * 1024)
MAX_RESPONSE_BYTES = IntegerSetting("MAX_RESPONSE_BYTES", 1024 * 1024, 16 * 1024 * 1024)
MAX_CONCURRENT_REQUESTS = IntegerSetting("MAX_CONCURRENT_REQUESTS", 64, 1024)
TIMEOUT_SECONDS = IntegerSetting("TIMEOUT_SECONDS", 15, 120)

INTEGER_SETTINGS = (
    PORT,
    RATE_LIMIT_PER_MINUTE,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_CONCURRENT_REQUESTS,
    TIMEOUT_SECONDS,
)

WEBHOOK_CONFIG_NAME = "WEBHOOK_CONFIG"
DEFAULT_WEBHOOK_CONFIG = "/run/secrets/webhooks.json"
