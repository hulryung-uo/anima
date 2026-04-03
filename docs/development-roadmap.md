# Anima Development Roadmap

> 3단계(Phase)로 나누어 개발. 각 Phase는 이전 단계가 **안정적으로 동작**해야 다음으로 진행.

---

## Overview

```
Phase 1: Foundation                    Phase 2: Behavior Rules           Phase 3: Autonomous Agent
"서버와 통신하고 행동한다"              "규칙 문서 기반으로 동작한다"       "스스로 판단하고 상호작용한다"
                                                                        
┌─────────────────────┐   ┌──────────────────────────┐   ┌────────────────────────────┐
│ 패킷 통신           │   │ YAML/JSON 행동 규칙       │   │ RL + LLM 자율 판단          │
│ 이동/채광/제작/거래  │   │ 다중 스킬 프로파일        │   │ 목표 설정 + 계획 수립       │
│ 스킬/스탯 관리      │   │ 상황별 의사결정 테이블    │   │ 다중 에이전트 상호작용      │
│ 저널/인벤토리       │──→│ 자기 진단 + Claude Code   │──→│ 포럼/채팅 소통              │
│ 규칙 기반 루프      │   │ 위치 지식 자동 확장       │   │ 평판/관계 시스템            │
│ 웹 대시보드 모니터링 │   │ 실패 패턴 학습 (DB 기반)  │   │ 경제 참여 + 자체 개선       │
└─────────────────────┘   └──────────────────────────┘   └────────────────────────────┘
     현재 여기               다음 목표                      최종 목표
```

---

## Phase 1: Foundation — "서버와 통신하고 행동한다"

> 단일 에이전트가 UO 서버에서 기본 활동을 **안정적으로** 수행할 수 있는 상태.
> 규칙은 코드에 하드코딩. 목표는 "동작하는 것"을 확인하는 것.

### 1.1 현재 상태 (대부분 완료)

| 기능 | 상태 | 비고 |
|------|------|------|
| 서버 접속/로그인 | **완료** | 2-phase login, 패킷 코덱 |
| 패킷 수신/파싱 | **완료** | 50+ 패킷 타입 핸들링 |
| 월드 상태 추적 | **완료** | 모바일, 아이템, 지형 |
| 자기 상태 추적 | **완료** | HP/마나/스태미나, 스킬, 장비, 인벤토리 |
| 이동 (pathfinding) | **완료** | A*, 문 열기, walk confirm/deny |
| 채광 (mine_ore) | **완료** | 타일 탐색, 곡괭이 사용, depleted 처리 |
| 제련 (smelt_ore) | **완료** | 용광로 탐색, 제련 실행 |
| 제작 (craft) | **완료** | 대장장이 gump 파싱, 레시피 선택 |
| 도구 제작 (tinkering) | **완료** | 곡괭이/도구 자동 제작 |
| 벤더 판매 | **완료** | context menu, sell list, 거부 처리 |
| 벤더 구매 | **완료** | buy list, 필요 도구 자동 구매 |
| 은행 입금 | **완료** | bank deposit |
| 대화 (speech) | **완료** | unicode speech 송수신 |
| 스킬 컨트롤 | **부분** | skill lock (up/down/locked) 설정 |
| 스탯 컨트롤 | **부분** | stat lock 설정 |
| 저널 읽기 | **부분** | cliloc/speech 수신, journal DB 기록 |
| 웹 대시보드 | **완료** | WebSocket API, 실시간 상태, 조작 |
| TUI 모니터 | **완료** | WebSocket 기반 터미널 UI |
| 자기 진단 | **완료** | self_improve.py, supervisor.py |

### 1.2 남은 작업

#### P1-A: 안정성 개선 (Stability)

- [ ] **Planner stuck loop 완전 제거**: 모든 우선순위 분기에서 can_start=false + move=None일 때 fall-through 보장
- [ ] **NPC 인식 안정화**: planner 루프에서 unnamed NPC에 OPL 요청 (완료), 결과 대기 시간 추가
- [ ] **Gump 결과 파싱 강화**: 모든 craft/trade gump의 notice 영역 읽기 (craft_blacksmith 완료, make_tools 완료)
- [ ] **재접속 안정성**: 연결 끊김 시 state 보존 + 자동 재접속 (기본 구조 있음, 테스트 필요)

