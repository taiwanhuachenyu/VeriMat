#!/usr/bin/env bash
# Create an isolated environment and install pinned development dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
VERIMAT_VENV_DIR="${VERIMAT_VENV_DIR:-.venv}"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"VeriMat requires Python 3.11+; found {sys.version.split()[0]}. "
        "Set PYTHON_BIN to a supported interpreter."
    )
PY

"$PYTHON_BIN" -m venv "$VERIMAT_VENV_DIR"
"$VERIMAT_VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VERIMAT_VENV_DIR/bin/python" -m pip install -r requirements-dev.lock

echo "INSTALL COMPLETE: $VERIMAT_VENV_DIR"
echo "Activate with: source $VERIMAT_VENV_DIR/bin/activate"
