"""Home Assistant Sensors Gateway."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ha-sensors-gateway")
except PackageNotFoundError:
    __version__ = "0+unknown"
