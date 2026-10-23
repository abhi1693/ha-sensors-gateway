# Contributing

Contributions that keep the gateway small, auditable, and dependency-free are
welcome.

1. Fork the repository and create a focused branch.
2. Never add real webhook IDs, URLs, locations, or request bodies to fixtures.
3. Run the checks below.
4. Open a pull request explaining the security impact of the change.

```sh
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
mypy --strict src
PYTHONPATH=src coverage run -m unittest discover -s tests -v
coverage report
docker build --build-arg VERSION=v0.2.1 -t ha-sensors-gateway:test .
bash scripts/smoke-runtime.sh ha-sensors-gateway:test "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
```

Changes that broaden the command allowlist require tests proving why they cannot
control Home Assistant or disclose unrelated data.

The runtime smoke starts the real gateway against a local, read-only fixture
inside a container with external networking disabled. Never replace that fixture
with a real Home Assistant origin or capability. CI runs it independently on
native AMD64 and ARM64 after the shared SBOM and strict vulnerability scan;
unfixed high and critical findings block image promotion too.
