#!/usr/bin/env bash
# ask_intent_loop.sh — periodically prompt the player for the current intent.
#
# Every $INTERVAL seconds (default 300 = 5 min), if the intent log file
# has not been updated within that window, play a chime, show a macOS
# notification "지금 뭐 하려고?", and invoke scripts/mark_intent_voice.sh.
# Recent manual labels (whether via chat, shell, hotkey, or voice) reset
# the clock so the script doesn't nag when you're already narrating.
#
# Usage:
#   scripts/ask_intent_loop.sh                  # 5 min cadence
#   INTERVAL=180 scripts/ask_intent_loop.sh     # every 3 min
#   QUESTION="What's next?" scripts/ask_intent_loop.sh
#
# Background-friendly — intended to be launched by demo_with_classicuo.sh
# and killed on Ctrl-C via the parent's trap.

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_FILE="${INTENT_WATCH_FILE:-$ROOT/data/intents/input.txt}"
INTERVAL="${INTERVAL:-300}"
QUESTION="${QUESTION:-지금 뭐 하려고?}"
AUTO_RECORD="${AUTO_RECORD:-1}"   # 1 = auto-invoke voice capture. 0 = notify only.

msg() { printf '\033[1;34m[asker]\033[0m %s\n' "$*" >&2; }

mkdir -p "$(dirname "$WATCH_FILE")"
touch "$WATCH_FILE"

last_mtime() {
    # macOS: stat -f %m. Returns epoch seconds.
    stat -f %m "$WATCH_FILE" 2>/dev/null || echo 0
}

msg "active (interval=${INTERVAL}s, auto_record=$AUTO_RECORD)"
while true; do
    sleep "$INTERVAL"

    now=$(date +%s)
    m=$(last_mtime)
    if (( now - m < INTERVAL )); then
        # User has logged an intent recently — stay quiet this round.
        continue
    fi

    msg "prompting: $QUESTION"
    afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1 &
    osascript -e "display notification \"$QUESTION\" with title \"Anima demo\"" \
        >/dev/null 2>&1 &

    if [[ "$AUTO_RECORD" == "1" ]]; then
        # Small lead time so the user can react before recording starts.
        sleep 2
        "$ROOT/scripts/mark_intent_voice.sh" || msg "voice capture failed (ok)"
    fi
done
