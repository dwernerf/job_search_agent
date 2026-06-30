#!/usr/bin/env bash
set -euo pipefail
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m compileall -q src tests
