# Security Policy

## Reporting Vulnerabilities

Do not open public issues for vulnerabilities or leaked secrets.

Report security concerns to the project maintainers through the repository's
private security advisory flow or another private channel configured by the
maintainers.

## Secret Handling

- Keep secrets in environment variables or local ignored files.
- Never commit real `.env` files, tokens, private keys, credentials, Google Maps
  session state, browser profiles, or production data.
- Use documented examples for configuration only.
- Keep debug dumps, screenshots, and browser artifacts out of committed docs
  unless they are intentionally sanitized fixtures.

## Dependency Policy

This project uses `uv` with dependency cooldowns and locked installs:

- `pyproject.toml` sets `exclude-newer = "7 days"` for `uv` and `uv pip`.
- CI should use `uv sync --locked --dev`.
- Dependency changes should keep `uv.lock` committed and reviewable.

## GitHub Actions

Workflows should use least-privilege permissions, pinned or reviewed action
versions, and `persist-credentials: false` where practical.
