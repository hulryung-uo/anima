# Agent API & Dashboard Architecture

## Overview

Anima는 두 개의 독립 레이어로 분리된다:

- **Agent Backend**: UO 서버 연결, perception, planner, procedure 실행, API 서버
- **Clients**: 상태 관찰 + 에이전트 제어. TUI, Web GUI, 또는 다른 AI 에이전트

모든 클라이언트는 **동일한 WebSocket 프로토콜**로 Agent에 접속한다.

```
┌──────────────────────────────────────┐
│           Agent Backend               │
│                                       │
│  UO TCP ←→ Perception                 │
│  Planner ←→ Procedures                │
│  CommandBus (외부 명령 수신)            │
│                                       │
│  ┌─────────────────────────────────┐  │
│  │   API Server (WebSocket :8150)  │  │
│  │                                 │  │
│  │   ← state push (0.5s interval)  │  │
│  │   → command dispatch            │  │
│  │   항상 실행 (core infrastructure) │  │
│  └─────────────────────────────────┘  │
└──────────────────────────────────────┘
         ▲            ▲            ▲
         │ws          │ws          │ws
    ┌────┴───┐  ┌─────┴────┐  ┌───┴────┐
    │  TUI   │  │ Web GUI  │  │ AI/Bot │
    │(터미널) │  │(브라우저) │  │ (향후) │
    └────────┘  └──────────┘  └────────┘

    모든 클라이언트가 동일한 WebSocket 프로토콜 사용
    여러 클라이언트 동시 접속 가능
```

## 동시 접속

- WebSocket 서버는 **다중 클라이언트**를 지원
- TUI 여러 개, Web GUI 여러 개, AI 에이전트를 동시에 연결 가능
- 모든 클라이언트가 동일한 state push를 받음
- 어떤 클라이언트에서든 command를 보낼 수 있음

## 실행 방법

```bash
# Agent 시작 (API 서버 자동 실행 on :8150)
uv run python -m anima

# 포트 변경
uv run python -m anima --web-port 9000

# --- 클라이언트 접속 ---

# TUI (WebSocket)
uv run python tools/tui.py

# TUI (다른 호스트의 Agent에 접속)
uv run python tools/tui.py --url ws://192.168.1.100:8150/ws

# TUI (레거시 file polling)
uv run python tools/tui.py --file

# Web GUI
open http://localhost:8150
```

## WebSocket Protocol

Endpoint: `ws://host:8150/ws`

### Server → Client: State Push

0.5초 간격으로 전체 상태 스냅샷 전송.

```json
{
  "ts": 1774597376.58,
  "status": {
    "name": "Grimm",
    "hp": 106, "hp_max": 106,
    "mana": 10, "mana_max": 10,
    "stam": 10, "stam_max": 10,
    "str": 113, "dex": 10, "int": 10,
    "x": 2472, "y": 564, "z": 5,
    "gold": 18, "weight": 36, "weight_max": 432,
    "goal": "", "move_target": null,
    "paused": false
  },
  "nearby": [
    {"name": "Mirielle", "x": 2472, "y": 565, "dx": 0, "dy": 1, "notoriety": 1}
  ],
  "journal": [
    {"ts": 1774597370, "name": "System", "text": "Welcome!", "is_self": false}
  ],
  "inventory": [
    {"name": "iron ingots", "amount": 2}
  ],
  "skills": {
    "list": [{"id": 45, "value": 77.1, "cap": 100.0, "lock": 0}],
    "total": 344.6
  },
  "activity": [
    {"ts": 1774597375, "topic": "action.end", "message": "...", "importance": 2}
  ],
  "minimap": {
    "rows": ["...###...@...###..."],
    "px": 2472, "py": 564, "radius": 30
  },
  "ping_ms": 33.0
}
```

### Client → Server: Commands

```json
{"cmd": "pause"}
{"cmd": "resume"}
{"cmd": "go_to", "x": 2500, "y": 550}
{"cmd": "set_goal", "goal": "mine"}
{"cmd": "say", "text": "Hello!"}
{"cmd": "run_procedure", "name": "mine_ore"}
```

| Command | Payload | Effect |
|---------|---------|--------|
| `pause` | — | Planner 일시정지 |
| `resume` | — | Planner 재개 |
| `go_to` | `x`, `y` | 해당 좌표로 이동 |
| `set_goal` | `goal` | Planner 목표 변경 |
| `say` | `text` | 게임 내 발화 |
| `run_procedure` | `name` | 다음 tick에 지정 procedure 실행 |

### Server → Client: Command Response

```json
{"type": "cmd_result", "cmd": "pause", "ok": true, "message": "Planner paused"}
```

## Internal Architecture

### CommandBus (`anima/web/command_bus.py`)

외부 명령을 Agent 내부로 전달하는 큐.

```python
class CommandBus:
    def push(cmd, **params)      # WebSocket handler가 호출
    def pop() -> Command | None  # Planner가 매 tick마다 확인

    paused: bool                 # Planner 일시정지 상태
    override_procedure: str      # 강제 실행할 procedure (1회성)
    override_go_to: (x, y)       # 강제 이동 (1회성)
    goal: str                    # 현재 목표
```

### Data Flow

```
StatePublisher (0.5s interval)
  ├→ state.json (file dump, 디버그용)
  └→ WebServer.broadcast(snapshot)
       └→ ws.send_str(json) to ALL connected clients

Client command
  → WebServer._ws_handler() receives JSON
    → Immediate commands (say): execute directly
    → Planner commands (pause, go_to): push to CommandBus
      → Planner.tick() checks CommandBus each cycle
  → ws.send_str(cmd_result) back to requesting client
```

## File Structure

```
anima/web/
  __init__.py
  server.py          # aiohttp WebSocket API server + static file serving
  command_bus.py      # CommandBus — external command queue
  static/
    index.html        # Web GUI dashboard (HTML + CSS + JS)

tools/
  tui.py              # TUI client (WebSocket or file polling)
```

## Notes

- `state.json`은 디버그/fallback 용도로 유지. Primary interface는 WebSocket.
- API 서버는 Agent와 같은 프로세스에서 실행 (state 접근이 필요하므로).
- 인증 없음 — 로컬 개발 전용. 외부 노출 시 reverse proxy + auth 필요.
