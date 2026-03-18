# Reinforcement Learning in Anima

## 핵심 컨셉

Anima의 RL은 **LLM-as-policy + Q-table contextual bandits** 구조다.

- 별도의 neural network policy가 없다
- LLM 자체가 policy 역할을 한다
- Q-table이 "어떤 상황에서 어떤 스킬이 좋았는지" 통계를 축적한다
- 축적된 통계가 LLM 프롬프트에 주입되어 더 나은 판단을 유도한다

```
┌─────────────────────────────────────────────┐
│  LLM (전략 결정)                              │
│  "Mining에 집중하자" "위험하니 도시로 가자"       │
│  ↑ Q-table 통계 + 메모리 주입                   │
├─────────────────────────────────────────────┤
│  Q-table SkillSelector (전술 결정)             │
│  state → available skills → UCB1 선택          │
│  실행 → 보상 관찰 → Q-value 업데이트            │
├─────────────────────────────────────────────┤
│  Skill (실행)                                 │
│  패킷 시퀀스 실행 → SkillResult 반환            │
└─────────────────────────────────────────────┘
```

---

## 1. State Encoding

게임 상태를 이산화된 문자열 키로 변환한다.

**구현**: `anima/skills/state.py` → `encode_state(ctx) -> str`

### State 구성 요소

| 요소 | 값 | 결정 방식 |
|---|---|---|
| location_type | `smithy`, `water`, `town`, `field` | 근처 오브젝트/NPC로 추론 |
| player_presence | `players`, `alone` | notoriety ≤ 6인 mobile 존재 여부 |
| enemy_presence | `enemies`, `safe` | notoriety 3,5,6인 mobile 존재 여부 |
| hp_level | `full`, `healthy`, `wounded`, `critical` | HP % 구간 |
| inventory_state | `empty`, `ore`, `ingots+heavy`, ... | 배낭 내 아이템 graphic 분석 |

### 예시

```
"smithy|alone|safe|full|ingots"     → 대장간, 혼자, 안전, 건강, 주괴 보유
"field|players|enemies|wounded|ore" → 필드, 플레이어 근처, 적 있음, 부상, 광석 보유
"town|alone|safe|full|empty"        → 마을, 혼자, 안전, 건강, 빈 배낭
```

### 설계 원칙

- **너무 세분화하면** Q-table이 sparse해져서 학습이 느려진다
- **너무 뭉치면** 상황 구분이 안 돼서 의미 없다
- 현재 5개 요소 × 각 2~5개 값 = 최대 ~400개 상태 조합 (현실적 범위)

---

## 2. Action Space

등록된 모든 스킬 중 **현재 실행 가능한 것들**이 action space다.

**구현**: `anima/skills/base.py` → `SkillRegistry.available_skills(ctx)`

각 스킬의 `can_execute(ctx)` 가 precondition을 자동 체크:
- 필요한 아이템이 배낭에 있는가?
- 필요한 오브젝트가 근처에 있는가?
- 필요한 UO 스킬 레벨을 충족하는가?
- 필요한 스탯 요건을 충족하는가?

**현재 등록된 스킬 (7개)**:

| 스킬 | 카테고리 | 주요 precondition |
|---|---|---|
| `heal_self` | combat | 붕대 + HP < 90% |
| `melee_attack` | combat | 근처에 적 + HP > 20% |
| `mine_ore` | gathering | 곡괭이 + 바위 근처 |
| `chop_wood` | gathering | 도끼 + 나무 근처 |
| `smelt_ore` | crafting | 광석 + 용광로 근처 |
| `buy_from_npc` | trade | 골드 + NPC 상인 근처 |
| `sell_to_npc` | trade | 아이템 + NPC 상인 근처 |

→ 대부분의 상태에서 1~3개만 실행 가능하므로 action space가 자연스럽게 제한된다.

---

## 3. Q-Learning

### Q-Table 저장

**구현**: `anima/memory/database.py` → `q_values` 테이블

```sql
CREATE TABLE q_values (
    agent_name TEXT,
    state_key TEXT,       -- encode_state() 결과
    action TEXT,          -- skill name
    q_value REAL,         -- 학습된 가치
    visit_count INTEGER,  -- 시도 횟수
    last_updated REAL
);
```

### 업데이트 규칙 (Bellman equation)

**구현**: `anima/skills/selector.py` → `SkillSelector.update()`

