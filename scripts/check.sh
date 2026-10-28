#!/usr/bin/env sh
# Definition-of-done gate: stops at the first failing check.
set -e
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
