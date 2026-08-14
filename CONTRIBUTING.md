# Contributing

Contributions that keep the gateway small, auditable, and dependency-free are
welcome.

1. Fork the repository and create a focused branch.
2. Never add real webhook IDs, URLs, locations, or request bodies to fixtures.
3. Run the checks below.
4. Open a pull request explaining the security impact of the change.

```sh
python -m pip install ruff==0.15.22
ruff check .
ruff format --check .
PYTHONPATH=src python -m unittest discover -s tests -v
docker build -t ha-sensors-gateway:test .
```

Changes that broaden the command allowlist require tests proving why they cannot
control Home Assistant or disclose unrelated data.
