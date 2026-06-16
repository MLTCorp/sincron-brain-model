# Security Policy

## Supported Versions

`sincron-brain-model` is currently pre-1.0. Security fixes are expected on the
latest released version only until the project reaches a stable support policy.

## Reporting a Vulnerability

Do not open public issues for suspected vulnerabilities.

Send a private report to:

- `contato@sincron.digital`

Please include:

- affected version or commit;
- operating system and Python version;
- reproduction steps;
- whether memory content, credentials, or local files can be exposed or changed;
- any relevant logs with secrets removed.

The project treats memory vault contents, drafts, reactivation events, audit
logs, API keys, and local MCP configuration as sensitive data.

## Security Posture

- The vault is local-first and stores memories as Markdown plus a rebuildable
  SQLite index.
- Audit logs redact common sensitive keys before writing.
- Generated viewers omit full memory bodies by default.
- Release workflows use PyPI Trusted Publishing instead of long-lived PyPI
  tokens.
- Distribution artifacts are built in GitHub Actions, checked, checksummed, and
  attested before publication.
