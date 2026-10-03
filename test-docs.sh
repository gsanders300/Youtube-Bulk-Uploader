#!/usr/bin/env bash

# Backward-compatible documentation check for Unix systems.
# The pytest suite runs the same CLI checks on all supported systems.
set -euo pipefail

uv sync --locked --no-default-groups --group test

uv run --no-sync youtube-uploader --help >/dev/null
uv run --no-sync youtube-uploader --version >/dev/null

for command in auth doctor exiftool scan sync-dates upload; do
    uv run --no-sync youtube-uploader "$command" --help >/dev/null
done

echo "All documented CLI commands are available."
