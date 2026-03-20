# Self-Improvement System — 자기 발전형 개발 자동화

## 개요

Anima 에이전트가 실행 중 발생하는 문제를 자동으로 감지하고, 분석 리포트를 생성하며,
Claude Code를 호출해 코드를 개선하고, 재시작하는 자동화 사이클.

## 아키텍처

```
┌──────────────────────────────────────────────────┐
│               Anima Agent (실행 중)                │
│  게임 플레이 → structlog JSON → data/anima.jsonl  │
│                                metrics 수집        │
└───────────────────┬──────────────────────────────┘
                    │
          cron: 10분마다 트리거
                    │
┌───────────────────▼──────────────────────────────┐
│              Log Analyzer                         │
│  1. 최근 10분 로그 읽기                            │
│  2. 메트릭 계산 (성공률, 이동효율, gold/시간)        │
│  3. 문제 패턴 감지 (연속실패, stuck, 무한루프)       │
│  4. data/analysis/YYYYMMDD_HHMM.md 생성           │
└───────────────────┬──────────────────────────────┘
                    │
          문제 심각도 >= MEDIUM?
                    │ YES
┌───────────────────▼──────────────────────────────┐
│           Improvement Planner (LLM)               │
│  1. analysis.md + 소스코드 컨텍스트 전달            │
│  2. 구체적 수정 계획 생성                          │
│  3. data/plans/YYYYMMDD_HHMM.md 저장              │
└───────────────────┬──────────────────────────────┘
                    │
          자동 적용 가능한 변경?
                    │ YES
┌───────────────────▼──────────────────────────────┐
│           Auto Patcher                            │
│  1. 파라미터 변경 (config, thresholds)             │
│  2. 데이터 추가 (locations, recipes)               │
│  3. pytest 실행 → 통과 시 적용                     │
│  4. git commit (자동)                              │
│  5. Anima 재시작                                   │
└──────────────────────────────────────────────────┘
```

## Phase 1: 로그 구조화 + 메트릭 수집

### 1.1 JSON 로그 포맷

`data/anima.jsonl` — 각 줄이 JSON 객체:

```json
{"ts": 1711060800.0, "event": "chop_success", "data": {"logs": 5, "tree": "(1600,1490)"}}
{"ts": 1711060810.0, "event": "walk_denied", "data": {"pos": "(1595,1490)", "seq": 42}}
{"ts": 1711060820.0, "event": "skill_gain", "data": {"skill": "Lumberjacking", "old": 50.0, "new": 50.1}}
```

### 1.2 메트릭 (10분 윈도우)

| 메트릭 | 설명 | 건강 기준 |
|--------|------|----------|
| `walk_success_rate` | confirmed / (confirmed + denied) | > 0.8 |
| `skill_success_rate` | success / total executions | > 0.3 |
| `chop_success_rate` | logs gained / chop attempts | > 0.2 |
| `gold_per_minute` | gold earned / elapsed minutes | > 0 |
| `distance_moved` | total tiles walked | > 10 |
| `stuck_count` | escape_stuck + wander_stuck 횟수 | < 3 |
| `unique_positions` | 방문한 고유 좌표 수 | > 5 |

### 1.3 구현 파일

```
anima/monitor/
├── metrics.py      # MetricsCollector — 이벤트 카운트, 윈도우 평균
├── analyzer.py     # LogAnalyzer — 로그 읽기, 패턴 감지, 보고서 생성
├── report.py       # (기존) 문제 리포트 생성
└── feed.py         # (기존) ActivityFeed
```

## Phase 2: 자동 분석 + 문제 감지

### 2.1 문제 패턴 감지

```python
PATTERNS = {
    "stuck_loop": {
        "condition": "walk_denied > 20 AND unique_positions < 3",
        "severity": "HIGH",
        "suggestion": "이동 로직 개선 또는 위치 변경 필요",
    },
    "skill_spam": {
        "condition": "same_skill_fail > 10 consecutively",
        "severity": "MEDIUM",
        "suggestion": "can_execute 조건 강화 필요",
    },
    "no_progress": {
        "condition": "gold_per_minute == 0 AND distance_moved < 5",
        "severity": "HIGH",
        "suggestion": "에이전트가 아무것도 못하고 있음",
    },
    "weight_blocked": {
        "condition": "weight > 80% AND no_sell_attempt",
        "severity": "MEDIUM",
        "suggestion": "판매 행동 필요",
    },
    "tool_missing": {
        "condition": "skill_fail reason contains 'No hatchet' OR 'No saw'",
        "severity": "MEDIUM",
        "suggestion": "도구 구매 필요",
    },
}
```

### 2.2 분석 리포트 포맷

`data/analysis/20260320_0110.md`:

