#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/models"
URL="https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
OUT="$ROOT/models/hand_landmarker.task"
if [[ -s "$OUT" ]]; then
  echo "MediaPipe model already present: $OUT"
  exit 0
fi
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
curl --fail --location --retry 3 --output "$OUT.tmp" "$URL"
mv "$OUT.tmp" "$OUT"
echo "Downloaded: $OUT"