```
Q(s, a) ← Q(s, a) + α × (r + γ × max Q(s', a') − Q(s, a))

α = 0.1    학습률 (새 경험 반영 속도)
γ = 0.9    할인율 (미래 보상 중요도)
r = 스킬 실행 결과의 reward
s' = 스킬 실행 후 상태
```

### 흐름

```
1. state = encode_state(ctx)
2. available = registry.available_skills(ctx)
3. skill = selector.select(ctx, available)   ← UCB1
4. result = skill.execute(ctx)               ← 패킷 시퀀스 실행
5. selector.update(ctx, skill, result)       ← Q-value 갱신
6. memory_db.record_episode(...)             ← 에피소드 기록
```

---

## 4. 탐험 전략: UCB1

**구현**: `anima/skills/selector.py` → `SkillSelector.select()`

스킬 선택 시 **exploitation (높은 Q-value)** 과 **exploration (덜 시도한 스킬)** 을 밸런싱.

```
score(a) = Q(s, a) + C × √(ln(N) / n(a))

C = 1.41   탐험 상수 (√2, UCB1 표준)
N = 해당 상태의 총 방문 횟수
n(a) = 해당 스킬의 시도 횟수
```

### 동작 방식

| 상황 | 행동 |
|---|---|
| 한 번도 안 해본 스킬 | score = ∞ → **무조건 시도** |
| 여러 개 미시도 | 랜덤으로 하나 선택 |
| 모두 시도 완료 | Q-value 높은 쪽 선호, 탐험 보너스가 점차 줄어듦 |
| 충분히 학습된 후 | exploitation 위주 (가장 보상 높은 스킬 반복) |

### C 값의 의미

- **C가 크면**: 탐험 많이 함 (다양한 스킬 시도)
- **C가 작으면**: exploitation 위주 (알려진 최선만 반복)
- **C = 1.41 (√2)**: 이론적으로 후회(regret)를 최소화하는 값

---

## 5. Location-Activity Value Map

위치별로 어떤 활동이 보상이 좋았는지 추적한다.

**구현**: `anima/memory/database.py` → `location_values` 테이블

```sql
CREATE TABLE location_values (
    agent_name TEXT,
    region_x INTEGER,    -- world_x // 32
    region_y INTEGER,    -- world_y // 32
    activity TEXT,       -- skill name
    total_reward REAL,
    visit_count INTEGER,
    last_visited REAL
);
```

### Region 좌표

맵을 32×32 타일 격자로 나눈다 (`anima/skills/state.py` → `region_coords()`).

```
world (1434, 1699) → region (44, 53) = "Britain Bank 근처"
world (1000, 2000) → region (31, 62) = "남쪽 필드"
```

### 활용

- 스킬 실행 후 해당 region + activity의 보상 누적
- "이 지역에서 mining avg +6.2, smelting avg +4.0"
- LLM 프롬프트에 주입 → "어디로 가서 뭘 할지" 판단 보조

---

## 6. Reward Signals

### 스킬별 보상

각 스킬의 `execute()`가 `SkillResult.reward`를 반환:

| 스킬 | 성공 보상 | 실패 보상 | 보너스 |
|---|---|---|---|
| `mine_ore` | +5 + 광석량 | -1 | 스킬 상승 시 +3 |
| `chop_wood` | +5 + 통나무량 | -1 | |
| `smelt_ore` | +5 + 주괴×0.5 | -2 | |
| `heal_self` | +1 + HP회복×0.2 | -0.5 | |
| `melee_attack` | +15 (처치) | -5 (도주) | HP 손실 -0.3/HP |
| `buy_from_npc` | +1 | -1 | |
| `sell_to_npc` | +0.5 + 골드×0.1 | -1 | |

### 기존 행동 보상 (memory/rewards.py)

```python
REWARDS = {
    "goal_arrived": +10,       # 목적지 도착
    "goal_failed": -5,         # 목적지 실패
    "speech_responded": +3,    # 대화 응답 받음
    "walk_denied": -2,         # 벽에 부딪힘
    "new_place_visited": +5,   # 새 장소 방문
    "damage_taken": -10,       # 피해 받음
}
```

---

## 7. LLM 프롬프트 주입

**구현**: `anima/memory/retrieval.py` → `retrieve_context()`

LLM이 판단할 때 다음 RL 통계가 system prompt에 포함된다:

