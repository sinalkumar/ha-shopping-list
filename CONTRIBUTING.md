# Contributing

Use synthetic examples and fixtures only. Never copy data from a real Home Assistant installation into this public repository, including shopping items, entity IDs, store locations, logs, databases, credentials, hostnames, addresses, or personal names.

Before submitting a change:

1. Run `pytest`, `ruff check .`, and `ruff format --check .`.
2. Review `git status` and the complete diff.
3. Search for tokens, private keys, email addresses, private hosts/IPs, real entity IDs, and personal data.
4. Confirm tests use mocked AI Tasks and make no paid or external AI requests.
5. Explain user-visible changes in `CHANGELOG.md`.

Keep modules cohesive, async code non-blocking, logs free of item text, and all source mutations keyed by source UID.
