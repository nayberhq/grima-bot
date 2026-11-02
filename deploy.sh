#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=python3
if [[ -x "$ROOT/.deploy-venv/bin/python" ]]; then
  PYTHON="$ROOT/.deploy-venv/bin/python"
fi
exec "$PYTHON" "$ROOT/deploy/control.py" "$@"