#### P1-B: 미완성 기능 보완

- [ ] **스킬 lock 완전 관리**: persona 기반 skill lock 설정 (mining up, others down)
- [ ] **스탯 lock 관리**: STR/DEX/INT lock 설정 (대장장이=STR up)
- [ ] **인벤토리 정리**: 불필요 아이템 버리기, 아이템 정리
- [ ] **장거리 이동**: waypoint 기반 이동 (Minoc → Britain 같은 도시 간 이동)
- [ ] **저널 활용**: 서버 메시지(cliloc) 기반 상황 판단 강화

#### P1-C: 게임플레이 루프 검증

전체 루프가 **1시간 이상 중단 없이** 동작하는지 검증:
```
mine → smelt → craft → sell → bank → buy tools → mine (반복)
```

**완료 기준**: supervisor.py로 1시간 실행 시 Level 2/3 개입 없이 정상 동작.

---

## Phase 2: Behavior Rules — "규칙 문서 기반으로 동작한다"

> 하드코딩된 규칙을 **외부 문서(YAML/JSON)**로 분리.
> 새로운 행동을 코드 변경 없이 문서만으로 추가할 수 있는 구조.
> Claude Code 자기 진단이 규칙 문서를 수정하여 행동을 개선.

### 2.1 행동 규칙 문서 시스템

#### 직업 프로파일 (Profession Profile)

```yaml
# personas/blacksmith.yaml
name: "Tormund"
profession: blacksmith

skills:
  primary: [blacksmithy, mining]
  secondary: [tinkering, arms_lore]
  locks:
    mining: up
    blacksmithy: up
    tinkering: up
    arms_lore: up
    # 나머지: locked

stats:
  priority: STR   # STR up, others locked

gameplay_loop:
  - mine_ore:
      location: [mine]
      until: "weight > 80% OR no_mineable_tile"
  - smelt_ore:
      location: [forge]
      until: "ore_count == 0"
  - craft_blacksmith:
      location: [forge, anvil]
      recipe_selection: highest_skill_match
      until: "ingot_count < 8"
  - sell_to_vendor:
      vendor_type: [weaponsmith, blacksmith, armorer]
      until: "crafted_count == 0"
  - bank_deposit:
      trigger: "gold > 200"
  - buy_tools:
      trigger: "NOT has_pickaxe"
      vendor_type: [tinker, provisioner]

fallback:
  no_resources: move_to_mine
  no_tools: buy_from_vendor
  stuck: move_to_nearest_known_location
```

#### 상황 판단 테이블 (Decision Table)

```yaml
# rules/vendor_routing.yaml
# 어떤 아이템을 어떤 벤더에게 팔지
sell_routing:
  - items: [cutlass, katana, longsword, scimitar, broadsword]
    vendor_type: [weaponsmith, blacksmith]
    fallback: arms_dealer
  - items: [ringmail_gloves, ringmail_tunic, chainmail]
    vendor_type: [armorer, blacksmith]
  - items: [iron_ingot, copper_ingot]
    vendor_type: [blacksmith, tinker]

buy_routing:
  - item: pickaxe
    vendor_type: [tinker, provisioner]
    max_price: 25
  - item: tongs
    vendor_type: [tinker]
    max_price: 15
```

#### 위치 지식 (Location Knowledge)

```yaml
# knowledge/locations/minoc.yaml
city: Minoc
locations:
  - name: "East Mine Entrance"
    x: 2556
    y: 499
    type: mine
    notes: "동쪽 광산 입구, 산악 타일 많음"
    verified: true
    last_verified: 2026-04-01
    
  - name: "Blacksmith (The Forgery)"
    x: 2471
    y: 564
    type: [forge, anvil, vendor]
    vendor_npcs:
      - name: "Indira the blacksmith"
        serial_pattern: "0x00002*"
        wander_range: 5
    notes: "NPC가 건물 안에서 돌아다님, 1층(z=5)"
```

### 2.2 규칙 엔진

현재 하드코딩된 planner 로직을 규칙 파일에서 읽어서 실행하는 엔진:

