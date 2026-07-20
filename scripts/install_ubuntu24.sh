#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3
python3 -m venv "$ROOT_DIR/.venv"
. "$ROOT_DIR/.venv/bin/activate"
pip install -U pip
pip install -e "${ROOT_DIR}[dev]"
playwright install --with-deps chromium
