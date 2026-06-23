#!/usr/bin/env python3
"""anima_steer — companion-tier bridge between an external agent (Hermes) and a
running free-play Anima avatar.

This is the PoC seam for the "two-tier brain": the fast tier (planner + 30s
Think loop) keeps running the moment-to-moment game in-process; this CLI lets a
*slow companion agent* observe the character and steer it at the minute scale,
WITHOUT touching the real-time loop.

Two mechanisms, each the simplest for its job:
  * READS  go through ``data/state.json`` — the snapshot StatePublisher writes
    every tick. No connection, no races.
  * WRITES go through the live ``/ws`` WebSocket → CommandBus → Planner — the
    exact channel the web dashboard already uses to steer the agent.

Usage (run inside the anima repo so the venv + relative state path resolve):

    uv run python tools/anima_steer.py state                 # compact summary
    uv run python tools/anima_steer.py state --raw           # full snapshot JSON
    uv run python tools/anima_steer.py set-goal "Walk to the Britain forge and mine, then bank the ingots."
    uv run python tools/anima_steer.py say "Well met, traveler."
    uv run python tools/anima_steer.py go-to 1495 1628
    uv run python tools/anima_steer.py clear-goal

Options: ``--host`` (default 127.0.0.1), ``--port`` (default 8150),
``--state-file`` (default data/state.json).

Exit code is 0 on success, 1 on failure — so a calling agent can branch on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

try:
    import aiohttp
except ImportError:  # pragma: no cover - guidance for a bare interpreter
    sys.stderr.write(
        "aiohttp not found. Run this through the project venv:\n"
        "    uv run python tools/anima_steer.py ...\n"
    )
    sys.exit(1)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8150
DEFAULT_STATE_FILE = "data/state.json"
WS_TIMEOUT = 8.0


# --------------------------------------------------------------------------- #
# READ: state.json → compact, LLM-friendly summary
# --------------------------------------------------------------------------- #

# Notoriety values that mark a nearby mobile as a THREAT, mirroring
# brain/think.py's _HOSTILE_NOTORIETY (3=attackable, 4=criminal, 5=enemy,
# 6=murderer). Surfacing these distinctly keeps the companion from treating a
# red PK like a town baker when it sets a goal.
_HOSTILE_NOTORIETY = {3, 4, 5, 6}


def _load_snapshot(state_file: Path) -> dict:
    if not state_file.exists():
        raise FileNotFoundError(
            f"{state_file} not found. Is a free-play agent running with the web "
            f"server on (default port {DEFAULT_PORT})?"
        )
    return json.loads(state_file.read_text())


def _summarize(snap: dict) -> str:
    st = snap.get("status", {}) or {}
    lines: list[str] = []

    name = st.get("name", "Anima")
    persona = st.get("persona", "?")
    title = st.get("title", "")
    who = f"{name} ({persona})" + (f" — {title}" if title else "")
    lines.append(who)

    age = time.time() - snap.get("ts", 0)
    staleness = f"  [snapshot age: {age:.0f}s]" if age >= 0 else ""
    lines.append(
        f"  pos=({st.get('x')},{st.get('y')},{st.get('z')})  "
        f"hp={st.get('hp')}/{st.get('hp_max')} "
        f"mana={st.get('mana')}/{st.get('mana_max')} "
        f"stam={st.get('stam')}/{st.get('stam_max')}{staleness}"
    )
    lines.append(
        f"  gold={st.get('gold')}  "
        f"weight={st.get('weight')}/{st.get('weight_max')}  "
        f"bank={(st.get('bank_balance') or {}).get('amount', '?')}"
    )

    goal = st.get("goal") or "(none)"
    proc = st.get("procedure") or "(idle)"
    intent = st.get("intent") or ""
    lines.append(f"  current_goal: {goal}")
    lines.append(f"  doing now:    procedure={proc}" + (f"  intent={intent}" if intent else ""))

    # Skills (top by value)
    skills = (snap.get("skills") or {}).get("list", [])
    if skills:
        top = sorted(skills, key=lambda s: s.get("value", 0), reverse=True)[:6]
        sk_str = ", ".join(f"{s.get('id')}={s.get('value')}" for s in top)
        lines.append(f"  top skills:   {sk_str}  (total={snap.get('skills', {}).get('total')})")

    # Nearby — threats first
    nearby = snap.get("nearby", []) or []
    if nearby:
        threats = [m for m in nearby if m.get("notoriety") in _HOSTILE_NOTORIETY]
        friendlies = [m for m in nearby if m.get("notoriety") not in _HOSTILE_NOTORIETY]
        if threats:
            t_str = ", ".join(
                f"{m.get('name')}@({m.get('dx'):+d},{m.get('dy'):+d})" for m in threats
            )
            lines.append(f"  ⚠ THREATS:   {t_str}")
        if friendlies:
            f_str = ", ".join(
                f"{m.get('name')}@({m.get('dx'):+d},{m.get('dy'):+d})" for m in friendlies[:6]
            )
            lines.append(f"  nearby:       {f_str}")
    else:
        lines.append("  nearby:       (no one)")

    # Recent journal — what the character has heard/said lately
    journal = snap.get("journal", []) or []
    if journal:
        lines.append("  recent journal:")
        for e in journal[-8:]:
            speaker = "me" if e.get("is_self") else e.get("name", "?")
            lines.append(f"    [{speaker}] {e.get('text', '')}")

    return "\n".join(lines)


def cmd_state(args: argparse.Namespace) -> int:
    snap = _load_snapshot(Path(args.state_file))
    if args.raw:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
    else:
        print(_summarize(snap))
    return 0


# --------------------------------------------------------------------------- #
# WRITE: send a command over the live /ws WebSocket
# --------------------------------------------------------------------------- #

async def _send_command(host: str, port: int, payload: dict) -> dict:
    url = f"http://{host}:{port}/ws"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, timeout=WS_TIMEOUT) as ws:
            # The server pushes the current snapshot on connect (server.py:84).
            # Drain that first frame so we read the real cmd_result next.
            try:
                await asyncio.wait_for(ws.receive(), timeout=WS_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            await ws.send_str(json.dumps(payload))
            # Read until we get the cmd_result for our command (skip any
            # interleaved snapshot broadcasts).
            deadline = time.time() + WS_TIMEOUT
            while time.time() < deadline:
                msg = await asyncio.wait_for(ws.receive(), timeout=WS_TIMEOUT)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("type") == "cmd_result":
                    return data
            return {"type": "cmd_result", "ok": False, "message": "no cmd_result before timeout"}


def _run_command(args: argparse.Namespace, payload: dict) -> int:
    try:
        result = asyncio.run(_send_command(args.host, args.port, payload))
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        print(
            f"FAILED to reach agent at ws://{args.host}:{args.port}/ws: {e}\n"
            f"Is a free-play agent running with --web-port {args.port}?",
            file=sys.stderr,
        )
        return 1
    ok = result.get("ok", False)
    print(("OK: " if ok else "FAILED: ") + str(result.get("message", "")))
    return 0 if ok else 1


def cmd_set_goal(args: argparse.Namespace) -> int:
    return _run_command(args, {"cmd": "set_goal", "goal": args.goal})


def cmd_clear_goal(args: argparse.Namespace) -> int:
    return _run_command(args, {"cmd": "set_goal", "goal": ""})


def cmd_say(args: argparse.Namespace) -> int:
    return _run_command(args, {"cmd": "say", "text": args.text})


def cmd_go_to(args: argparse.Namespace) -> int:
    return _run_command(args, {"cmd": "go_to", "x": args.x, "y": args.y})


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anima_steer",
        description="Companion-tier bridge: observe and steer a running Anima avatar.",
    )
    p.add_argument("--host", default=DEFAULT_HOST, help="agent web host")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="agent --web-port")
    p.add_argument(
        "--state-file", default=DEFAULT_STATE_FILE,
        help="snapshot file written by StatePublisher (default data/state.json)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("state", help="print a compact summary of the avatar's state")
    sp.add_argument("--raw", action="store_true", help="dump the full snapshot JSON instead")
    sp.set_defaults(func=cmd_state)

    sg = sub.add_parser("set-goal", help="set the high-level goal the Think loop pursues")
    sg.add_argument("goal", help="goal text, e.g. 'Mine at the Britain forge, then bank ingots.'")
    sg.set_defaults(func=cmd_set_goal)

    cg = sub.add_parser("clear-goal", help="clear the current goal")
    cg.set_defaults(func=cmd_clear_goal)

    sy = sub.add_parser("say", help="speak a line in-game, in character")
    sy.add_argument("text", help="what to say")
    sy.set_defaults(func=cmd_say)

    gt = sub.add_parser("go-to", help="force-walk to a map coordinate")
    gt.add_argument("x", type=int)
    gt.add_argument("y", type=int)
    gt.set_defaults(func=cmd_go_to)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