```
ProfessionProfile (YAML)
    ↓
RuleEngine.select_procedure(ctx)
    ├── gameplay_loop 순서대로 체크
    ├── trigger 조건 평가 (간단한 표현식)
    ├── location/vendor_type 매칭
    └── fallback 규칙 적용
    ↓
Procedure.run(ctx)
```

#### 구현 순서

- [ ] **2.2a YAML 프로파일 로더**: persona YAML에서 gameplay_loop, skill locks 읽기
- [ ] **2.2b 조건 표현식 평가기**: `"weight > 80%"`, `"gold > 200"` 같은 간단한 조건 파싱
- [ ] **2.2c 규칙 엔진**: gameplay_loop을 순회하며 procedure 선택
- [ ] **2.2d 벤더 라우팅 테이블**: 아이템→벤더 타입 매핑을 YAML에서 읽기
- [ ] **2.2e 위치 지식 자동 확장**: NPC 위치를 실측 데이터로 업데이트

### 2.3 다중 스킬 프로파일

하나의 에이전트가 여러 직업을 수행할 수 있도록:

```yaml
# personas/miner_lumberjack.yaml
profession: gatherer
gameplay_loops:
  mining:
    priority: 1
    skills: [mining]
    loop: [mine_ore, smelt_ore, sell_to_vendor]
    trigger: "has_pickaxe AND near_mine"
  lumberjacking:
    priority: 2
    skills: [lumberjacking, carpentry]
    loop: [chop_wood, make_boards, craft_carpentry, sell_to_vendor]
    trigger: "has_hatchet AND near_forest"
  fallback:
    action: move_to_nearest_resource
```

### 2.4 자기 진단 + Claude Code 연동

Phase 1의 `supervisor.py` + `self_improve.py`를 확장:

- [ ] **규칙 문서 수정 권한**: Claude Code가 YAML 규칙을 수정할 수 있도록 프롬프트 확장
- [ ] **위치 데이터 자동 보정**: NPC가 예상 위치에 없으면 실측 좌표로 YAML 업데이트
- [ ] **실패 패턴 → 규칙 추가**: 반복 실패 시 decision table에 새 규칙 추가
- [ ] **새 procedure 자동 생성**: 기존 procedure를 참고하여 새 행동 코드 생성

### 2.5 완료 기준

- 새 직업(예: woodworker)을 **YAML 파일만 추가**하여 동작시킬 수 있다
- supervisor가 실패 패턴을 감지하고 규칙 문서를 수정하여 자동 개선한다
- 24시간 연속 운영이 가능하다

---

## Phase 3: Autonomous Agent — "스스로 판단하고 상호작용한다"

> RL + LLM 기반 자율 판단. 다른 에이전트/플레이어와 상호작용.
> 규칙 문서 → 가이드라인으로 전환. 에이전트가 스스로 최적 행동을 학습.

### 3.1 목표 지향 계획 수립 (Goal-Oriented Planning)

Phase 2의 고정 루프 대신, 에이전트가 **목표를 설정하고 계획을 세우는** 구조:

```
LLM Think (매 5분 또는 계획 실패 시)
    ↓ "나는 대장장이로 성공하고 싶다. 현재 Mining 56, Blacksmithy 45."
    ↓ "주괴를 모아서 무기를 만들어 팔자. 먼저 mining을 올려야 한다."
    ↓
Plan: [mine_ore × N → smelt → craft → sell → repeat]
    ↓
SkillSelector (Utility AI + Q-learning)
    ├── plan의 다음 단계 우선
    ├── Q-value로 장소/방법 선택
    └── 실패 시 plan 수정 요청
    ↓
Procedure.run(ctx)
    ↓
결과 → Q-table 업데이트 + reflection 저장
```

#### 핵심 컴포넌트

- [ ] **Plan 데이터 구조**: goal, steps[], current_step, failure_count
- [ ] **LLM Planner**: 현재 상태 + Q-table 통계 + 과거 reflection을 바탕으로 계획 수립
- [ ] **DEPS 패턴 적용**: 실패 시 "왜 실패했는지" 설명을 LLM에 전달하여 계획 수정
- [ ] **Utility Scorer**: Q-value + plan_alignment + 재고 상태 + 위치 적합도로 행동 점수 산출

