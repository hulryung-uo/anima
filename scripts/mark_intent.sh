#!/usr/bin/env bash
# mark_intent.sh — append an intent label from the shell.
#
# Usage:
#   scripts/mark_intent.sh mining bootstrap
#   echo "광석 → 은행" | scripts/mark_intent.sh -
#
# The running uo_proxy (via `--intent-watch`) picks the line up within
# ~1 second and records it into data/intents/intents-<ts>.jsonl with
# source="file".

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_FILE="${INTENT_WATCH_FILE:-$ROOT/data/intents/input.txt}"

mkdir -p "$(dirname "$WATCH_FILE")"

if [[ $# -eq 0 ]]; then
    echo "usage: $0 <label words...>" >&2
    echo "       echo 'label' | $0 -" >&2
    exit 2
fi

if [[ "$1" == "-" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        printf '%s\n' "$line" >> "$WATCH_FILE"
    done
else
    printf '%s\n' "$*" >> "$WATCH_FILE"
fi
