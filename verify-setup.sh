#!/usr/bin/env bash

# Backward-compatible Unix wrapper. The diagnostic implementation itself is
# Python-based and is also available directly on Windows.
set -euo pipefail
exec uv run --locked youtube-uploader doctor "$@"
