#!/usr/bin/env bash
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  uv run pyrefly check --output-format=github
else
  uv run pyrefly check --summarize-errors
fi
