#!/usr/bin/env bash
set -euo pipefail

sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e '.[dev]'
playwright install --with-deps chromium
