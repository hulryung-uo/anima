#!/usr/bin/env bash
#
# demo_with_classicuo.sh — launch uo_proxy + ClassicUO for demo trajectory capture.
#
# Flow:
#   1. Start `python -m uo_proxy` in background, listening on 127.0.0.1:2593,
#      forwarding to uo.hulryung.com:2593.
#   2. Wait until the listener is accepting.
#   3. Launch ClassicUO with -ip 127.0.0.1 -port 2593 so it talks to the proxy.
#   4. On Ctrl-C / exit, terminate both cleanly.
#
# Outputs:
#   data/trajectories/demo-<ts>.jsonl   — framed packet log
#   data/intents/intents-<ts>.jsonl     — //-prefixed chat labels
#   /tmp/uo_proxy-<ts>.log              — proxy stderr
#   /tmp/classicuo-<ts>.log             — ClassicUO stdout/stderr
#
# Requirements:
#   - ClassicUO checkout at $CLASSICUO_DIR (default ../classicuo)
#     built to bin/Release/net10.0/cuo (run `scripts/build.sh` in ClassicUO first).
#   - UO data files at $UOPATH (default /Users/dkkang/dev/uo/uo-resource).
#   - uv + Python env for uo_proxy.
#
# Usage:
#   ./scripts/demo_with_classicuo.sh                 # default server (uo.hulryung.com)
#   UPSTREAM=other.server:2593 ./scripts/demo_with_classicuo.sh
#   INTENT_PREFIX="[i " CUO_USER=admin ./scripts/demo_with_classicuo.sh

set -u
set -o pipefail

# ---------------------------------------------------------------------- config

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLASSICUO_DIR="${CLASSICUO_DIR:-$ROOT/../classicuo}"
CUO_BIN="${CUO_BIN:-$CLASSICUO_DIR/bin/Release/net10.0/cuo}"
UOPATH="${UOPATH:-/Users/dkkang/dev/uo/uo-resource}"
CLIENT_VERSION="${CLIENT_VERSION:-7.0.102.3}"

UPSTREAM="${UPSTREAM:-uo.hulryung.com:2593}"
LISTEN="${LISTEN:-127.0.0.1:2593}"
ADVERTISE="${ADVERTISE:-$LISTEN}"
# Default prefix is "[i " — ClassicUO doesn't intercept `[`, and
# ServUO/RunUO-style shards reject unknown `[foo` commands privately so
# other players don't see the line. Override with INTENT_PREFIX=... if
# your shard handles `[` differently.
INTENT_PREFIX="${INTENT_PREFIX:-[i }"

# Credentials: load from a git-ignored env file so the password never
# enters the repo. `CREDS_FILE` can be overridden; defaults to
# scripts/credentials.env. Start from the committed example:
#   cp scripts/credentials.env.example scripts/credentials.env
# Precedence: explicit shell env var > credentials.env > (no default).
CREDS_FILE="${CREDS_FILE:-$ROOT/scripts/credentials.env}"
__shell_cuo_user="${CUO_USER:-}"
__shell_cuo_pass="${CUO_PASS:-}"
if [[ -f "$CREDS_FILE" ]]; then
    # shellcheck disable=SC1090
    . "$CREDS_FILE"
fi
[[ -n "$__shell_cuo_user" ]] && CUO_USER="$__shell_cuo_user"
[[ -n "$__shell_cuo_pass" ]] && CUO_PASS="$__shell_cuo_pass"

if [[ -z "${CUO_USER:-}" || -z "${CUO_PASS:-}" ]]; then
    echo "[demo] CUO_USER and CUO_PASS must be set (shell env or $CREDS_FILE)." >&2
    echo "       cp scripts/credentials.env.example $CREDS_FILE   # then edit" >&2
    exit 1
fi

TS="$(date +%Y%m%d-%H%M%S)"
PROXY_LOG="/tmp/uo_proxy-$TS.log"
CUO_LOG="/tmp/classicuo-$TS.log"
TRAJ_OUT="$ROOT/data/trajectories/demo-$TS.jsonl"
INTENT_OUT="$ROOT/data/intents/intents-$TS.jsonl"

PROXY_PID=""
CUO_PID=""
ASKER_PID=""

# ASK_INTERVAL=0 disables the periodic voice prompt. Non-zero seconds
# enables it (5 min = 300 is a reasonable default). The asker calls
# scripts/mark_intent_voice.sh, which requires whisper-cpp + ffmpeg.
ASK_INTERVAL="${ASK_INTERVAL:-0}"

# ---------------------------------------------------------------------- helpers

msg() { printf '\033[1;34m[demo]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[demo]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[demo]\033[0m %s\n' "$*" >&2; }

