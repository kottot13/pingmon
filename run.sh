#!/usr/bin/env bash
# Launch pingmon from the local venv.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q "textual>=0.80" "pyte>=0.8"
fi

exec .venv/bin/python -m pingmon "$@"
