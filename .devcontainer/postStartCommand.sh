#!/bin/bash

set -u

# Keep Python dependencies in sync when the container starts.
uv sync --locked

# Load .env if present. Values from the container environment still take precedence.
if [[ -f .env ]]; then
	set -a
	# shellcheck disable=SC1091
	source .env
	set +a
fi
