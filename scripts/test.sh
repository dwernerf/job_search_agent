#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT_DIR/.venv/bin/activate"
PYTHONPATH="$ROOT_DIR/src" pytest -q "$ROOT_DIR/tests"
PYTHONPATH="$ROOT_DIR/src" python -m compileall -q "$ROOT_DIR/src" "$ROOT_DIR/tests"
