# Training Data Pipeline

> **목적**: 도메인 특화 소형 모델(Anima 전용 UO 에이전트 LLM)을 **오프라인**으로 학습시키기 위한 데이터 수집 설계.
>
> **관련 문서**:
> - `docs/reinforcement-learning.md` — 현재의 온라인 Q-learning + UCB1 (skill selector 레이어). 본 문서와 레이어가 다름.
> - `docs/skill-system.md` — Skill/Procedure 인터페이스.
> - `DESIGN.md` — 전체 아키텍처.

---

## 1. 왜 별도 파이프라인인가

현재의 Q-table은 **선택 시점에 참조되는 통계**다. 반면 학습 데이터 파이프라인이 필요한 이유:

- 소형 LLM (Qwen 3B / 7B 등)을 **behavioral cloning** 또는 **offline RL**로 파인튜닝하려면 `(state_text, chosen_action, reward)` 궤적이 필요하다.
- Q-table은 "요약된 가치"만 저장하므로 **컨텍스트 복원 불가** (어떤 상황에서 왜 그 선택을 했는지).
- 전문가 시연(사람 플레이) 데이터는 Q-table 구조에 들어가지 않는다 — 별도 스키마 필요.
- 평가/홀드아웃·A/B 비교를 하려면 **원본 궤적**을 보존해야 한다.

**핵심 원칙**: 기존 에이전트 운영에 **영향 0** — 관찰자 레이어만 추가.

---

## 2. 3가지 데이터 소스

| 소스 | 누가 생성 | 가치 | 양 |
|---|---|---|---|
| **A. 에이전트 궤적** | 현 Anima (자동) | 실패 사례 풍부, 현재 정책 기준선 | 대량 (지금도 쌓임) |
| **B. 전문가 시연** | 사람이 직접 플레이 | 고품질 labels, BC bootstrap | 소량 (주 1–2회) |
| **C. LLM 교사** | Opus/Sonnet이 라벨링 | 대량 생성 가능, 단 비용 | 중간 |

시작은 **A + B** 먼저. C는 파이프라인 검증 후.

---

## 3. 스키마

### 3.1 Trajectory Event (궤적 단위 원자)

`data/trajectories/YYYY-MM-DD.jsonl` 에 한 줄 = 한 이벤트:

```json
{
  "schema": "trajectory.v1",
  "ts": 1776550000.123,
  "episode_id": "2026-04-18T14:30:00Z-00007",
  "step_id": 42,
  "source": "agent|human|llm_teacher",
  "agent_name": "Grimm",
  "decision_point": "procedure_select|procedure_end|gump_response|combat_decision",
  "state": { ... },            // 아래 3.2
  "candidates": [ ... ],       // 선택 가능했던 행동 목록 (optional, but valuable)
  "action": { ... },           // 실제 선택된 행동 (아래 3.3)
  "outcome": { ... },          // 실행 결과 (아래 3.4)
  "reward": { ... },           // 즉시/지연 보상 (아래 3.5)
  "context": { ... }           // goal, intent, constraints
}
```

### 3.2 `state` — 관찰된 세상

에이전트가 결정 시점에 **실제로 본 정보만**. Ground truth 아님.

```json
{
  "pos": {"x": 2532, "y": 573, "z": 0, "map": 0},
  "vitals": {"hp": 112, "hp_max": 112, "mana": 10, "stam": 10},
  "resources": {"gold": 4, "weight": 37, "weight_max": 474},
  "skills": [{"id": 7, "value": 64.8, "lock": 2}, ...],

  "inventory": [
    {"graphic": 0x0F3F, "hue": 0, "count": 3, "name": "tinker's tools"},
    ...
  ],

  "nearby_mobiles": [
    {"serial": "0x00000111", "body": 0x0191,
     "dx": -18, "dy": -14,
     "notoriety": 1, "name": "Autumn the armourer",
     "is_vendor": true}
  ],
  "nearby_items": [
    {"graphic": 0x1BF2, "dx": 2, "dy": 0, "hue": 0}
  ],

  "map_digest": "minoc_bank_area",   // coarse region label (32×32 격자)
  "recent_events": [                 // 최근 N개 action.end summary
    {"proc": "move_to_Minoc Bank", "result": "blocked", "age_s": 12},
    ...
  ]
}
```

### 3.3 `action` — 두 레벨 기록

