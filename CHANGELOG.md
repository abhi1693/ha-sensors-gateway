# Changelog

## 0.2.1

Update the Python runtime to 3.14.9 on Alpine 3.24 and refresh coverage 7.17.0, mypy 2.4.0, and Ruff 0.17.2. Runtime dependencies remain standard-library only.

Require native AMD64 and ARM64 pull-request image builds, SBOMs, and strict high/critical vulnerability scans including unfixed findings. Reuse the same isolated HTTP/health smoke before release-image promotion, verify the pinned Python/non-root/pip-free runtime, and expose the source version in the dependency-free image.
