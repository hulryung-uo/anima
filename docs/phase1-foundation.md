# Phase 1: Foundation — 상세 계획

> "서버와 통신하고, 안정적으로 행동한다"

---

## 1. 현재 상태 진단

### 동작하는 것

| 시스템 | 구현체 | 검증 수준 |
|--------|--------|-----------|
| 패킷 통신 | `anima/client/` (connection, codec, packets, parser) | 50+ 패킷 타입. 실서버 테스트 완료 |
| Perception | `anima/perception/` (world_state, self_state, social) | MobileIncoming, ItemInfo, Skills, Equipment 추적 |
| 이동 | `anima/action/movement.py` — A* + go_to() | 문 열기, stuck 탈출, denied tile 캐시 |
| 채광 | `anima/procedures/mine_ore.py` | 타일 검색, depleted 처리, too-far 블랙리스트 |
| 제련 | `anima/procedures/smelt_ore.py` | 용광로 검색, 제련 실행 |
| 제작 | `anima/procedures/craft_blacksmith.py` | gump 파싱, 레시피 선택, notice 읽기 |
| 도구 | `anima/procedures/make_tools.py` | tinkering gump, 실패 감지 |
| 판매 | `anima/procedures/sell_to_vendor.py` | context menu, sell list, 거부 처리 |
| 구매 | `anima/procedures/buy_from_vendor.py` | buy list, 도구 자동 구매 |
| 은행 | `anima/procedures/bank_deposit.py` | bank deposit |
| 대시보드 | `anima/web/` + `tools/tui.py` | WebSocket 실시간 상태, Agent Intent 패널 |
| 자기진단 | `tools/supervisor.py` + `tools/self_improve.py` | 3-level 자동 복구 + Claude Code 연동 |

### 알려진 문제점

#### 상태 비동기화 (State Desynchronization)
**문제**: 클라이언트의 월드 모델이 서버 현실과 점차 어긋남.
- 아이템이 서버에서 사라졌지만 perception에 남아있음
- NPC가 이동했지만 마지막 알려진 위치로 인식
- 장비/인벤토리가 서버와 불일치

**기존 UO 봇의 해결책**:
- **Razor Enhanced**: 액션 사이에 350-650ms 지연 강제 (서버 틱 레이트 맞춤)
- **ClassicAssist**: 엄격한 패킷-응답-패킷 순서 (서버 확인 없이 다음 액션 안 보냄)
- **EasyUO**: 주기적 인벤토리 재스캔 (backpack double-click)

**현재 Anima**: `asyncio.sleep(0.3)` 사용 (너무 짧을 수 있음). 글로벌 "pending response" 락 없음.

