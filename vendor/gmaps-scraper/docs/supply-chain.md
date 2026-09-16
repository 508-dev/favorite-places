# Dependency Supply-Chain Policy

This project is a Python package managed with `uv`.

## uv

Dependency resolution uses a seven-day cooldown:

```toml
[tool.uv]
exclude-newer = "7 days"

[tool.uv.pip]
exclude-newer = "7 days"
```

Keep `uv.lock` committed. CI and release workflows should install with:

```bash
uv sync --locked --dev
```

## Runtime Dependencies

Treat dependency changes as executable-code changes. Before adding or upgrading
packages, review the package purpose, release recency, lockfile diff, and any
native or browser automation surface area.

## Local Artifacts

Do not commit browser profiles, session directories, raw Google Maps payload
dumps, LLM cache files, learned translation-memory files, or screenshots unless
they are intentionally sanitized fixtures.
