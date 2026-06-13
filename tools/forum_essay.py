"""Post an in-world essay to the UO Tavern forum as the Anima agent.

The agents live in Britannia; once in a while one of them writes down what
the work has been teaching them. This posts such an essay to the tavern
board. Reads the API key from config.yaml (never hard-code it).

    uv run python tools/forum_essay.py --title "..." --body-file essay.md
    uv run python tools/forum_essay.py --title "..." --body "..." --board tavern

Boards: tavern (tales/roleplay), trade, qa, library (guides).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp
import yaml

REPO = Path(__file__).resolve().parents[1]


def _load_forum_cfg() -> tuple[str, str]:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    f = cfg.get("forum", {})
    base = f.get("base_url", "https://www.uotavern.com/api").rstrip("/")
    key = f.get("api_key", "")
    if not key:
        sys.exit("no forum.api_key in config.yaml")
    return base, key


async def post_essay(title: str, body: str, board: str) -> int:
    base, key = _load_forum_cfg()
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    payload = {"board": board, "title": title, "content": body}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/agent/posts", json=payload,
                          headers=headers, timeout=30) as r:
            text = await r.text()
            print(f"status={r.status}")
            print(text[:400])
            return r.status


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Post an in-world essay to UO Tavern")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body")
    ap.add_argument("--body-file", help="read body markdown from this file")
    ap.add_argument("--board", default="tavern")
    args = ap.parse_args(argv)

    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text()
    if not body:
        sys.exit("provide --body or --body-file")

    status = asyncio.run(post_essay(args.title, body, args.board))
    return 0 if status == 201 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
