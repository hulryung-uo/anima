"""CLI entry: `uv run python -m uo_proxy --upstream host:port [...]`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .intent import DEFAULT_PREFIX, IntentLogger
from .logger import ProxyLogger
from .proxy import ProxyConfig, serve


def _parse_hostport(s: str, default_port: int) -> tuple[str, int]:
    if ":" in s:
        host, port = s.rsplit(":", 1)
        return host, int(port)
    return s, default_port


def main() -> None:
    p = argparse.ArgumentParser(
        prog="uo_proxy",
        description="UO MITM proxy — capture ClassicUO ↔ server traffic to JSONL.",
    )
    p.add_argument(
        "--upstream",
        required=True,
        help="Real UO server host[:port], e.g. 'login.example.com:2593'",
    )
    p.add_argument(
        "--listen",
        default="127.0.0.1:2593",
        help="Local bind address (default 127.0.0.1:2593)",
    )
    p.add_argument(
        "--advertise",
        default=None,
        help="Host[:port] to advertise in 0x8C rewrites (default = --listen)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="JSONL output path (default data/trajectories/demo-<timestamp>.jsonl)",
    )
    p.add_argument(
        "--intent-out",
        default=None,
        help="Intent JSONL output path (default data/intents/intents-<timestamp>.jsonl)",
    )
    p.add_argument(
        "--intent-prefix",
        default=DEFAULT_PREFIX,
        help=f"Chat prefix for intent labels (default '{DEFAULT_PREFIX}'). "
             "Empty string disables chat-intent capture.",
    )
    p.add_argument(
        "--intent-watch",
        default=None,
        help="Path to a text file; any line appended becomes an intent "
             "label (default data/intents/input.txt). Pass '' to disable.",
    )
    args = p.parse_args()

    upstream_host, upstream_port = _parse_hostport(args.upstream, 2593)
    listen_host, listen_port = _parse_hostport(args.listen, 2593)
    if args.advertise:
        adv_host, adv_port = _parse_hostport(args.advertise, listen_port)
    else:
        adv_host, adv_port = listen_host, listen_port

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path("data/trajectories") / f"demo-{ts}.jsonl"
    if args.intent_out:
        intent_path = Path(args.intent_out)
    else:
        intent_path = Path("data/intents") / f"intents-{ts}.jsonl"
    # --intent-watch default: data/intents/input.txt unless explicitly "".
    if args.intent_watch is None:
        watch_path: str | None = str(Path("data/intents") / "input.txt")
    elif args.intent_watch == "":
        watch_path = None
    else:
        watch_path = args.intent_watch

    config = ProxyConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        advertised_host=adv_host,
        advertised_port=adv_port,
        intent_prefix=args.intent_prefix,
    )
    logger = ProxyLogger(out_path)
    intent_logger = IntentLogger(intent_path) if args.intent_prefix else None

    print(
        f"[uo_proxy] listen={listen_host}:{listen_port}  "
        f"upstream={upstream_host}:{upstream_port}  "
        f"advertise={adv_host}:{adv_port}  "
        f"log={out_path}  "
        f"intent_log={intent_path if intent_logger else 'disabled'}  "
        f"intent_prefix={args.intent_prefix!r}  "
        f"intent_watch={watch_path or 'disabled'}",
        file=sys.stderr,
        flush=True,
    )
    try:
        asyncio.run(serve(
            config, logger,
            intent_logger=intent_logger,
            intent_watch_path=watch_path,
        ))
    except KeyboardInterrupt:
        print("[uo_proxy] shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