```markdown
# Analysis Report — 2026-03-20 01:10

## Metrics (last 10 minutes)
- Walk success rate: 0.45 (LOW — threshold 0.8)
- Skill success rate: 0.0 (CRITICAL)
- Stuck count: 5 (HIGH)
- Gold earned: 0

## Problems Detected
1. [HIGH] stuck_loop: 20 walk denials, only 2 unique positions
2. [MEDIUM] skill_spam: craft_carpentry failed 15 times (no materials)

## Suggested Actions
1. Move character to different area (current area fully blocked)
2. Add minimum material check to craft_carpentry can_execute
```

## Phase 3: 자동 수정 (안전한 범위)

### 3.1 자동 적용 가능한 변경 유형

| 유형 | 예시 | 위험도 |
|------|------|--------|
| **파라미터 조정** | SKILL_COOLDOWN 변경, SEARCH_RADIUS 조정 | LOW |
| **데이터 추가** | 새 Location, depleted tree 좌표 | LOW |
| **config 변경** | walk_delay_ms, 목표 변경 | LOW |
| **can_execute 조건** | 최소 재료량 추가, 무게 threshold | MEDIUM |
| **새 스킬 레시피** | CRAFT_TARGETS에 아이템 추가 | MEDIUM |

### 3.2 자동 패치 흐름

```python
async def auto_patch(plan: dict) -> bool:
    """안전한 자동 패치 적용."""
    # 1. 변경 유형 확인
    if plan["risk"] not in ("LOW", "MEDIUM"):
        return False  # HIGH risk는 수동 검토

    # 2. 변경 적용
    apply_changes(plan["changes"])

    # 3. 테스트 실행
    if not run_tests():
        rollback()
        return False

    # 4. 커밋
    git_commit(plan["description"])

    # 5. 에이전트 재시작
    restart_agent()
    return True
```

### 3.3 안전장치

- pytest 통과 필수
- 한 번에 1개 파일만 변경
- HIGH risk 변경은 사람 확인 필요
- 10분 쿨다운 (연속 패치 방지)
- 성능 악화 시 자동 롤백 (이전 메트릭과 비교)
- 하루 최대 10회 자동 패치

## Phase 4: Claude Code 연동

### 4.1 복잡한 문제 → Claude Code 호출

```bash
# tools/call_claude.sh
claude -p "
다음 분석 리포트를 읽고, 코드를 수정해주세요.

$(cat data/analysis/latest.md)

수정 규칙:
- 한 번에 1-2개 파일만 수정
- pytest 통과 확인
- git commit 후 push
"
```

### 4.2 자동 호출 조건

- 자동 패치로 해결 불가능한 문제
- 같은 문제가 3회 연속 발생
- 심각도 HIGH 문제

## 구현 우선순위

### 즉시 (Phase 1)
1. `metrics.py` — 이벤트 카운팅, 윈도우 메트릭
2. `analyzer.py` — 로그 분석, 패턴 감지, 리포트 생성
3. `tools/analyze.py` — CLI에서 수동 실행 가능

### 단기 (Phase 2)
4. brain_loop에 주기적 분석 통합 (10분마다)
5. 문제 감지 시 activity feed + journal에 표시

### 중기 (Phase 3)
6. 파라미터 자동 조정
7. config 기반 튜닝 (data-driven)
8. A/B 테스트 프레임워크

### 장기 (Phase 4)
9. Claude Code CLI 자동 호출
10. 수정-테스트-배포 파이프라인
11. 성능 대시보드

## 소스 구조 개선 필요사항

### 파라미터를 config로 분리

현재 하드코딩된 값들을 `config.yaml`이나 별도 파일로:

```yaml
# tuning.yaml
skills:
  chop_wood:
    search_radius: 8
    depleted_cooldown: 1200
    weight_margin: 20
  craft_carpentry:
    min_materials: 4
    weight_margin: 10
movement:
  deny_cooldown_ms: 200
  escape_search_radius: 14
  max_consecutive_denials: 5
brain:
  skill_cooldown: 0.5
  think_cooldown: 15.0
  max_skill_fails_before_rethink: 5
```

### 스킬 레시피 외부 데이터화

```yaml
# data/recipes/carpentry.yaml
- name: Barrel Staves
  group_index: 0
  item_index: 0
  min_skill: 0.0
  materials: {board: 5}
- name: Wooden Box
  group_index: 2
  item_index: 0
  min_skill: 21.0
  materials: {board: 10}
```

### 위치 정보 외부 데이터화

```yaml
# data/locations/britain.yaml
- name: West Britain Bank
  x: 1434
  y: 1699
  type: outdoor
  tags: [bank, gathering_spot]
```