```json
{
  "level": "procedure",           // procedure | primitive | tool_call
  "procedure": "sell_to_vendor",  // high-level
  "params": {"vendor_serial": "0x0000074D"},
  "primitives": [                 // 실제 내보낸 패킷 요약 (optional)
    {"kind": "context_menu", "target": "0x0000074D"},
    {"kind": "gump_response", "button": 2}
  ]
}
```

### 3.4 `outcome` — 실행 결과

```json
{
  "success": false,
  "result": "blocked",
  "reason": "context_menu_timeout",
  "duration_ms": 1583,
  "terminal_state_diff": {        // state 변화 요약 (inventory delta, pos delta 등)
    "gold_delta": 0,
    "inventory_delta": []
  }
}
```

### 3.5 `reward` — 즉시 + 지연

```json
{
  "immediate": -1.0,              // SkillResult.reward or rule-based
  "delayed": {                    // 사후 어사인 (episode 종료 후 backfill)
    "horizon_60s_gold": 0,
    "horizon_300s_gold": 0,
    "episode_total": -3.5,
    "episode_outcome": "deadlock_entered"
  }
}
```

지연 보상은 **episode 종료 시 batch로 backfill**하는 전용 스크립트가 기록한다 (원본 줄은 `reward.delayed: null`로 쓰고 나중에 병합). 이 방식이 온라인 루프 블로킹 없음.

---

## 4. Episode 경계

한 에피소드 = "의미 있는 사이클 한 바퀴" 또는 "주요 상태 리셋 사이".

경계 트리거:
- 사망 (`planner.death`)
- 데드락 진입 (`DEADLOCK` intent)
- 골드 0→양수 전환 또는 양수→0 전환
- 재접속 (세션 시작)
- 수동 라벨 (사람 플레이 시 "여기서 세그먼트 끊기" 버튼)

`episode_id` 는 시작 ISO timestamp + 카운터로 식별자 생성. 에피소드 단위 집계는 별도 파일 `data/trajectories/episodes.jsonl`:

```json
{
  "episode_id": "...",
  "start_ts": ..., "end_ts": ...,
  "step_count": 87,
  "outcome": "mining_cycle_complete",   // 또는 deadlock, death, idle_timeout
  "gold_delta": 450,
  "skill_gains": {"7": 0.2},
  "goal": "mine and sell 100 ingots"
}
```

---

## 5. 수집 인프라

### 5.1 Agent 측 (자동, source="agent")

**훅 지점**:
- `Procedure.execute()` 시작 전: state 스냅샷 + candidates 기록
- `Procedure.execute()` 종료 후: outcome + immediate reward 기록
- Planner 결정 지점: 후보 프로시저 집합 기록

**구현 스케치** (`anima/memory/trajectory.py` 신규):
```python
class TrajectoryRecorder:
    """Non-blocking JSONL writer. Never raises into caller."""
    def __init__(self, out_dir: Path):
        self._queue = asyncio.Queue(maxsize=10000)
        self._task = asyncio.create_task(self._flush_loop())

    def record(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop silently rather than block

    async def _flush_loop(self) -> None:
        # batch-writes every 5s or 100 events
        ...
```

**feature flag**:
- `ANIMA_RECORD_TRAJECTORIES=1` env 또는 `--record-trajectories` CLI 옵션
- OFF일 때 훅은 no-op

### 5.2 Human 시연 측 (수동, source="human")

별도 프로세스: `tools/record_demo.py`

- ClassicUO 옆에서 사람이 플레이
- TCP 패킷 sniff (에이전트가 UO 서버에 연결하듯이, 단 read-only)
- 자기 행동을 `procedure_select`와 유사한 단위로 세그먼트화 (사람이 한 목적 단위)
- 중간중간 `SPACE` 키 같은 마커로 episode 경계 수동 지정

**최소 MVP**: packet log만 raw로 저장 → 오프라인에서 파싱·세그먼트화.

### 5.3 LLM 교사 측 (비동기, source="llm_teacher")

`tools/llm_labeler.py`: 기존 에이전트 궤적의 `(state, candidates)` 지점을 Opus에게 던져 "가장 좋은 선택 + 이유" 받아 별도 파일 `data/labels/*.jsonl`에 저장.

- 비용 관리: 샘플링 (전체가 아닌 특정 상태 조합만)
- 제휴 저장: `episode_id + step_id` 기준으로 궤적과 join

