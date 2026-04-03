# Phase 3: Autonomous Agent — 상세 계획

> "스스로 판단하고, 학습하고, 상호작용한다"

---

## 1. 실현 가능성 분석

### Q-Learning 적합성

현재 설계:
- 상태 공간: ~400개 (5요소 × 2-5값) → 목표 추가 시 ~2,000개
- 행동 공간: ~20개 (procedure 수)
- Q-table 크기: 2,000 × 20 = 40,000 엔트리

**결론: Tabular Q-learning이 충분함.** DQN 같은 function approximation이 불필요한 수준.

수렴 속도 추정:
- 각 state-action 쌍 10-30회 방문 필요
- 일반적 상태에 대해 10-20시간 플레이로 수렴
- 희귀 상태 (던전, 전투)는 수렴에 더 오래 걸림

> **참고**: [Comprehensive Survey of RL (2025)](https://arxiv.org/pdf/2411.18892) — tabular vs function approximation 비교

### LLM 추론 비용/지연

| 모델 | 지연 (로컬) | 품질 | 사용 시나리오 |
|------|------------|------|-------------|
| gemma3:4b (Tier 2) | 50-200ms | 구조화된 프롬프트에 적합 | 빠른 판단, 아이템 평가 |
| llama3.1:8b (Tier 3) | 500ms-3s | 대화, 계획 수립에 양호 | 전략 결정, 대화 |
| Claude Haiku (cloud) | 200-500ms | 우수 | 품질 중요 판단의 fallback |

Brain tick = 200ms. Tier 2 (100ms)는 1 tick 안에 가능. Tier 3 (1-3s)는 5-15 tick 블로킹하지만, 전략 결정은 저빈도이므로 허용.

**50 에이전트 스케일링 문제**:
- 로컬 Ollama 1GPU = 순차 처리 → 4B 모델, 분당 50콜이면 콜당 ~1.2초 → 가능하지만 빡빡함
- 해결: LLM 콜 빈도를 실제로 낮게 유지 (전체 결정의 <5%)

> **참고**: [LLM Agent Latency (ICLR 2025)](https://openreview.net/pdf?id=0iLbiYYIpC) — dual-process (빠른 규칙 + 느린 LLM) 권장
> **참고**: [Token Cost at Scale](https://medium.com/@klaushofenbitzer/token-cost-trap-why-your-ai-agents-roi-breaks-at-scale-and-how-to-fix-it-4e4a9f6f5b9a)

### Generative Agents 실사용 분석

Stanford Smallville 시뮬레이션 (25 에이전트, 2일):

**성공한 점**:
- Memory stream + reflection + planning의 3단 구조가 설득력 있는 행동 생성
- 에이전트 간 자연스러운 대화와 관계 형성

**실패 사례 (커뮤니티 재구현에서)**:
1. **메모리 폭발**: 48시간 후 retrieval 지연이 LLM 추론보다 병목
2. **환각 사회 역학**: 에이전트가 일어나지 않은 사건/관계를 꾸며냄. Reflection이 작은 오류를 증폭
3. **개성 수렴**: 강한 성격 제약 없으면 모든 에이전트가 비슷해짐 → Big Five 성격 파라미터 필수
4. **비용**: 원본은 GPT-4 사용, 2일 시뮬레이션에 $50-150. Ollama 로컬이면 비용 0이지만 GPU 시간 제약
5. **무한 대화**: 대화 길이 제한 없으면 두 에이전트가 끝없이 대화 → 쿨다운 필수

> **참고**: [Generative Agents (Park et al.)](https://arxiv.org/abs/2304.03442)
> **참고**: [MegaAgent: Multi-Agent LLM Scaling (ACL 2025)](https://aclanthology.org/2025.findings-acl.259.pdf)

---

## 2. 목표 지향 계획 수립 (Goal-Oriented Planning)

### 2.1 Plan 데이터 구조

```python
@dataclass
class Plan:
    goal: str                      # "대장장이로 100골드 벌기"
    steps: list[PlanStep]          # 계획 단계들
    current_step: int = 0
    created_at: float = 0.0
    failure_count: int = 0
    max_failures: int = 3          # 3회 실패 시 LLM 재계획
    source: str = "llm"           # "llm" | "rule" | "reflection"

@dataclass
class PlanStep:
    procedure: str                 # "mine_ore"
    params: dict = field(default_factory=dict)
    expected_outcome: str = ""     # "광석 10개 이상 확보"
    failure_reason: str = ""       # 실패 시 이유 (DEPS 패턴)
```

### 2.2 LLM Planner — DEPS 패턴 적용

**DEPS (Describe, Explain, Plan, Select)** 핵심: 실패 시 "왜 실패했는지"를 LLM에 전달.

```
현재 상태:
  위치: Minoc Mine, 금화 5g, 곡괭이 있음, 주괴 0개
  최근 실패: "smelt_ore 실패 — 근처에 용광로 없음"
  Q-table 통계: mine_ore avg_reward=4.2, smelt_ore avg_reward=3.8

이전 계획:
  [mine_ore → smelt_ore → craft → sell]
  실패 지점: step 2 (smelt_ore) — 용광로가 이 위치에 없음

실패 원인 분석:
  mine에서 smelt로 전환하려면 용광로가 있는 곳으로 이동해야 함.
  현재 계획에 이동 단계가 빠져있음.

수정된 계획을 JSON으로 출력:
```

→ LLM 출력:
```json
{
  "goal": "주괴를 만들어 무기 제작 후 판매",
  "steps": [
    {"procedure": "mine_ore", "expected_outcome": "광석 5개 이상"},
    {"procedure": "move_to_location", "params": {"type": "forge"}, "expected_outcome": "용광로 근처 도착"},
    {"procedure": "smelt_ore", "expected_outcome": "주괴 5개 이상"},
    {"procedure": "craft_blacksmith", "expected_outcome": "무기 1개 이상"},
    {"procedure": "sell_to_vendor", "expected_outcome": "골드 증가"}
  ]
}
```

> **참고**: [DEPS: Describe, Explain, Plan and Select](https://arxiv.org/abs/2302.01560)

### 2.3 Utility Scorer — BT + Utility AI 하이브리드

현재 Planner의 고정 우선순위 대신, 동적 점수 계산:

```python
def score_procedure(proc, ctx, plan, q_table):
    score = 0.0

    # 1. Q-value (학습된 기대 보상)
    state = encode_state(ctx)
    q = q_table.get(state, proc.name, default=0.0)
    score += q * 1.0

    # 2. Plan alignment (현재 계획과 일치하면 보너스)
    if plan and plan.steps[plan.current_step].procedure == proc.name:
        score += 10.0

    # 3. 상황 긴급도 (inventory/weight/hp 기반)
    if proc.name == "heal_self" and ctx.perception.self_state.hits_ratio < 0.3:
        score += 100.0  # 생존은 항상 최우선
    if proc.name == "smelt_ore" and inventory_weight_ratio(ctx) > 0.85:
        score += 20.0  # 과적 시 제련 긴급

    # 4. 탐색 보너스 (UCB1)
    visits = q_table.visit_count(state, proc.name)
    total = q_table.total_visits(state)
    ucb = UCB_C * sqrt(log(total + 1) / (visits + 1))
    score += ucb

    return score
```

> **참고**: [GOBT: Goal-Oriented Utility-Based Planning in Behavior Trees](https://www.jmis.org/archive/view_article?pid=jmis-10-4-321)

---

## 3. 강화학습 (RL) 상세 설계

### 3.1 Q-Learning 활성화 계획

현재 상태: `anima/skills/selector.py`에서 `random.choice()` → Q-learning disabled.

활성화 단계:
1. **Phase 2 데이터 축적**: 규칙 기반 운영 중 action_logs에 (state, action, reward) 축적
2. **Q-table 초기화**: 축적된 데이터에서 초기 Q-value 계산 (cold-start 해결)
3. **UCB1 탐색 활성화**: `SkillSelector.select()`에서 Q-value + UCB1 사용
4. **Plan-aware 선택**: Plan의 현재 step에 대한 보너스 추가

### 3.2 Cold-Start 해결

| 방법 | 설명 | 구현 난이도 |
|------|------|------------|
| **도메인 지식 시드** | 기대 보상으로 Q-table 사전 채움 (mine=+5, sell=+gold/10) | 쉬움 |
| **에이전트 간 Q-table 공유** | 경험 많은 에이전트의 Q-value 복사 | 쉬움 (SQL 복사) |
| **Phase 2 데이터 부트스트랩** | 규칙 기반 운영 로그에서 Q-value 역산 | 중간 |
| **행동 복제 (Imitation Learning)** | 사람 플레이 패킷 로그에서 state-action 쌍 추출 | 중간 |

**추천**: 도메인 지식 시드 + Phase 2 부트스트랩 조합

```python
# Q-table 초기화 스크립트
async def bootstrap_q_table(db: MemoryDB, agent: str):
    """Phase 2의 action_logs에서 Q-value 초기값 계산"""
    rows = await db.execute_fetchall("""
        SELECT procedure, result, AVG(CASE WHEN result='success' THEN 1.0 ELSE -1.0 END) as avg_r
        FROM action_logs WHERE agent = ?
        GROUP BY procedure
    """, (agent,))
    for proc, _, avg_r in rows:
        await db.execute("""
            INSERT INTO q_values (agent_name, state_key, action, q_value, visit_count)
            VALUES (?, '*', ?, ?, 10)  -- '*' = 모든 상태의 기본값
        """, (agent, proc, avg_r * 5))
```

> **참고**: [Behavioral Cloning in VizDoom (2024)](https://arxiv.org/abs/2401.03993)

### 3.3 다목적 보상 (Multi-Objective)

```python
def compute_reward(result: ProcedureResult, ctx: AgentContext, weights: RewardWeights) -> float:
    """다목적 보상 계산. weights는 LLM Think에서 조정."""
    reward = 0.0

    # 경제적 이득
    if result.gold_changed:
        reward += weights.gold * result.gold_changed / 100

    # 스킬 성장 (높은 스킬에서의 gain이 더 가치 있음)
    if result.details and result.details.get("skill_gain"):
        gain = result.details["skill_gain"]
        current = result.details.get("skill_value", 50)
        # 90.0에서의 0.1 gain = 50.0에서의 1.0 gain과 동가치
        skill_reward = gain * (1 + current / 50)
        reward += weights.skill * skill_reward

    # 사회적 상호작용
    if result.details and result.details.get("social_interaction"):
        reward += weights.social * 2.0

    # 생존 페널티
    ss = ctx.perception.self_state
    if ss.hits < ss.hits_max * 0.3:
        reward += weights.survival * -5.0

    # 실패 페널티
    if not result.success:
        reward -= 1.0

    return reward

@dataclass
class RewardWeights:
    gold: float = 1.0
    skill: float = 2.0
    social: float = 1.0
    survival: float = 3.0
```

LLM이 전략에 따라 가중치 조정:
- "스킬 올리기에 집중" → `weights.skill = 5.0, weights.gold = 0.5`
- "돈 벌기에 집중" → `weights.gold = 3.0, weights.skill = 1.0`

### 3.4 커리큘럼 학습

```python
class CurriculumManager:
    STAGES = [
        CurriculumStage(
            name="movement",
            required_skills=set(),
            available_procedures=["move_to_location"],
            advance_condition="unique_locations_visited >= 5",
        ),
        CurriculumStage(
            name="gathering",
            required_skills=set(),
            available_procedures=["mine_ore", "chop_wood", "move_to_location"],
            advance_condition="total_resources_gathered >= 20",
        ),
        CurriculumStage(
            name="processing",
            available_procedures=["mine_ore", "chop_wood", "smelt_ore", "make_boards", "move_to_location"],
            advance_condition="total_processed >= 10",
        ),
        CurriculumStage(
            name="crafting",
            available_procedures=["*_except_social"],  # 거래/사회 제외 전부
            advance_condition="total_crafted >= 5",
        ),
        CurriculumStage(
            name="trading",
            available_procedures=["*_except_social"],
            advance_condition="total_gold_earned >= 100",
        ),
        CurriculumStage(
            name="social",
            available_procedures=["*"],  # 전부
            advance_condition=None,  # 최종 단계
        ),
    ]
```

진급 조건은 action_logs에서 자동 측정. 새 에이전트는 Stage 1부터, 경험 있는 에이전트는 Q-table 상태에 따라 적절한 Stage에서 시작.

> **참고**: [Syllabus: Portable Curricula for RL (2024)](https://arxiv.org/html/2411.11318v1)
> **참고**: [Skill-Based Bayesian Networks for Curriculum (2025)](https://arxiv.org/html/2502.15662v1)

---

## 4. Reflection (자기 성찰) 시스템

### 4.1 메모리 구조

Stanford Generative Agents의 3단 메모리:
1. **Memory Stream**: 시간순 관찰 기록 → Anima의 `action_logs` + `journal`
2. **Reflection**: 관찰을 종합한 고수준 인사이트 → **새로 구현 필요**
3. **Planning**: Reflection을 바탕으로 한 계획 → Phase 3의 Plan 구조

```sql
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    agent_name TEXT NOT NULL,
    topic TEXT NOT NULL,       -- "mining_efficiency", "vendor_knowledge", "combat_safety"
    insight TEXT NOT NULL,     -- "Minoc 동쪽 광산이 서쪽보다 광석이 풍부하다"
    confidence REAL DEFAULT 0.5,  -- 0.0-1.0
    source_count INTEGER DEFAULT 1, -- 이 인사이트를 뒷받침하는 관찰 수
    last_validated REAL          -- 마지막으로 검증된 시간
);
```

### 4.2 Reflection 프롬프트

```
매 30분 또는 plan 완료/실패 시 실행:

시스템 프롬프트:
  "너는 {name}이다. {personality_description}
   최근 활동을 돌아보고, 배운 것을 정리해라."

유저 프롬프트:
  "## 최근 30분 활동 요약
   - mine_ore: 15회 성공, 3회 실패 (depleted 2, too_far 1)
   - smelt_ore: 8회 성공
   - craft_blacksmith: 2회 성공, 5회 실패 (insufficient metal 5)
   - sell_to_vendor: 0회 (벤더 못 찾음)
   
   ## 이동 경로
   - Minoc East Mine → Minoc Forge → Minoc East Mine (3왕복)
   - Minoc Blacksmith 방문 → 벤더 없음 → Minoc Tanner 방문 → 거부됨
   
   ## 골드 변화: +0 (판매 실패)
   ## 스킬 변화: Mining 56.2 → 57.1 (+0.9), Blacksmithy 45.0 → 45.3 (+0.3)
   
   다음 질문에 JSON으로 답해라:
   1. 잘 된 것은? (what_worked)
   2. 문제가 된 것은? (problems) 
   3. 다음에 다르게 할 것은? (next_actions)
   4. 새로 배운 사실은? (new_knowledge)"
```

→ LLM 응답 예시:
```json
{
  "what_worked": "채광과 제련은 안정적. Mining 스킬이 꾸준히 오르고 있다.",
  "problems": "제작 시 insufficient metal 반복 — 주괴 수량 체크가 부정확하거나 colored metal을 counting하고 있을 수 있다. 벤더 판매 실패 — Minoc Blacksmith 위치에 실제 벤더가 없다.",
  "next_actions": "제작 전 주괴 종류 확인. 벤더 위치 데이터 업데이트 필요. 판매를 위해 다른 도시(Britain) 탐색 고려.",
  "new_knowledge": "Minoc Tanner는 무기를 사지 않는다. Minoc Blacksmith 위치(2471,564)에는 벤더가 없을 때가 있다."
}
```

### 4.3 Reflection → 행동 반영

Reflection 결과를 다음 Think 프롬프트에 주입:
```python
async def build_think_context(ctx):
    # 기존: Q-table 통계 + 위치 정보
    # 추가: 최근 reflection 결과
    reflections = await db.execute_fetchall(
        "SELECT insight FROM reflections WHERE agent_name=? ORDER BY timestamp DESC LIMIT 5",
        (agent_name,),
    )
    return f"""
    최근 깨달은 것들:
    {chr(10).join(f'- {r[0]}' for r in reflections)}
    """
```

> **참고**: [Reflexion: Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)
> **참고**: [A-MEM: Agentic Memory (NeurIPS 2025)](https://arxiv.org/abs/2502.12110)

---

## 5. 다중 에이전트 시스템

### 5.1 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                  Orchestrator                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│  │ Agent #1 │ │ Agent #2 │ │ Agent #3 │ ...        │
│  │ Miner    │ │ Smith    │ │ Trader   │            │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘            │
│       │             │            │                   │
│  ┌────▼─────────────▼────────────▼─────────────┐    │
│  │         Shared Infrastructure                │    │
│  │  LLM Pool │ Shared Q-table │ Forum Bridge   │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────────│───────────────────────────┘
                           │ (개별 TCP 접속)
                    ┌──────▼──────┐
                    │  UO Server  │
                    └─────────────┘
```

### 5.2 에이전트 간 소통 채널

| 채널 | 범위 | 지연 | 용도 |
|------|------|------|------|
| **In-game speech** | 주변 8타일 | 실시간 | 근거리 대화, 거래 제안 |
| **Forum posting** | 전체 | 비동기 (30분+) | 시장 정보, 경험 공유, 일기 |
| **Shared DB (reflections)** | 전체 | 즉시 | 학습 데이터 공유 |
| **Q-table 공유** | 전체 | 주기적 동기화 | 행동 지식 공유 |

### 5.3 LLM 대화 시스템

```python
async def handle_speech(ctx, speaker_name, text):
    """다른 에이전트/플레이어가 말했을 때 처리"""
    
    # Tier 1: 패턴 매칭 (즉시, LLM 불필요)
    if is_greeting(text):
        await say(ctx, random.choice(GREETING_RESPONSES))
        return
    
    # Tier 2: 간단한 대화 (100ms)
    if is_simple_question(text):
        response = await ctx.llm.chat([
            {"role": "system", "content": f"너는 {persona.name}, {persona.title}. 짧게 답해라."},
            {"role": "user", "content": f"{speaker_name}: {text}"},
        ], model="gemma3:4b")
        await say(ctx, response.text)
        return
    
    # Tier 3: 복잡한 대화 (1-3s) 
    context = await build_conversation_context(ctx, speaker_name)
    response = await ctx.llm.chat([
        {"role": "system", "content": persona_system_prompt},
        *context.messages,
        {"role": "user", "content": f"{speaker_name}: {text}"},
    ], model="llama3.1:8b")
    await say(ctx, response.text)
```

**AI-AI 대화 안전 장치**:
- 대화당 최대 5턴
- 같은 에이전트와 10분 쿨다운
- 전체 LLM 대화 콜 분당 최대 3회

### 5.4 거래 상호작용

UO Secure Trade 패킷 구현 필요:
- 0x6F SecureTrade: 거래 창 열기/닫기/확인
- 아이템을 거래 창에 넣기/빼기
- 양측 모두 확인해야 거래 성립

### 5.5 관계 시스템

```sql
CREATE TABLE IF NOT EXISTS relationships (
    agent_name TEXT NOT NULL,
    target_serial INTEGER NOT NULL,
    target_name TEXT NOT NULL,
    disposition REAL DEFAULT 0.0,    -- -1.0 (적대) ~ 1.0 (우호)
    trust REAL DEFAULT 0.5,          -- 0.0 ~ 1.0
    trade_count INTEGER DEFAULT 0,
    last_interaction REAL,
    notes TEXT DEFAULT '',           -- LLM이 생성한 메모
    PRIMARY KEY (agent_name, target_serial)
);
```

Disposition 변화:
- 거래 성공: +0.1
- 대화: +0.05
- 거래 거부: -0.05
- 공격: -0.5

---

## 6. 포럼/채팅 소통 확장

### 6.1 정보 추출

다른 에이전트의 포럼 글에서 유용한 정보 자동 추출:

```python
async def extract_forum_knowledge(ctx, post_title, post_body):
    """포럼 글에서 게임 지식 추출"""
    prompt = f"""이 UO 게임 포럼 글에서 유용한 정보를 추출해라:
    제목: {post_title}
    본문: {post_body}
    
    JSON으로 답해라:
    - locations: [{{name, x, y, info}}] — 언급된 장소
    - prices: [{{item, price, vendor}}] — 가격 정보
    - warnings: [{{text}}] — 위험/주의사항
    - tips: [{{text}}] — 유용한 팁
    """
    response = await ctx.llm.chat([...])
    knowledge = json.loads(response.text)
    
    # 추출된 지식을 reflection DB에 저장
    for loc in knowledge.get("locations", []):
        await save_reflection(ctx, topic="location", insight=f"{loc['name']}은 ({loc['x']},{loc['y']})에 있다. {loc['info']}")
```

### 6.2 경험 공유 패턴

```
Agent A (miner): [채광 중 광산 고갈 감지]
    → Forum: "Minoc 동쪽 광산이 한동안 고갈 상태입니다. 다른 광산을 찾아보세요."
    
Agent B (miner): [포럼 읽기 → reflection]
    → Reflection: "Minoc 동쪽 광산이 고갈됐다고 한다. 서쪽 광산으로 가보자."
    → Plan 수정: move_to "West Mine"
```

---

## 7. 대안적 접근법

### 7.1 Hierarchical RL (Options Framework)

현재 Procedure가 이미 비공식적 Option:
- **Initiation set** = `can_start()` 
- **Policy** = `execute()`
- **Termination** = procedure 완료/실패

공식화 이점: Procedure 시퀀스의 Q-value를 학습 가능 ("mine_ore → smelt_ore" 시퀀스의 가치)

```python
# goal_transitions 테이블 활성화
# (state_before, procedure_sequence, state_after, total_reward) 기록
# → 최적 시퀀스 학습
```

> **참고**: [Hierarchical RL with Macro Actions (2025)](https://link.springer.com/article/10.1007/s40747-025-01895-9)

### 7.2 World Model (간단한 전이 모델)

DreamerV3 수준의 학습된 world model은 과도하지만, **수작업 전이 모델**은 유용:

```yaml
# knowledge/transitions.yaml
transitions:
  mine_ore:
    precondition: {has_pickaxe: true, near_mine: true}
    effects: {ore_count: "+1-3", weight: "+10-30"}
  smelt_ore:
    precondition: {ore_count: ">0", near_forge: true}
    effects: {ore_count: "0", ingot_count: "+ore/2"}
  craft_blacksmith:
    precondition: {ingot_count: ">=8", near_anvil: true}
    effects: {ingot_count: "-8", crafted_count: "+1"}
```

이걸로 GOAP-style 계획이 가능: "골드 100 필요" → 역방향 추론 → sell → craft → smelt → mine.

> **참고**: [DreamerV3 (Nature 2025)](https://www.nature.com/articles/s41586-025-08744-2) — 학습 world model은 연속 상태에 적합

### 7.3 Imitation Learning

사람 플레이 또는 잘 동작하는 봇의 패킷 로그에서 학습:

```python
# 로그에서 (state, action) 쌍 추출
for entry in action_logs:
    state = encode_state_from_log(entry)
    action = entry.procedure
    reward = 1.0 if entry.result == "success" else -0.5
    
    # Q-table 초기값으로 사용
    q_table.update(state, action, reward, alpha=0.3)
```

> **참고**: [EVOLUTE: Human-Like Game Agents (AAAI 2025)](https://ojs.aaai.org/index.php/AAAI/article/view/35305/37460)

---

## 8. 성공 지표

| 지표 | 목표 | 측정 방법 |
|------|------|-----------|
| Q-table 수렴 | 일반 상태 Q-value 분산 <10% (1000회 후) | Q-value 통계 분석 |
| LLM 콜 비율 | 전체 결정의 <5% | Tier 2+3 콜 수 / 총 결정 수 |
| Plan 완수율 | 생성된 계획의 >60% 완수 | plan 완료/생성 비율 |
| 에이전트 행동 다양성 (다중) | Gini coefficient >0.3 | 에이전트별 활동 히스토그램 비교 |
| 대화 자연스러움 | 블라인드 테스트 통과율 >50% | 사람이 AI/사람 구분 |
| 연속 운영 | 1주일+ | supervisor 로그 |
| 에이전트 간 거래 성공 | >1회/시간 (10 에이전트) | trade 로그 |

---

## 9. 구현 순서 (Phase 3 내부)

```
P3-A: RL 기반 (4주)
  1. Q-learning 활성화 (selector.py)
  2. Phase 2 데이터로 Q-table 부트스트랩
  3. 다목적 보상 함수 구현
  4. 커리큘럼 매니저

P3-B: LLM 계획 (3주)
  5. Plan 데이터 구조 + 저장
  6. DEPS 패턴 LLM planner
  7. Utility Scorer (Q + Plan + 상황)

P3-C: Reflection (2주)
  8. reflections 테이블 + LLM reflection 프롬프트
  9. Reflection → Think 프롬프트 주입
  10. 포럼 글에서 지식 추출

P3-D: 다중 에이전트 (4주)
  11. Orchestrator (다중 접속 관리)
  12. LLM 대화 시스템 (Tier 1/2/3)
  13. Secure Trade 패킷 구현
  14. 관계 시스템

P3-E: 통합 + 안정화 (3주)
  15. 포럼 경험 공유
  16. Q-table 에이전트 간 동기화
  17. 10 에이전트 스케일 테스트
  18. 1주일 연속 운영 테스트
```

---

## 10. 참고 자료 종합

### 핵심 논문
- [Voyager (2023)](https://voyager.minedojo.org/) — LLM 기반 오픈엔드 게임 에이전트, 스킬 라이브러리
- [DEPS (NeurIPS 2023)](https://arxiv.org/abs/2302.01560) — 실패 설명 기반 재계획
- [Generative Agents (2023)](https://arxiv.org/abs/2304.03442) — 메모리 스트림 + reflection + 계획
- [Reflexion (NeurIPS 2023)](https://arxiv.org/abs/2303.11366) — 자기 성찰 기반 학습
- [A-MEM (NeurIPS 2025)](https://arxiv.org/abs/2502.12110) — Zettelkasten 스타일 에이전트 메모리

### 게임 AI 설계
- [GOBT (2024)](https://www.jmis.org/archive/view_article?pid=jmis-10-4-321) — BT + Utility AI 하이브리드
- [DwarfCorp AI](https://www.gamedeveloper.com/programming/how-we-developed-robust-ai-for-dwarfcorp) — GOAP+BT 하이브리드
- [LLM Game Agent Survey (2024)](https://arxiv.org/abs/2404.02039) — LLM 게임 에이전트 종합 서베이

### RL 관련
- [Neural MMO 2.0 (2023)](https://arxiv.org/abs/2311.03736) — 대규모 게임 환경 RL
- [Syllabus (2024)](https://arxiv.org/html/2411.11318v1) — 자동 커리큘럼 설계
- [Hierarchical RL Macro Actions (2025)](https://link.springer.com/article/10.1007/s40747-025-01895-9)
- [Sparse Rewards Shaping (2025)](https://arxiv.org/html/2501.19128v4)
- [DreamerV3 (Nature 2025)](https://www.nature.com/articles/s41586-025-08744-2) — World Model

### 스케일링 / 안전
- [MegaAgent (ACL 2025)](https://aclanthology.org/2025.findings-acl.259.pdf) — 다중 에이전트 LLM 스케일링
- [LLM Agent Latency (ICLR 2025)](https://openreview.net/pdf?id=0iLbiYYIpC) — 지연 시간 문제
- [Constitutional AI Guardrails](https://dev.to/zer0h1ro/7-layer-constitutional-ai-guardrails-preventing-agent-mistakes-15i5)
- [EVOLUTE (AAAI 2025)](https://ojs.aaai.org/index.php/AAAI/article/view/35305/37460) — 인간 모방 게임 에이전트
