#!/usr/bin/env bash
set -euo pipefail

uv run python -m unittest discover -s tests
