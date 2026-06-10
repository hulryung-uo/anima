#!/usr/bin/env bash
#
# spectate_classicuo.sh — launch ClassicUO as the foundrygm GameMaster to
# watch a live foundry eval agent on the local ServUO shard.
#
# Connects DIRECTLY to the shard (no uo_proxy) so the eval's packet capture
# is untouched. Never log in with the eval account itself — a duplicate
# login kicks the agent's connection and ruins the eval window.
#
# In-game, once logged in:
#   [hide                stay invisible so the agent's perception
#                        (player_presence / sociability) is not perturbed
#   [who → click player  opens the client gump → "Go to them"
#                        ([admin needs Administrator; foundrygm is GameMaster)
#
# Usage:
#   ./scripts/spectate_classicuo.sh                # foundrygm @ 127.0.0.1:2594
#   HOST=127.0.0.1 PORT=2594 CUO_USER=foundrygm ./scripts/spectate_classicuo.sh

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLASSICUO_DIR="${CLASSICUO_DIR:-$ROOT/../classicuo}"
CUO_BIN="${CUO_BIN:-$CLASSICUO_DIR/bin/Release/net10.0/cuo}"
UOPATH="${UOPATH:-$HOME/dev/uo/uo-resource}"
CLIENT_VERSION="${CLIENT_VERSION:-7.0.102.3}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-2594}"
# foundrygm is the GameMaster account provisioned by foundry.kernel.provision
# (credentials defined in foundry/kernel/gm.py — local dev shard only).
CUO_USER="${CUO_USER:-foundrygm}"
CUO_PASS="${CUO_PASS:-foundry-gm-pass}"

TS="$(date +%Y%m%d-%H%M%S)"
CUO_LOG="${CUO_LOG:-/tmp/classicuo-spectate-$TS.log}"

msg() { printf '\033[1;34m[spectate]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[spectate]\033[0m %s\n' "$*" >&2; }

if [[ ! -x "$CUO_BIN" ]]; then
    err "ClassicUO binary not found: $CUO_BIN (build it or override CUO_BIN)"
    exit 1
fi
if [[ ! -d "$UOPATH" ]]; then
    err "UO data directory missing: $UOPATH (override with UOPATH=...)"
    exit 1
fi
if ! nc -z "$HOST" "$PORT" 2>/dev/null; then
    err "shard not reachable at $HOST:$PORT — is ServUO running?"
    err "  cd ~/dev/uo/servuo && MONO_GAC_PREFIX=/opt/homebrew mono ServUO.exe -noconsole"
    exit 1
fi
case "$CUO_USER" in
    evo*|eval*)
        err "refusing to log in as '$CUO_USER' — duplicate login would kick the"
        err "agent mid-eval. Use the foundrygm observer account instead."
        exit 1
        ;;
esac

export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
export PATH="$DOTNET_ROOT:$PATH"

msg "shard      : $HOST:$PORT"
msg "account    : $CUO_USER (GameMaster observer)"
msg "log        : $CUO_LOG"
msg ""
msg "once in game:  [hide                → stay invisible to the agent"
msg "               [who → click player  → 'Go to them' to jump to the evo* char"
msg ""

cd "$(dirname "$CUO_BIN")"
exec "./$(basename "$CUO_BIN")" \
    -language ENU \
    -uopath "$UOPATH" \
    -clientversion "$CLIENT_VERSION" \
    -ip "$HOST" \
    -port "$PORT" \
    -username "$CUO_USER" \
    -password "$CUO_PASS" \
    -autologin true \
    -skiploginscreen \
    -plugins "" \
    -encryption 0 \
    >"$CUO_LOG" 2>&1