### 3.2 강화학습 (Reinforcement Learning)

Phase 2에서 축적된 action_logs 데이터를 활용:

#### Q-Learning 활성화

```python
# 현재: random.choice(available) — 비활성
# 목표: UCB1 + plan-aware selection

def select(ctx, available, plan):
    state = encode_state(ctx)  # location|presence|hp|inventory|current_goal
    
    for skill in available:
        q = get_q_value(state, skill.name)
        visits = get_visit_count(state, skill.name)
        plan_bonus = 10.0 if skill.name == plan.current_step else 0.0
        
        score = q + UCB_C * sqrt(log(total_visits) / (visits + 1)) + plan_bonus
        # ...
```

#### 다목적 보상 (Multi-Objective Reward)

```python
reward = (
    w_gold * gold_delta / 100 +      # 경제적 이득
    w_skill * skill_gain * 10 +       # 스킬 성장
    w_social * social_reward +         # 사회적 상호작용
    w_survival * survival_penalty      # 생존
)
# w_* 가중치는 LLM이 전략적으로 조정
```

#### 커리큘럼 학습 (Curriculum Learning)

```
Stage 1: 이동 + 관찰만                  ← "월드를 알아간다"
Stage 2: 자원 수집 (채광, 벌목)          ← "일하는 법을 배운다"
Stage 3: 가공 (제련, 판자)               ← "원재료를 처리한다"
Stage 4: 제작 (무기, 도구)               ← "물건을 만든다"
Stage 5: 거래 (판매, 구매)               ← "경제에 참여한다"
Stage 6: 사회 (대화, 포럼, 협업)         ← "커뮤니티에 참여한다"
```

진급 조건: 현재 stage의 핵심 스킬 visit_count ≥ N AND avg_reward > threshold

### 3.3 Reflection (자기 성찰)

Stanford Generative Agents + Reflexion 패턴 적용:

```
매 30분 또는 plan 완료/실패 시:
    ↓
최근 action_logs + journal 수집
    ↓
LLM Reflection Prompt:
  "최근 활동을 돌아보고, 배운 것을 정리해라.
   - 어떤 장소가 좋았나?
   - 어떤 행동이 효율적이었나?
   - 어떤 실패를 반복했나?
   - 다음에 다르게 할 것은?"
    ↓
Reflection 저장 (DB: reflections 테이블)
    ↓
다음 Think 프롬프트에 관련 reflection 주입
```

### 3.4 다중 에이전트 상호작용

#### 에이전트 간 인식

```
Agent A (miner):  "채광 중, 광석을 모으고 있다"
Agent B (smith):  "주괴가 필요하다, 누가 광석을 팔면 좋겠다"
    ↓
Agent B가 Agent A에게 말을 건다: "광석 팔 생각 없나?"
    ↓ (LLM 대화)
Agent A: "10개에 50골드면 팔지"
    ↓
Secure Trade 또는 vendor 거래
```

#### 구현 순서

- [ ] **3.4a 다중 접속**: 여러 에이전트가 동시에 서버 접속 (Orchestrator)
- [ ] **3.4b NPC 인식**: 다른 AI 에이전트를 일반 NPC처럼 인식하고 대화
- [ ] **3.4c LLM 대화**: speech 수신 → LLM 응답 생성 → speech 송신
- [ ] **3.4d 거래 상호작용**: player-to-player secure trade 구현
- [ ] **3.4e 관계 시스템**: 신뢰도, 호감도 DB 관리

#### 포럼/채팅 소통

이미 forum_client가 구현되어 있음. 확장:

- [ ] **포럼 읽기 → reflection**: 다른 에이전트의 포럼 글에서 정보 추출
- [ ] **경험 공유**: "Minoc 동쪽 광산이 고갈됐다" → 다른 에이전트가 회피
- [ ] **시장 정보**: "주괴 가격이 올랐다" → 채광 우선순위 상승
- [ ] **사회적 포스팅**: LLM이 일상 이야기, 거래 제안, 소문 등 생성

### 3.5 경제 참여

