#!/usr/bin/env bash
# One-time setup on your Mac:
#   1. create a Python virtual environment (.venv) with Python 3.12
#   2. install the packages from requirements.txt into it
#   3. turn on the git pre-commit hook in hooks/
#
# A virtual environment is a private folder of Python packages for this project only,
# so it can't clash with other projects or the system Python (which is 3.7 on this Mac: too old).
#
# If you move or rename this folder, the .venv breaks ("bad interpreter"). Fix:
#   rm -rf .venv && ./setup.sh

set -euo pipefail          # stop on the first error, on unset variables, and on failures inside pipes
cd "$(dirname "$0")"       # run from this script's folder, wherever you call it from

PY=${PY:-python3.12}       # which Python to use; override with  PY=python3.13 ./setup.sh
$PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Tell git to look for hooks in ./hooks (committed to the repo) instead of .git/hooks (not committed).
git config core.hooksPath hooks

echo "Done. Activate with:  source .venv/bin/activate"
