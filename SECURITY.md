# Security and privacy

Report vulnerabilities privately through GitHub's security advisory feature once the repository is published. Do not open a public issue containing secrets or private infrastructure data.

Never include Home Assistant long-lived access tokens, AI provider keys, `secrets.yaml`, `.storage` contents, full configuration archives, databases, or logs that have not been reviewed. Redact addresses, personal data, entity IDs, hostnames, private URLs/IPs, item names, and store/profile details unless a minimal synthetic value is necessary to reproduce the problem.

If a secret was committed, revoke it immediately. Removing it from the latest commit does not remove it from Git history.