- [ ] **가격 인식**: 벤더 buy/sell 가격 DB 축적, 가격 트렌드 파악
- [ ] **이윤 최적화**: 제작 비용 vs 판매 가격 비교, 최적 레시피 선택
- [ ] **재고 관리**: 필요 물자 자동 확보, 잉여 자동 판매
- [ ] **에이전트 간 경제**: AI끼리 물물교환, 전문화 분업

### 3.6 완료 기준

- 에이전트가 **사람의 개입 없이** 목표를 설정하고 달성한다
- 여러 에이전트가 서로 대화하고 거래하며 분업한다
- 포럼에 의미 있는 글을 쓰고 다른 글에 반응한다
- 1주일 연속 운영이 가능하다

---

## 팀 관점 분류

각 Phase의 작업을 담당 영역별로 정리:

### Client / Protocol 팀
- P1: 재접속 안정성, 누락 패킷 처리
- P2: 새 패킷 타입 (secure trade, party system)
- P3: 다중 접속 관리, 패킷 큐 최적화

### Perception / World 팀
- P1: NPC 인식 안정화, 저널 파싱 강화
- P2: 위치 지식 자동 확장, 맵 데이터 활용
- P3: 다른 에이전트 인식, 관계 추적

### Planner / Brain 팀
- P1: stuck loop 제거, 게임플레이 루프 안정화
- P2: 규칙 엔진 구현, YAML 프로파일 로더
- P3: LLM Planner, GOAP, Utility Scorer, Plan 관리

### Skills / Procedures 팀
- P1: 기존 procedure 안정화, gump 파싱
- P2: YAML 기반 procedure 구성, 새 직업 스킬
- P3: RL 연동, 커리큘럼 학습

### Memory / Learning 팀
- P1: action_logs 축적, 기본 통계
- P2: 실패 패턴 DB, 위치 보정 데이터
- P3: Q-learning 활성화, reflection, 다목적 보상

### Social / Communication 팀
- P1: speech 송수신 기본
- P2: 포럼 자동 포스팅
- P3: LLM 대화, 관계 시스템, 포럼 소통, 경험 공유

### DevOps / Monitoring 팀
- P1: supervisor.py, self_improve.py, 웹 대시보드
- P2: Claude Code 자동 규칙 수정, 24시간 운영
- P3: 다중 에이전트 오케스트레이터, 모니터링 스케일링

---

## 실현 가능성 분석

### Phase 1: 높음 (기존 UO 봇 생태계에서 검증된 패턴)
- Razor Enhanced, EasyUO, ClassicAssist 등이 동일한 동작을 이미 구현
- Anima의 procedure 구조가 이미 대부분 동작 중
- **주요 리스크**: 상태 비동기화 (패킷 타이밍), gump 처리 취약성
- **해결**: 서버 틱 맞춤 패킷 지연 (350-650ms), 이벤트 기반 gump 대기

### Phase 2: 중간 (선례 있지만 자동 수정은 실험적)
- RimWorld의 ThinkTree/WorkGiver XML 시스템이 동일 패턴으로 성공
- CEL (Common Expression Language)로 조건 표현식 평가 — K8s에서 검증됨
- **주요 리스크**: Claude Code 자동 YAML 수정의 진동/과적합
- **해결**: Constitutional AI 가드레일, canary 배포, 변경 간격 제한

### Phase 3: 도전적 (연구 수준이지만 부분 구현 가능)
- Tabular Q-learning은 2,000 상태에서 충분 (function approx 불필요)
- LLM 계획은 Voyager/DEPS로 검증됨, 로컬 8B 모델로 가능
- **주요 리스크**: 50 에이전트 LLM 처리량, 메모리 폭발, 개성 수렴
- **해결**: dual-process (규칙 + LLM), 메모리 pruning, 대화 쿨다운

---

## 성공 지표 (Phase별)

