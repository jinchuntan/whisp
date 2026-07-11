#!/usr/bin/env bash
# Run the Persephone worker (or the Agora canary) with LD_LIBRARY_PATH pointed at the
# Agora native SDK. The path is DERIVED from the active Python's site-packages —
# nothing is hardcoded to a specific user or machine.
#
# Usage (from anywhere; the script cd's into worker/):
#   bash scripts/run_worker_agora.sh                 # runs run_worker.py
#   bash scripts/run_worker_agora.sh -m persephone_worker.agora_canary --wav a.wav --live
#
# Override the interpreter with PYTHON=/path/to/python.
set -euo pipefail

cd "$(dirname "$0")/.."   # -> worker/
PY="${PYTHON:-.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "Python interpreter not found at '$PY'. Set PYTHON=... or create .venv." >&2
  exit 1
fi

# Compute <site-packages>/agora/agora_sdk WITHOUT importing agora (importing it
# prints diagnostics and loads native libs before LD_LIBRARY_PATH is set).
AGORA_SDK_DIR="$("$PY" - <<'PY'
import os, sysconfig
purelib = sysconfig.get_paths()["purelib"]
print(os.path.join(purelib, "agora", "agora_sdk"))
PY
)"

if [ -z "$AGORA_SDK_DIR" ] || [ ! -d "$AGORA_SDK_DIR" ]; then
  echo "Agora SDK not found at: $AGORA_SDK_DIR" >&2
  echo "Install it first:  pip install -r requirements-agora.txt" >&2
  exit 1
fi

export LD_LIBRARY_PATH="${AGORA_SDK_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
echo "LD_LIBRARY_PATH -> ${AGORA_SDK_DIR}" >&2

if [ "$#" -eq 0 ]; then
  exec "$PY" run_worker.py
fi
exec "$PY" "$@"
