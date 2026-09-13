#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
exec "$PY" -m bmo --dev --mock-cloud --wav tests/fixtures/user_question.wav --camera-fixtures tests/fixtures "$@"
