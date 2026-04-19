#!/usr/bin/env bash
# mark_intent_dialog.sh — macOS popup dialog for intent input.
#
# Shows a small text-entry dialog, then appends the typed line to
# data/intents/input.txt. The dialog defaults focus to the input field
# so you can type immediately; Enter submits, Esc cancels.
#
# Ideal usage: bind to a macOS global hotkey via
#   - System Settings → Keyboard → Keyboard Shortcuts → Services, or
#   - Raycast / BetterTouchTool / Hammerspoon / Shortcuts.app, or
#   - Apple Script: `osascript -e 'do shell script ".../mark_intent_dialog.sh"'`
#
# The previous label becomes the default answer on the next invocation
# so repeating a short label is 1 keystroke.

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_FILE="${INTENT_WATCH_FILE:-$ROOT/data/intents/input.txt}"
LAST_FILE="$ROOT/data/intents/.last_label"

mkdir -p "$(dirname "$WATCH_FILE")"

DEFAULT=""
if [[ -f "$LAST_FILE" ]]; then
    DEFAULT="$(head -n1 "$LAST_FILE")"
fi

LABEL=$(osascript <<APPLESCRIPT
try
    tell application "System Events"
        activate
        set resp to display dialog "Intent:" default answer "$DEFAULT" ¬
            with title "Anima demo" buttons {"Cancel", "Log"} default button "Log" ¬
            giving up after 30
        if button returned of resp is "Log" then
            return text returned of resp
        else
            return ""
        end if
    end tell
on error
    return ""
end try
APPLESCRIPT
)

LABEL="${LABEL// /}"       # trim surrounding whitespace cheaply
LABEL=$(echo "$LABEL" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

if [[ -z "$LABEL" ]]; then
    exit 0
fi

printf '%s\n' "$LABEL" >> "$WATCH_FILE"
printf '%s\n' "$LABEL" > "$LAST_FILE"
