#!/usr/bin/env bash
# One-time setup: creates .venv with Python 3.12 and installs dependencies.
# If you move or rename this folder, run:  rm -rf .venv && ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-python3.12}
$PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
git config core.hooksPath hooks     # turn on the pre-commit review hook
echo "Done. Activate with:  source .venv/bin/activate"