cleanup() {
    local rc=$?
    msg "cleanup (exit=$rc)…"
    if [[ -n "$ASKER_PID" ]] && kill -0 "$ASKER_PID" 2>/dev/null; then
        msg "stopping asker (pid=$ASKER_PID)"
        kill "$ASKER_PID" 2>/dev/null || true
    fi
    if [[ -n "$CUO_PID" ]] && kill -0 "$CUO_PID" 2>/dev/null; then
        msg "stopping ClassicUO (pid=$CUO_PID)"
        kill "$CUO_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$CUO_PID" 2>/dev/null || true
    fi
    if [[ -n "$PROXY_PID" ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
        msg "stopping uo_proxy (pid=$PROXY_PID)"
        kill "$PROXY_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$PROXY_PID" 2>/dev/null || true
    fi
    msg "logs:"
    msg "  proxy  : $PROXY_LOG"
    msg "  cuo    : $CUO_LOG"
    msg "  packets: $TRAJ_OUT"
    msg "  intents: $INTENT_OUT"
    exit "$rc"
}
trap cleanup INT TERM EXIT

# ---------------------------------------------------------------------- checks

if [[ ! -x "$CUO_BIN" ]]; then
    err "ClassicUO binary not found or not executable: $CUO_BIN"
    err "Build ClassicUO first (see $CLASSICUO_DIR/README.md), or override CUO_BIN."
    exit 1
fi
if [[ ! -d "$UOPATH" ]]; then
    err "UO data directory missing: $UOPATH"
    err "Override with UOPATH=... if your install is elsewhere."
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    err "'uv' not on PATH — install it or adapt this script to your Python runner."
    exit 1
fi

# Parse listen host:port for later port-check
LISTEN_HOST="${LISTEN%%:*}"
LISTEN_PORT="${LISTEN##*:}"

if nc -z "$LISTEN_HOST" "$LISTEN_PORT" 2>/dev/null; then
    err "Port $LISTEN already accepting connections — another proxy or service is using it."
    err "Kill it or pass LISTEN=127.0.0.1:<other-port>."
    exit 1
fi

mkdir -p "$(dirname "$TRAJ_OUT")" "$(dirname "$INTENT_OUT")"

# ---------------------------------------------------------------------- launch proxy

msg "starting uo_proxy  upstream=$UPSTREAM listen=$LISTEN  advertise=$ADVERTISE"
(
    cd "$ROOT"
    exec uv run python -m uo_proxy \
        --upstream "$UPSTREAM" \
        --listen "$LISTEN" \
        --advertise "$ADVERTISE" \
        --out "$TRAJ_OUT" \
        --intent-out "$INTENT_OUT" \
        --intent-prefix "$INTENT_PREFIX"
) >"$PROXY_LOG" 2>&1 &
PROXY_PID=$!

# Wait up to 10 seconds for proxy to listen
for i in $(seq 1 50); do
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        err "uo_proxy died during startup — see $PROXY_LOG"
        tail -20 "$PROXY_LOG" >&2 || true
        exit 1
    fi
    if nc -z "$LISTEN_HOST" "$LISTEN_PORT" 2>/dev/null; then
        msg "proxy ready on $LISTEN (pid=$PROXY_PID)"
        break
    fi
    sleep 0.2
done
if ! nc -z "$LISTEN_HOST" "$LISTEN_PORT" 2>/dev/null; then
    err "proxy did not start listening in time — see $PROXY_LOG"
    tail -20 "$PROXY_LOG" >&2 || true
    exit 1
fi

# ---------------------------------------------------------------------- launch ClassicUO

msg "starting ClassicUO  bin=$CUO_BIN"
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export PATH="$HOME/.dotnet:$PATH"

(
    cd "$(dirname "$CUO_BIN")"
    exec "./$(basename "$CUO_BIN")" \
        -language ENU \
        -uopath "$UOPATH" \
        -clientversion "$CLIENT_VERSION" \
        -ip "$LISTEN_HOST" \
        -port "$LISTEN_PORT" \
        -username "$CUO_USER" \
        -password "$CUO_PASS" \
        -autologin true \
        -plugins "" \
        -encryption 0
) >"$CUO_LOG" 2>&1 &
CUO_PID=$!

msg "ClassicUO pid=$CUO_PID  — Ctrl-C here to stop everything"
msg ""
msg "intent inputs:"
msg "  (A) in-game chat: type '${INTENT_PREFIX}<label>'  (server rejects, usually private)"
msg "  (B) shell:        scripts/mark_intent.sh <label>  (private, recommended)"
msg "  (C) macOS hotkey: bind scripts/mark_intent_dialog.sh to a shortcut"
msg "  (D) voice:        scripts/mark_intent_voice.sh  (needs whisper-cpp)"
if [[ "$ASK_INTERVAL" -gt 0 ]]; then
    msg "starting asker — will prompt for voice intent every ${ASK_INTERVAL}s of silence"
    ( exec "$ROOT/scripts/ask_intent_loop.sh" ) \
        </dev/null >>"$PROXY_LOG" 2>&1 &
    ASKER_PID=$!
else
    msg "  (periodic asker disabled — set ASK_INTERVAL=300 to enable 5-min voice prompts)"
fi
msg ""
msg "tail proxy log:  tail -f $PROXY_LOG"
msg "tail cuo log:    tail -f $CUO_LOG"
msg "watch intents:   tail -f $INTENT_OUT"

# Wait for ClassicUO. If it exits, stop proxy. If proxy dies, stop cuo.
while true; do
    if ! kill -0 "$CUO_PID" 2>/dev/null; then
        msg "ClassicUO exited"
        break
    fi
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        warn "uo_proxy died while ClassicUO still running — see $PROXY_LOG"
        break
    fi
    sleep 1
done