| Phase | 지표 | 목표 |
|-------|------|------|
| **P1** | 무중단 운영 | 4시간+ |
| **P1** | Procedure 성공률 | >70% |
| **P1** | 시간당 골드 | >50g/hr |
| **P1** | 실패 복구 시간 | <60초 |
| **P2** | 새 직업 추가 시 코드 변경 | 0 lines |
| **P2** | 자동 규칙 수정 성공률 | >50% |
| **P2** | 연속 운영 | 24시간+ |
| **P2** | 사람 개입 빈도 | <1회/8시간 |
| **P3** | LLM 콜 비율 | 전체 결정의 <5% |
| **P3** | Plan 완수율 | >60% |
| **P3** | 대화 자연스러움 | 블라인드 테스트 >50% 통과 |
| **P3** | 연속 운영 | 1주일+ |

---

## 타임라인 (대략적 추정)

| Phase | 기간 | 핵심 마일스톤 |
|-------|------|--------------|
| **P1 완성** | 1-2주 | 4시간 무중단 게임플레이 루프 |
| **P2 시작** | 2-3주 | YAML 규칙 엔진 + CEL 조건 평가 |
| **P2 완성** | 4-6주 | 새 직업 YAML만으로 추가, 24시간 운영 |
| **P3-A** | 6-10주 | Q-learning 활성화 + LLM Plan + Reflection |
| **P3-D** | 10-14주 | 다중 에이전트 접속 + LLM 대화 + 거래 |
| **P3-E** | 14-20주 | 포럼 소통 + 자율 경제 + 1주일 운영 |

---

## 상세 문서

각 Phase의 구체적인 구현 계획, 리스크, 참고 자료:
- **[Phase 1 상세](phase1-foundation.md)** — 안정성, 패킷 타이밍, 테스트 전략
- **[Phase 2 상세](phase2-behavior-rules.md)** — YAML 규칙 엔진, CEL, Claude Code 자동 수정
- **[Phase 3 상세](phase3-autonomous-agent.md)** — RL, LLM 계획, Reflection, 다중 에이전트

---

## 핵심 참고 자료

### 게임 AI 아키텍처
- [RimWorld AI: How Pawns Think](https://github.com/roxxploxx/RimWorldModGuide/wiki/SHORTTUTORIAL:-How-Pawns-Think) — ThinkTree/WorkGiver 패턴
- [DwarfCorp AI](https://www.gamedeveloper.com/programming/how-we-developed-robust-ai-for-dwarfcorp) — GOAP+BT 하이브리드
- [GOBT](https://www.jmis.org/archive/view_article?pid=jmis-10-4-321) — BT + Utility AI

### LLM 에이전트
- [Voyager](https://voyager.minedojo.org/) — LLM 기반 오픈엔드 게임 에이전트
- [DEPS](https://arxiv.org/abs/2302.01560) — 실패 설명 기반 계획 수정
- [Generative Agents (Park et al.)](https://arxiv.org/abs/2304.03442) — 메모리 + Reflection + 계획
- [Reflexion](https://arxiv.org/abs/2303.11366) — 자기 성찰 기반 개선
- [A-MEM (NeurIPS 2025)](https://arxiv.org/abs/2502.12110) — Zettelkasten 에이전트 메모리
- [LLM Game Agent Survey](https://arxiv.org/abs/2404.02039) — 종합 서베이

### 강화학습
- [Neural MMO 2.0](https://arxiv.org/abs/2311.03736) — 대규모 게임 환경 RL
- [Syllabus](https://arxiv.org/html/2411.11318v1) — 자동 커리큘럼 설계
- [Hierarchical RL Macro Actions](https://link.springer.com/article/10.1007/s40747-025-01895-9)
- [Sparse Rewards Shaping (2025)](https://arxiv.org/html/2501.19128v4)

### 스케일링 / 안전
- [MegaAgent (ACL 2025)](https://aclanthology.org/2025.findings-acl.259.pdf) — 다중 에이전트 스케일링
- [Constitutional AI Guardrails](https://dev.to/zer0h1ro/7-layer-constitutional-ai-guardrails-preventing-agent-mistakes-15i5)
- [CEL (Common Expression Language)](https://cel.dev/) — 조건 표현식 엔진

### UO 봇 참고
- [Razor Enhanced](https://www.razorenhanced.net/) — 패킷 타이밍, 스크립팅
- [UO Outlands Scripts](https://outlands.uorazorscripts.com/scripts) — 커뮤니티 봇 패턴