```
== Your Memory ==

Skill learning (smithy|alone|safe|full|ingots):
  - smelt_ore: Q=7.2 (12 tries)
  - mine_ore: Q=5.5 (8 tries)
  - sell_to_npc: Q=3.1 (4 tries)

This area (region 44,53):
  - smelt_ore: avg reward +4.8 (10 visits)
  - buy_from_npc: avg reward +1.2 (3 visits)

Past experience (exploring):
  - "mine_ore": 8/10 success (avg reward: +5.5)
  - "melee_attack": 3/5 success (avg reward: +2.1)
```

LLM은 이 통계를 보고:
- "제련이 보상이 높으니 광석을 더 모아오자"
- "이 지역에선 mining이 잘 되니 여기서 계속하자"
- "전투 성공률이 낮으니 피하자"

같은 **통계 기반 전략 판단**을 내린다.

---

## 8. Brain 통합

**구현**: `anima/brain/brain.py` → `_skill_action()`

Behavior Tree에서 SkillExec 노드가 RL 루프를 실행:

```
Selector (root)
├── Survival    HP<30% → flee/heal
├── Social      대화 → respond
├── Forum       포럼 → read/write
├── SkillExec   ← RL 스킬 선택 + 실행
│   ├── can_execute? → skill_registry 확인
│   ├── Q-table + UCB1 → 스킬 선택
│   ├── skill.execute() → 결과
│   └── Q-value 업데이트 + 에피소드 기록
└── Think       LLM 자율 판단 (이동, 탐험)
```

- SkillExec은 **10초 쿨다운**이 있어서 Think와 교대로 실행됨
- Think에서 LLM이 "Mining에 집중하자"고 결정하면, 다음 SkillExec 때 그 bias가 반영됨

---

## 9. 학습 흐름 예시

### 시나리오: Anima가 처음 Britain에 도착

```
Tick 1: Think → LLM "은행 근처를 구경하자" → go to bank
Tick 5: SkillExec → available: [buy_from_npc] → 한 번도 안 해봄 → 시도
         결과: success, reward +1.0
         Q(town|alone|safe|full|empty, buy_from_npc) = 0.1

Tick 10: Think → LLM "대장간 쪽으로 가보자" → go to blacksmith

Tick 15: SkillExec → available: [sell_to_npc] → 미시도 → 시도
          결과: success, reward +0.5
          Q(smithy|alone|safe|full|misc, sell_to_npc) = 0.05

... (100번째 스킬 실행 후) ...

Tick 500: SkillExec at mine
  available: [mine_ore, heal_self]
  Q(field|alone|safe|full|empty, mine_ore) = 4.8   (40회, 성공률 80%)
  Q(field|alone|safe|full|empty, heal_self) = 0.2   (3회)
  → UCB1: mine_ore score 5.1, heal_self score 2.8
  → mine_ore 선택 (exploitation 우세)
```

### 시나리오: 새로운 지역에서 탐험

```
새 region 방문 → location_values 없음
→ 모든 활동의 탐험 보너스 높음
→ 다양한 스킬 시도
→ 보상 축적 → 점차 최적 활동 수렴
```

---

## 10. 코드 파일 맵

| 역할 | 파일 | 핵심 함수/클래스 |
|---|---|---|
| State encoding | `anima/skills/state.py` | `encode_state()`, `region_coords()` |
| Skill selection | `anima/skills/selector.py` | `SkillSelector.select()`, `.update()` |
| Skill interface | `anima/skills/base.py` | `Skill`, `SkillResult`, `SkillRegistry` |
| Q-table DB | `anima/memory/database.py` | `get_q_values()`, `update_q_value()` |
| Location values | `anima/memory/database.py` | `update_location_value()`, `get_best_locations()` |
| LLM prompt injection | `anima/memory/retrieval.py` | `retrieve_context()` |
| Brain integration | `anima/brain/brain.py` | `_skill_action()` |
| Reward definitions | `anima/memory/rewards.py` | `REWARDS` dict |

---

## 11. 미구현 / 향후 계획

| 항목 | 상태 | 설명 |
|---|---|---|
| Goal Sequence Learning | 미구현 | `goal_transitions` 테이블: "A 다음 B가 좋았다" |
| Temporal Decay | 미구현 | 오래된 경험의 가중치 감소 |
| LLM → RL Bias | 미구현 | LLM 전략 → SkillSelector 가중치 조정 |
| Multi-agent Learning | 미구현 | 다른 에이전트 경험 공유 (포럼 경유) |
| Hyperparameter Tuning | 미구현 | α, γ, C 값 자동 조정 |