> **참고**: [Razor Enhanced Documentation](https://www.razorenhanced.net/) — 패킷 타이밍 가이드

#### Gump 상태 머신 취약성
**문제**: 제작/구매/판매 모두 gump을 거침. gump이 늦게 오거나, 예상과 다른 내용이거나, 안 올 수 있음.
- 현재: 고정 `asyncio.sleep(3.0)` 대기 → 취약
- 필요: 이벤트 기반 대기 (gump_opened 이벤트 구독) + 타임아웃

**기존 봇의 패턴** (EasyUO/Razor Enhanced):
```
1. 액션 전송 (double-click tool)
2. 100ms마다 gump 도착 확인 (최대 5초)
3. 타임아웃 시 bail out
4. gump 내용 검증 후 응답
```

#### 패스파인딩 한계
**문제**: 4방향 A* (N/E/S/W)만 사용. ClassicUO는 8방향 (대각선 포함).
- 대각선 이동 없이 ~41% 더 많은 걸음 필요
- 코너에서 막힘 (대각선이면 통과 가능한 곳)

**ClassicUO 참고** (`Pathfinder.cs`):
- 8방향 A*, 대각선 비용 2, 직선 비용 1
- 대각선 이동 시 인접 직선 타일 2개 모두 통과 가능해야 함
- 최대 10,000 노드 탐색

> **참고**: ClassicUO 소스 `~/dev/uo/classicuo/src/ClassicUO.Client/Game/Pathfinder.cs`

---

## 2. 남은 작업 상세

### P1-A: 안정성 개선

#### A1. 패킷 타이밍 강화
```
현재: 각 procedure에서 개별적으로 sleep
목표: Connection 레이어에서 최소 간격 보장
```

구현:
- [ ] `UoConnection`에 `min_packet_interval = 0.4` (400ms) 추가
- [ ] `send_packet()` 에서 마지막 전송 시간 체크, 필요시 자동 대기
- [ ] Gump 응답은 별도 관리 (gump_id 기반 대기)

#### A2. Gump 이벤트 기반 대기
```
현재: await asyncio.sleep(3.0)  # 고정 대기
목표: await wait_for_gump(ctx, gump_id, timeout=5.0)  # 이벤트 대기
```

`wait_for_gump`은 이미 `anima/actions/gump.py`에 있음. 모든 procedure에서 일관되게 사용하도록 통일.

#### A3. 주기적 인벤토리 재동기화
```python
# planner 루프에 5분마다 실행
if now - last_inventory_sync > 300:
    await conn.send_packet(build_double_click(backpack_serial))
    last_inventory_sync = now
```

#### A4. 재접속 시 월드 상태 복구
연결 끊김 → 재접속 후 실행할 "cold boot" 시퀀스:
1. 장비/백팩 재요청
2. 주변 NPC OPL 요청
3. 위치 확인 (서버가 보내는 LoginConfirm에서)
4. blackboard 일시 상태 초기화 (depleted_mines는 유지)

### P1-B: 미완성 기능

#### B1. 스킬/스탯 Lock 관리

UO 스킬 시스템:
- 전체 스킬 합계 상한: 700.0 (서버 설정)
- 개별 스킬 상한: 100.0
- Lock 상태: Up (올림) / Down (내림) / Locked (고정)
- 스킬 gain 시 Up인 스킬이 올라가고, Down인 다른 스킬이 내려감

ServUO 스킬 gain 공식:
```
gain_chance = (skill_cap - current_value) / skill_cap
```
- 0.0 → 100% gain 확률
- 50.0 → 50% gain 확률  
- 100.0 근처 → 거의 0%
- GGS (Guaranteed Gain System): ~10분간 gain 없으면 다음 시도 보장

대장장이 최적 Lock 설정:
```yaml
skills:
  mining: up        # 주력 1
  blacksmithy: up   # 주력 2
  tinkering: up     # 도구 제작
  arms_lore: up     # 무기 감정 (제작 보너스)
  # 나머지 모두: locked
stats:
  STR: up           # 채광/대장장이 관련
  DEX: locked
  INT: locked
```

구현: 패킷 0x3A (SkillLockChange), 0xBF sub 0x1A (StatLockChange)

#### B2. 8방향 패스파인딩
현재 4방향을 8방향으로 확장:
- `pathfinding.py`의 DIRECTIONS에 NE/SE/SW/NW 추가
- 대각선 이동 시 인접 직선 타일 검증 추가
- ClassicUO의 `CanWalk()` 로직 참고

#### B3. 장거리 이동 (Waypoint System)
도시 간 이동을 위한 waypoint 그래프:
```
Minoc Mine ← 30 → Minoc Town ← 5 → Minoc Bank
                       ↓ 200
               Britain Gate ← 10 → Britain Bank
```

`world_knowledge.py`에 이미 위치 데이터 있음. 이를 waypoint 그래프로 연결하면 됨.
현재 `_find_waypoint_toward()` 함수가 기본 구현 있음. 보강 필요:
- 도시 간 경로 사전 정의
- Moongate 지원 (텔레포트)

---

## 3. 게임플레이 루프 검증 계획

### 자동 테스트 시나리오

```
시나리오 1: 기본 채광 루프 (30분)
  시작: Minoc Mine 근처, 곡괭이 보유
  기대: mine → smelt → mine 반복
  성공: ore_mined > 10, ingots_smelted > 5

시나리오 2: 전체 경제 루프 (1시간)
  시작: Minoc, 곡괭이 + 약간의 gold
  기대: mine → smelt → craft → sell → bank → mine 반복
  성공: gold_earned > 50, no stuck > 5분

시나리오 3: 도구 고갈 복구 (30분)
  시작: Minoc, 곡괭이 없음, gold 50
  기대: buy pickaxe → mine → ...
  성공: 도구 구매 성공, 채광 재개

시나리오 4: 재접속 복구 (30분)
  시작: 정상 동작 중 강제 연결 끊기
  기대: 자동 재접속 → 상태 복구 → 활동 재개
  성공: 재접속 후 5분 내 정상 procedure 실행
```

### 성공 지표 (Metrics)

| 지표 | 목표 | 측정 방법 |
|------|------|-----------|
| 무중단 운영 시간 | 4시간+ | 첫/마지막 성공 procedure 시간 차이 |
| 시간당 골드 수입 | 50g+/hr | action_logs 집계 |
| Procedure 성공률 | 70%+ | action_logs success/total |
| 실패 복구 시간 | <60초 | 마지막 실패 → 다음 성공 시간 차이 |
| Stuck 발생 빈도 | <1회/시간 | 5분간 procedure 없는 구간 수 |
| 메모리 사용량 | <200MB | 프로세스 모니터링 |

### 테스트 방법론

#### 패킷 리플레이 테스트
실제 서버 세션의 패킷을 녹화하여 perception 레이어에 재생:
```python
# tests/test_perception_replay.py
async def test_worldstate_from_packet_trace():
    trace = load_packet_trace("data/traces/minoc_mining_30min.bin")
    handler = PacketHandler(perception)
    for packet_id, data in trace:
        handler.dispatch(packet_id, data)
    assert len(perception.world.items) > 0
    assert perception.self_state.x != 0
```

#### Planner 시뮬레이션 테스트
Perception을 mocking하고 planner가 올바른 procedure를 선택하는지 검증:
```python
async def test_planner_selects_smelt_when_has_ore():
    ctx = make_test_context(
        inventory={"ore": 10, "pickaxe": 1},
        location="forge_nearby",
    )
    proc = await planner.select_procedure(ctx)
    assert proc.name == "smelt_ore"
```

> **참고**: [Behavioral Cloning in Game Environments (2024)](https://arxiv.org/abs/2401.03993) — 패킷 로그에서 행동 데이터 추출

---

## 4. 리스크와 완화 전략

| 리스크 | 영향 | 완화 |
|--------|------|------|
| Gump 응답 타이밍 변동 | 제작/거래 실패 누적 | 이벤트 기반 대기 + 타임아웃 |
| NPC 이동으로 벤더 못 찾음 | 판매 루프 stuck | OPL 선요청 + 넓은 검색 범위 |
| 패스파인딩 무한루프 | 에이전트 멈춤 | 최대 시도 횟수 + 랜덤 워크 탈출 |
| 인벤토리 비동기화 | 잘못된 판단 (주괴 있다고 생각하지만 없음) | 주기적 재스캔 |
| 서버 재시작 | 연결 끊김 | 자동 재접속 + cold boot |
| SQLite 동시 쓰기 병목 | 로그 기록 지연 | WAL 모드 (이미 사용 중) + 배치 쓰기 |

---

## 5. 선행 연구 / 참고 자료

- [Razor Enhanced 스크립팅 가이드](https://www.razorenhanced.net/) — UO 봇 패킷 타이밍
- [UO Outlands Razor Scripts](https://outlands.uorazorscripts.com/scripts) — 커뮤니티 스크립트 패턴
- [ClassicUO Pathfinder.cs](~/dev/uo/classicuo/) — 8방향 A* + Z-level 처리
- [ClassicUO WalkerManager.cs](~/dev/uo/classicuo/) — Walk 시퀀스 관리
- ServUO `BaseVendor.cs` — 벤더 buy/sell 메커니즘
- ServUO `SkillCheck.cs` — 스킬 gain 공식, GGS
