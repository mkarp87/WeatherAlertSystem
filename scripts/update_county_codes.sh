#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_FILE="$ROOT_DIR/docs/CountyCodes.upstream.md"
URL="https://raw.githubusercontent.com/Mason10198/SkywarnPlus/main/CountyCodes.md"

mkdir -p "$ROOT_DIR/docs"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$OUT_FILE"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$OUT_FILE" "$URL"
else
  echo "curl or wget is required" >&2
  exit 1
fi

printf 'Downloaded upstream CountyCodes.md to %s\n' "$OUT_FILE"
