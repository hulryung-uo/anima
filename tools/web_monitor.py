#!/usr/bin/env python3
"""Web monitor — serves a live dashboard for the Anima agent."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_PORT = 8080
DEFAULT_EVENTS = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
MAX_EVENTS = 100
TAIL_INTERVAL = 1.0  # seconds between file polls


# ---------------------------------------------------------------------------
# Shared state — written by tailer thread, read by HTTP handler
# ---------------------------------------------------------------------------

class EventStore:
    """Thread-safe ring buffer of parsed event dicts."""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._max = max_events

    def add(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max:
                self._events = self._events[-self._max :]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._events)


STORE = EventStore()


# ---------------------------------------------------------------------------
# Background file tailer
# ---------------------------------------------------------------------------

def tail_events(path: Path) -> None:
    """Continuously tail *path*, pushing new lines into STORE."""
    offset = 0
    while True:
        try:
            if path.exists():
                with open(path, "r") as f:
                    f.seek(offset)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            STORE.add(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                    offset = f.tell()
        except OSError:
            pass
        time.sleep(TAIL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Anima Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a2e;color:#e0e0e0;font-family:"Menlo","Consolas",monospace;font-size:13px}
#root{display:flex;height:100vh;flex-direction:column}
header{background:#16213e;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #0f3460}
header h1{font-size:16px;color:#e0e0e0;font-weight:600;letter-spacing:1px}
header .status{font-size:12px;padding:3px 10px;border-radius:10px;background:#0f3460;color:#5b9bd5}
header .status.ok{color:#70c470;border:1px solid #70c470}
#content{display:flex;flex:1;overflow:hidden}
#feed{flex:1;overflow-y:auto;padding:10px 16px}
#sidebar{width:280px;background:#16213e;border-left:1px solid #0f3460;padding:14px;overflow-y:auto}
.event{padding:5px 0;border-bottom:1px solid #0f3460;display:flex;gap:10px;align-items:baseline}
.event .ts{color:#666;flex-shrink:0;width:64px}
.event .topic{flex-shrink:0;width:200px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.event .msg{color:#ccc;word-break:break-word}
#sidebar h3{color:#5b9bd5;font-size:13px;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
#sidebar .section{margin-bottom:18px}
#sidebar .kv{display:flex;justify-content:space-between;padding:2px 0;color:#aaa}
#sidebar .kv .val{color:#e0e0e0;font-weight:600}
#empty{color:#555;padding:40px;text-align:center;font-size:14px}
</style>
</head>
<body>
<div id="root">
  <header>
    <h1>Anima Monitor</h1>
    <span class="status" id="conn">connecting...</span>
  </header>
  <div id="content">
    <div id="feed"><div id="empty">Waiting for events...</div></div>
    <div id="sidebar">
      <div class="section">
        <h3>Stats</h3>
        <div class="kv"><span>Events</span><span class="val" id="st-count">0</span></div>
        <div class="kv"><span>Last update</span><span class="val" id="st-last">--</span></div>
      </div>
      <div class="section">
        <h3>Last Position</h3>
        <div class="kv"><span>Coords</span><span class="val" id="st-pos">--</span></div>
      </div>
      <div class="section">
        <h3>Recent Topics</h3>
        <div id="st-topics"></div>
      </div>
      <div class="section">
        <h3>Recent Skills</h3>
        <div id="st-skills"></div>
      </div>
    </div>
  </div>
</div>
<script>
const POLL_MS = 2000;
const COLORS = {avatar:"#5b9bd5",action:"#70c470",brain:"#b07cc8",combat:"#e06060"};
let knownLen = 0;

function colorFor(topic) {
  const p = (topic || "").split(".")[0];
  return COLORS[p] || "#aaa";
}

function fmtTs(ts) {
  try { return new Date(ts * 1000).toLocaleTimeString(); } catch { return String(ts); }
}

function renderEvents(events) {
  const feed = document.getElementById("feed");
  const conn = document.getElementById("conn");
  conn.textContent = "live";
  conn.classList.add("ok");

  if (events.length === 0) return;

  document.getElementById("st-count").textContent = events.length;

  // Detect new events
  if (events.length === knownLen) return;
  knownLen = events.length;

  // Clear placeholder
  const empty = document.getElementById("empty");
  if (empty) empty.remove();

  // Rebuild feed
  feed.innerHTML = "";
  events.forEach(e => {
    const d = document.createElement("div");
    d.className = "event";
    const c = colorFor(e.topic);
    d.innerHTML = `<span class="ts">${fmtTs(e.timestamp)}</span>`
      + `<span class="topic" style="color:${c}">${e.topic || "?"}</span>`
      + `<span class="msg">${(e.message || "").replace(/</g,"&lt;")}</span>`;
    feed.appendChild(d);
  });
  feed.scrollTop = feed.scrollHeight;

  // Last update
  const last = events[events.length - 1];
  document.getElementById("st-last").textContent = fmtTs(last.timestamp);

  // Position — scan for walk/move events
  for (let i = events.length - 1; i >= 0; i--) {
    const m = (events[i].message || "").match(/\((\d+),\s*(\d+)\)/);
    if (m) { document.getElementById("st-pos").textContent = `${m[1]}, ${m[2]}`; break; }
  }

  // Recent topics histogram
  const tc = {};
  events.slice(-50).forEach(e => { const t = (e.topic||"?").split(".")[0]; tc[t] = (tc[t]||0)+1; });
  const topicsEl = document.getElementById("st-topics");
  topicsEl.innerHTML = "";
  Object.entries(tc).sort((a,b)=>b[1]-a[1]).forEach(([k,v]) => {
    topicsEl.innerHTML += `<div class="kv"><span style="color:${COLORS[k]||'#aaa'}">${k}</span><span class="val">${v}</span></div>`;
  });

  // Recent skill results
  const skillsEl = document.getElementById("st-skills");
  skillsEl.innerHTML = "";
  events.filter(e => (e.topic||"").startsWith("action.skill") || (e.topic||"").startsWith("brain.skill"))
    .slice(-5).forEach(e => {
      skillsEl.innerHTML += `<div class="kv"><span>${(e.topic||"").split(".").pop()}</span><span class="val" style="color:#ccc">${(e.message||"").slice(0,30)}</span></div>`;
    });
}

async function poll() {
  try {
    const r = await fetch("/api/events");
    if (r.ok) renderEvents(await r.json());
  } catch {
    document.getElementById("conn").textContent = "disconnected";
    document.getElementById("conn").classList.remove("ok");
  }
}

setInterval(poll, POLL_MS);
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._html(200, DASHBOARD_HTML)
        elif self.path == "/api/events":
            self._json(200, STORE.snapshot())
        else:
            self._html(404, "<h1>404</h1>")

    # -- helpers --

    def _html(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: object) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ARG002
        # Silence per-request logs; only errors matter.
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Anima web monitor")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port (default: 8080)")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="Path to events.jsonl")
    args = parser.parse_args()

    events_path: Path = args.events.resolve()
    port: int = args.port

    # Start tailer thread
    t = threading.Thread(target=tail_events, args=(events_path,), daemon=True)
    t.start()
    print(f"Tailing {events_path}")

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Anima Monitor running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
