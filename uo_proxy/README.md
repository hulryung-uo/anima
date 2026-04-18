# uo_proxy

MITM proxy that captures ClassicUO ↔ UO-server traffic as JSONL trajectories,
without modifying ClassicUO.

## What it does

- Listens on a local port and forwards bytes to the real UO server.
- Rewrites the login-server `0x8C` **ServerRedirect** packet so ClassicUO
  reconnects back to the proxy for the game-phase connection.
- Logs every framed packet in both directions to
  `data/trajectories/demo-<timestamp>.jsonl` (one packet per line).
- On the server→client side, decompresses Huffman *for logging only* — the
  bytes sent to ClassicUO are forwarded raw, unchanged.
- Reuses `anima.client` codec, so the packet schema matches what the agent
  already produces.

## Why this approach

- **No ClassicUO code change.** Point the client at `127.0.0.1:<port>`
  (or edit your `/etc/hosts`) — nothing else.
- **No `tcpdump` / sudo.** Pure user-space Python sockets.
- **Same codec as the agent.** Trajectory files from human demos can be
  combined with agent trajectories without a separate parser.

## Usage

```sh
uv run python -m uo_proxy \
  --upstream login.your-shard.example:2593 \
  --listen 127.0.0.1:2593 \
  --advertise 127.0.0.1:2593 \
  --out data/trajectories/demo-session1.jsonl
```

Then configure ClassicUO to connect to `127.0.0.1:2593` (same port you passed
to `--listen`). The proxy will forward to the real server.

**Arguments:**

- `--upstream HOST[:PORT]` (required) — real UO login/game host.
- `--listen HOST[:PORT]` — local bind (default `127.0.0.1:2593`).
- `--advertise HOST[:PORT]` — address to put in the 0x8C redirect rewrite.
  Defaults to `--listen`. Set this if the login and game ports differ.
- `--out PATH` — JSONL output. Defaults to
  `data/trajectories/demo-<timestamp>.jsonl`.

## Intent labels via in-game chat

Type a line beginning with the configured prefix (default `//`) in ClassicUO.
The proxy detects it, **drops the packet** so the server never sees it (and
other players can't either), and records the label in a separate JSONL:

```
// mining bootstrap
// sell to vendor
// end cycle
```

Labels are written to `data/intents/intents-<timestamp>.jsonl`:

```json
{"schema":"uo_proxy.intent.v1","ts":1776582345.1,"session_id":"...","label":"mining bootstrap","source":"chat"}
```

Disable with `--intent-prefix ""`. Change prefix with e.g.
`--intent-prefix ";;"`.

## Output schema

Each line is one packet event:

```json
{
  "schema": "uo_proxy.packet.v1",
  "ts": 1776582345.123,
  "session_id": "1776582340-ab12cd34",
  "direction": "C->S",
  "phase": "game",
  "pid": "0x02",
  "size": 7,
  "hex": "020101020304050607",
  "note": null
}
```

- `direction`: `"C->S"` (ClassicUO → server) or `"S->C"`.
- `phase`: `"login"` (pre-GameLogin, both sides plaintext) or `"game"`
  (post-GameLogin, server side Huffman-compressed in transit).
- `note`: human-readable annotations set by the proxy, e.g.
  `"redirect orig=10.0.0.1:2593 auth=0xDEADBEEF"` or
  `"closed:ConnectionError"`.

## How the two login phases are handled

UO login is a two-connection flow:

1. Client → login server (plaintext both ways).
2. Login server sends `0x8C` with the game-server IP/port and an auth key.
3. Client reconnects to the game server (plaintext C→S, Huffman S→C).

The proxy:

- Intercepts `0x8C`, logs the original IP/port, rewrites to `--advertise`.
- Accepts the client's fresh connection (same listen port) and opens a new
  upstream connection. Whether it's "login" or "game" phase is detected from
  the first framed client packet (`0x80 AccountLogin` vs `0x91 GameLogin`).

## Limitations (MVP)

- Assumes login and game servers are reachable at the same upstream host.
  If your shard uses different hosts, the proxy currently always forwards to
  `--upstream`. A future upgrade will use the IP from the original 0x8C.
- Huffman is decompressed **only for logging** — we never re-encode and send
  back. This is fine because the proxy forwards the server's raw compressed
  bytes unchanged.
- No reconnect handling: if the proxy dies, ClassicUO session dies. Run
  under a process supervisor if you care.
- Variable-length packet with unknown id aborts the current direction.
  The wire path keeps working because forwarding is byte-level; only logging
  resyncs.

## Testing

```sh
uv run pytest tests/test_uo_proxy.py -v
```

14 tests cover framing, 0x8C rewrite, logger, and an end-to-end integration
test with a mock upstream.