---

## 6. 디스크·프라이버시·보관

- **용량 예상**: 평균 이벤트 ~2KB × 하루 10k 이벤트 = ~20MB/일. 한 달 600MB. 허용.
- **압축**: 매일 자정 `gzip` 회전 → `data/trajectories/2026-04-18.jsonl.gz`
- **보존 정책**: 최근 90일 원본, 그 이상은 집계만 (에피소드 요약 보존)
- **민감 정보**: 에이전트 비밀번호·세션 키는 절대 기록 금지 (리스트 기반 redact)
- **git**: `data/trajectories/` 는 gitignore (이미 `data/` 자체가 제외)

---

## 7. 품질 게이트

훈련에 쓰기 전 검증:

- **스키마 유효성**: `jsonschema` 검증 CLI (`tools/validate_trajectories.py`)
- **필수 필드 존재율**: state·action·outcome 누락 <1%
- **에피소드 경계 정합성**: 모든 step_id가 episode에 속함
- **시간 정합성**: ts 단조 증가
- **보상 분포**: outlier 감지 (episode_total이 -1000 같은 비정상 값)

---

## 8. 학습 경로 (스키마와 연결)

### 8.1 Behavioral Cloning

```
입력: (state_text, candidates_text)
출력: chosen_action + reasoning
훈련: cross-entropy loss on action label
```

필요 데이터: source=human 궤적 1만+ step 권장.

### 8.2 Offline RL (CQL / AWR)

```
입력: (state, action, reward, next_state)
출력: Q-function 또는 policy
```

필요 데이터: agent 궤적 10만+ step, 다양한 보상 분포.

### 8.3 소형 LLM 파인튜닝 (SFT + DPO)

```
SFT: (state_text, best_action) pairs
DPO: (state_text, better_action, worse_action) pairs from outcome comparison
```

현 에이전트 궤적이면 DPO pair 자동 생성 가능 (같은 state에서 outcome이 다른 action들을 비교).

---

## 9. 단계별 로드맵

### Phase 0 — 스키마·관찰자 (1주)
- [ ] 본 문서 확정 + review
- [ ] `anima/memory/trajectory.py` 구현 (async JSONL writer, feature-flagged)
- [ ] `Procedure.execute()` 훅 + planner candidate 훅
- [ ] `tools/validate_trajectories.py` 스키마 검증 CLI

### Phase 1 — 자동 수집 시작 (2주)
- [ ] feature flag ON → 1주일 수집 → 10만 step 목표
- [ ] 지연 보상 backfill 스크립트
- [ ] 에피소드 경계 자동 검출 + `episodes.jsonl`

### Phase 2 — 전문가 시연 (2주)
- [ ] `tools/record_demo.py` MVP (packet sniff + JSONL)
- [ ] 주 2회 플레이 세션 (시간 기록 30분/회)
- [ ] 세그먼트화 후처리 도구

### Phase 3 — 첫 학습 실험 (3주)
- [ ] `sell_to_vendor` 하나만 BC 모델 (Qwen 3B base) 파인튜닝
- [ ] shadow eval: 실제 선택은 현 procedure, 모델 예측과 비교
- [ ] 성공률 차이 보고

### Phase 4 — 확장 결정 지점
- Phase 3 결과 보고 전체 프로시저 확장 여부 판단.

---

## 10. 비-목표 (scope out)

이 파이프라인에서 **하지 않을 것**:

- 실시간 추론 통합 (훈련된 모델을 에이전트에 꽂는 것)
- RL 온라인 루프 수정 (기존 Q-table/UCB1은 별도로 운영)
- UI / 대시보드 (로그는 JSONL, 분석은 노트북)
- 멀티 에이전트 공유 (일단 단일 에이전트 기준)

---

## 11. 결정해야 할 항목 (열린 질문)

- **state의 표현 단위**: pure JSON vs. 프롬프트용 텍스트로 미리 렌더링? → 둘 다 보관 권장 (raw + rendered)
- **candidates 기록 여부**: planner 내부 훅 필요 — 수집 시작 시점에 결정
- **패킷 레벨까지 기록?**: primitive actions도 저장하면 tool-use agent 훈련에 활용 가능, 하지만 볼륨 증가 ~5×
- **보상 horizon**: 60s / 300s / episode 중 어떤 걸 기본으로? → 셋 다 저장하고 학습 시 선택
