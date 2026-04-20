#!/usr/bin/env bash
# mark_intent_voice.sh — record a short voice note, transcribe with
# whisper-cpp, append the resulting text to data/intents/input.txt.
#
# Usage:
#   scripts/mark_intent_voice.sh              # records 5s, uses default model
#   DURATION=8 scripts/mark_intent_voice.sh   # longer window
#   WHISPER_LANG=ko scripts/mark_intent_voice.sh
#
# Requires whisper-cpp + ffmpeg (install with scripts/install_voice_intent.sh).
# Bind to a macOS global hotkey (Raycast / Shortcuts / Hammerspoon) for
# one-tap voice labelling while ClassicUO has focus.

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_FILE="${INTENT_WATCH_FILE:-$ROOT/data/intents/input.txt}"
DURATION="${DURATION:-5}"
MODEL_DIR="${WHISPER_MODEL_DIR:-$HOME/.whisper-models}"
MODEL="${WHISPER_MODEL:-$MODEL_DIR/ggml-base.bin}"
LANG="${WHISPER_LANG:-auto}"
# macOS avfoundation audio device index. `ffmpeg -f avfoundation -list_devices true -i ""`
# to list. Default ":0" = first audio input.
AUDIO_DEVICE="${AUDIO_DEVICE:-:0}"

msg() { printf '\033[1;34m[voice]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[voice]\033[0m %s\n' "$*" >&2; }

# Resolve whisper binary — packages ship as either `whisper-cpp` (older)
# or `whisper-cli` (recent) depending on brew version.
WHISPER_BIN=""
for cand in whisper-cpp whisper-cli; do
    if command -v "$cand" >/dev/null 2>&1; then
        WHISPER_BIN="$cand"
        break
    fi
done
if [[ -z "$WHISPER_BIN" ]]; then
    err "whisper-cpp not installed — run scripts/install_voice_intent.sh"
    exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    err "ffmpeg not installed — run scripts/install_voice_intent.sh"
    exit 1
fi
if [[ ! -f "$MODEL" ]]; then
    err "model missing: $MODEL — run scripts/install_voice_intent.sh"
    exit 1
fi

mkdir -p "$(dirname "$WATCH_FILE")"

TMPDIR_OUT="$(mktemp -d)"
WAV="$TMPDIR_OUT/rec.wav"
trap 'rm -rf "$TMPDIR_OUT"' EXIT

# Start beep (ignore missing system sounds silently).
afplay /System/Library/Sounds/Pop.aiff >/dev/null 2>&1 &

msg "recording ${DURATION}s from $AUDIO_DEVICE…"
if ! ffmpeg -y -hide_banner -loglevel error \
        -f avfoundation -i "$AUDIO_DEVICE" \
        -t "$DURATION" -ac 1 -ar 16000 "$WAV" 2>/tmp/ffmpeg_voice.err; then
    err "ffmpeg recording failed"
    cat /tmp/ffmpeg_voice.err >&2 || true
    exit 1
fi

# End beep.
afplay /System/Library/Sounds/Tink.aiff >/dev/null 2>&1 &

msg "transcribing…"
"$WHISPER_BIN" -m "$MODEL" -l "$LANG" \
    -otxt -of "$TMPDIR_OUT/rec" -nt "$WAV" >/dev/null 2>&1 || true

if [[ ! -f "$TMPDIR_OUT/rec.txt" ]]; then
    err "no transcript produced — mic silent or whisper failed"
    exit 1
fi

TEXT=$(tr -s '[:space:]' ' ' < "$TMPDIR_OUT/rec.txt" \
        | sed -e 's/^ *//' -e 's/ *$//')

if [[ -z "$TEXT" ]]; then
    msg "(silent — nothing appended)"
    exit 0
fi

printf '%s\n' "$TEXT" >> "$WATCH_FILE"
msg "appended: $TEXT"
osascript -e "display notification \"$TEXT\" with title \"Intent logged\"" >/dev/null 2>&1 &
