#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT_DIR/.venv/bin/activate"
JOBAGENT_CONFIG="${JOBAGENT_CONFIG:-$ROOT_DIR/config/config.yaml}" python -m jobagent
