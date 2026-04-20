#!/usr/bin/env bash
# install_voice_intent.sh — one-shot setup for mark_intent_voice.sh.
#
# Installs whisper-cpp + ffmpeg via Homebrew and downloads a multilingual
# whisper model (~142 MB, works for Korean and English). Idempotent:
# re-running is safe and cheap.

set -u
set -o pipefail

MODEL_DIR="${WHISPER_MODEL_DIR:-$HOME/.whisper-models}"
MODEL_FILE="${WHISPER_MODEL:-$MODEL_DIR/ggml-base.bin}"
MODEL_URL="${WHISPER_MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin}"

msg() { printf '\033[1;34m[install]\033[0m %s\n' "$*" >&2; }

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found — install it first: https://brew.sh" >&2
    exit 1
fi

msg "installing whisper-cpp + ffmpeg via brew (idempotent)…"
brew install whisper-cpp ffmpeg >/dev/null

mkdir -p "$MODEL_DIR"
if [[ ! -f "$MODEL_FILE" ]]; then
    msg "downloading $MODEL_FILE (~142 MB)…"
    curl -L --fail -o "$MODEL_FILE" "$MODEL_URL"
else
    msg "model already present: $MODEL_FILE"
fi

msg "done. Test:  scripts/mark_intent_voice.sh"
msg "model path: $MODEL_FILE"
msg ""
msg "Larger models (better accuracy, slower on old Macs):"
msg "  ggml-small.bin  (~466 MB) — recommended for mixed Korean/English"
msg "  ggml-medium.bin (~1.5 GB)"
msg "  Download: curl -L -o \$HOME/.whisper-models/ggml-small.bin \\"
msg "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
msg "Then:  WHISPER_MODEL=\$HOME/.whisper-models/ggml-small.bin scripts/mark_intent_voice.sh"
