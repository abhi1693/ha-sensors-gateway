"""Home Assistant Sensors Gateway."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tomllib import load

try:
    __version__ = version("ha-sensors-gateway")
except PackageNotFoundError:
    # Source checkouts and the dependency-free image do not install a wheel.
    try:
        with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as metadata:
            __version__ = str(load(metadata)["project"]["version"])
    except FileNotFoundError:
        __version__ = "0+unknown"
