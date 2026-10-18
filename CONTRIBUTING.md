# Contributing

Use synthetic examples and fixtures only. Never copy data from a real Home Assistant installation into this public repository, including shopping items, entity IDs, store locations, logs, databases, credentials, hostnames, addresses, or personal names.

## Development setup

```bash
uv venv --python 3.14
uv pip install -r requirements_test.txt
npm ci --ignore-scripts
```

Then run the checks with the virtual environment active:

```bash
pytest
ruff check .
ruff format --check .
npm run test:frontend
```

CI enforces an 85% coverage floor for `custom_components/smart_shopping_route` via `pytest --cov=smart_shopping_route --cov-fail-under=85`; run that command before opening a pull request, because a single test file alone will not reach the floor. The tests import the integration modules under a short `smart_shopping_route` package name and stub the AI Task import chain, so they run without a Home Assistant instance or a voice/assist dependency set.

PyArrow is a test/reference dependency only: it exercises real multi-row-group
Parquet files and is not installed by the integration. The shopping card has no
runtime JavaScript dependencies; JSDOM is used only by frontend tests.

For a disposable browser fixture with synthetic items and deterministic providers:

```bash
PYTHONPATH=tests python tests/frontend_fixture.py
# Open http://127.0.0.1:8766/; stop the process when finished.
```

This harness calls the real route entity/coordinator/payload code with an in-memory
source adapter. It never connects to your live HA instance or calls an AI provider.
It does not substitute for a post-install check of HA resource registration.

Refresh public history-store choices at release time with
`scripts/update_store_catalog.py --database <public-base.duckdb>` using a separate
development environment with DuckDB. Export only public store IDs/names, never
household files or mappings.

Before submitting a change:

1. Run `pytest`, `ruff check .`, `ruff format --check .`, and `npm run test:frontend`.
2. Review `git status` and the complete diff.
3. Search for tokens, private keys, email addresses, private hosts/IPs, real entity IDs, and personal data.
4. Confirm tests use mocked AI Tasks and make no paid or external AI requests.
5. Explain user-visible changes in `CHANGELOG.md`.

Keep modules cohesive, async code non-blocking, logs free of item text, and all source mutations keyed by source UID. When you change behaviour that the README describes, update the README in the same change.
