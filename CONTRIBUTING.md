# Contributing

Use synthetic examples and fixtures only. Never copy data from a real Home Assistant installation into this public repository, including shopping items, entity IDs, store locations, logs, databases, credentials, hostnames, addresses, or personal names.

## Development setup

```bash
uv venv --python 3.14
uv pip install -r requirements_test.txt
```

Then run the checks with the virtual environment active:

```bash
pytest
ruff check .
ruff format --check .
```

CI enforces an 85% coverage floor for `custom_components/smart_shopping_route` via `pytest --cov=smart_shopping_route --cov-fail-under=85`; run that command before opening a pull request, because a single test file alone will not reach the floor. The tests import the integration modules under a short `smart_shopping_route` package name and stub the AI Task import chain, so they run without a Home Assistant instance or a voice/assist dependency set.

Before submitting a change:

1. Run `pytest`, `ruff check .`, and `ruff format --check .`.
2. Review `git status` and the complete diff.
3. Search for tokens, private keys, email addresses, private hosts/IPs, real entity IDs, and personal data.
4. Confirm tests use mocked AI Tasks and make no paid or external AI requests.
5. Explain user-visible changes in `CHANGELOG.md`.

Keep modules cohesive, async code non-blocking, logs free of item text, and all source mutations keyed by source UID. When you change behaviour that the README describes, update the README in the same change.
