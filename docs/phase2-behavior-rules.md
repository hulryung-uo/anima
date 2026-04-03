# Phase 2: Behavior Rules — 상세 계획

> "규칙 문서 기반으로 동작한다"

---

## 1. 목표와 실현 가능성

### 핵심 목표
하드코딩된 planner 로직을 **외부 YAML/JSON 문서**로 분리하여, 코드 변경 없이 새 직업/행동을 추가할 수 있는 구조.

### 실현 가능성 평가

**긍정적 근거**:
- **RimWorld의 ThinkTreeDef/WorkGiverDef 시스템**이 이미 같은 패턴으로 성공적으로 동작 중. XML로 NPC 행동 정의 → C# 코드는 실행만 담당. ([RimWorld AI: How Pawns Think](https://github.com/roxxploxx/RimWorldModGuide/wiki/SHORTTUTORIAL:-How-Pawns-Think))
- **Factorio의 Prototype 시스템**도 Lua 데이터 테이블로 모든 엔티티 행동을 파라미터화. ([Factorio Prototype Docs](https://lua-api.factorio.com/latest/prototypes.html))
- 현재 Anima의 procedure 구조가 이미 분리에 적합 — `can_start()` (조건), `execute()` (실행)

**리스크**:
- 조건 표현식 평가기 구현 복잡도
- YAML 스키마가 게임 복잡성을 충분히 표현할 수 있는가
- Claude Code 자동 수정의 안정성

### 선행 연구 필요

| 항목 | 이유 | 어디서 |
|------|------|--------|
| RimWorld ThinkTree 구조 분석 | YAML 규칙 구조 설계 참고 | RimWorld modding docs |
| CEL (Common Expression Language) 평가 | 조건식 엔진 선택 | [cel.dev](https://cel.dev/), [cel-python](https://github.com/cloud-custodian/cel-python) |
| DwarfCorp GOAP+BT 하이브리드 | 규칙과 계획의 조합 방법 | [gamedeveloper.com](https://www.gamedeveloper.com/programming/how-we-developed-robust-ai-for-dwarfcorp) |
| JSON Schema 기반 YAML 검증 | 자동 수정 시 무결성 보장 | jsonschema 라이브러리 |

---

## 2. 규칙 문서 시스템 설계

### 2.1 조건 표현식 엔진 선택

| 옵션 | 장점 | 단점 | 적합도 |
|------|------|------|--------|
| **CEL** (Common Expression Language) | 안전 (비 Turing-complete), 빠름, K8s에서 검증됨 | Python 바인딩이 완전하지 않을 수 있음 | **최선** |
| **Simple Python eval()** | 구현 쉬움 | 보안 위험, 무한 루프 가능 | 부적합 |
| **Jinja2** | 템플릿 기능 풍부 | 무겁고 보안 위험 | 부적합 |
| **Custom DSL** | 완전한 통제 | 구현 비용 높음, CEL 재발명 | 차선 |
| **JSONLogic** | 간단, JSON 기반 | 표현력 제한 | 간단한 경우만 |

**결론: CEL 우선, 단순 fallback으로 Custom DSL**

CEL 사용 예시:
```yaml
conditions:
  overweight: "self.weight > self.weight_max * 0.85"
  has_ore: "inventory.count('ore') > 0"
  can_afford_pickaxe: "self.gold >= 15"
  near_forge: "nearby.has_type('forge', radius=3)"
```

> **참고**: [CEL Specification](https://cel.dev/), [Kubernetes CEL 사용 사례](https://kubernetes.io/docs/reference/using-api/cel/)

### 2.2 문서 구조 — RimWorld 패턴 적용

RimWorld 방식:
```
ThinkTree (= 행동 우선순위)
  └── ThinkNode_Priority (= 상위 카테고리)
       ├── ThinkNode_JobGiver: "Flee if danger" (= 조건 + 액션)
       └── ThinkNode_SubTree: ref to WorkGiverDefs
            ├── WorkGiver_Mining (priority=100)
            ├── WorkGiver_Crafting (priority=80)
            └── WorkGiver_Hauling (priority=50)
```

Anima 매핑:
```yaml
# rules/think_tree.yaml — 직업별 행동 트리
blacksmith:
  priority_tree:
    - name: survival
      priority: 1000
      conditions:
        - "self.hp < self.hp_max * 0.3"
      action: heal_self

    - name: overweight
      priority: 900
      conditions:
        - "self.weight > self.weight_max * 0.85"
        - "inventory.count('ore') > 0"
      action: smelt_ore
      fallback_action: move_to_location
      fallback_params: {type: forge}

    - name: has_ore
      priority: 800
      conditions:
        - "inventory.count('ore') > 0"
      action: smelt_ore
      location_required: forge

    - name: has_ingots_craft
      priority: 700
      conditions:
        - "inventory.count('ingot') >= 8"
      action: craft_blacksmith
      location_required: [forge, anvil]

    - name: has_crafted_sell
      priority: 600
      conditions:
        - "inventory.count_type('crafted_weapon') > 0"
      action: sell_to_vendor
      params:
        vendor_type: [weaponsmith, blacksmith]

    - name: gold_deposit
      priority: 500
      conditions:
        - "self.gold > 200"
      action: bank_deposit

    - name: need_tools
      priority: 400
      conditions:
        - "NOT inventory.has('pickaxe')"
        - "self.gold >= 10"
      action: buy_from_vendor
      params:
        vendor_type: [tinker, provisioner]

    - name: mine
      priority: 300
      conditions:
        - "inventory.has('pickaxe')"
        - "nearby.has_type('mineable_tile', radius=3)"
      action: mine_ore

    - name: move_to_mine
      priority: 100
      action: move_to_location
      params: {type: mine}
```

### 2.3 규칙 엔진 구현

```python
# anima/planner/rule_engine.py (설계)

class RuleEngine:
    def __init__(self, rules: list[Rule], procedures: ProcedureRegistry):
        self.rules = sorted(rules, key=lambda r: -r.priority)
        self.procedures = procedures

    async def select_procedure(self, ctx: AgentContext) -> Procedure | None:
        env = self._build_eval_env(ctx)  # CEL 평가 환경

        for rule in self.rules:
            if all(cel_eval(cond, env) for cond in rule.conditions):
                proc = self.procedures.get(rule.action)
                if proc and await proc.can_start(ctx):
                    return proc

                # fallback
                if rule.fallback_action:
                    fb = self.procedures.get(rule.fallback_action)
                    if fb and await fb.can_start(ctx):
                        return fb

                # 이 규칙에 매칭됐지만 실행 불가 → 다음 규칙으로 fall-through
                continue

        return None

    def _build_eval_env(self, ctx: AgentContext) -> dict:
        """CEL 평가를 위한 환경 변수 구축"""
        ss = ctx.perception.self_state
        return {
            "self": {
                "hp": ss.hits, "hp_max": ss.hits_max,
                "weight": ss.weight, "weight_max": ss.weight_max,
                "gold": ss.gold,
                "x": ss.x, "y": ss.y,
            },
            "inventory": InventoryProxy(ctx),  # .count(), .has() 등
            "nearby": NearbyProxy(ctx),         # .has_type(), .vendor() 등
        }
```

### 2.4 벤더 라우팅 테이블

현재 `vendor_knowledge.py`에 하드코딩. YAML로 분리:

```yaml
# rules/vendor_routing.yaml
sell_routing:
  weapons:
    graphics: [0x1441, 0x13FF, 0x13B6, 0x0F5E]  # cutlass, katana, scimitar, broadsword
    vendor_types: [weaponsmith, blacksmith]
    description: "Bladed weapons"

  armor:
    graphics: [0x13EB, 0x13F0, 0x13EE, 0x13EC]  # ringmail set
    vendor_types: [armorer, blacksmith]
    description: "Metal armor"

  ingots:
    graphics: [0x1BF2]
    vendor_types: [blacksmith, tinker]
    description: "Metal ingots"

  tools:
    graphics: [0x0E86, 0x0E85, 0x0FBB]  # pickaxe, tongs
    vendor_types: [tinker, provisioner]
    description: "Crafting tools"

buy_essentials:
  - graphic: 0x0E86  # pickaxe
    vendor_type: [tinker, provisioner]
    max_price: 25
    priority: critical

  - graphic: 0x0FBB  # tongs
    vendor_type: [tinker]
    max_price: 15
    priority: normal
```

### 2.5 위치 지식 (자동 확장)

```yaml
# knowledge/locations/minoc.yaml
city: Minoc
locations:
  - name: "East Mine Entrance"
    x: 2556
    y: 499
    type: mine
    verified: true
    last_verified: "2026-04-01"
    notes: "동쪽 광산 입구"

  - name: "Blacksmith (The Forgery)"
    x: 2471
    y: 564
    type: [forge, anvil]
    verified: true
    vendor_npcs:
      - title: "blacksmith"
        observed_positions: [[2470, 569], [2467, 569], [2470, 570]]
        wander_range: 5
        z_level: 5
    notes: "NPC가 1층(z=5)에서 돌아다님"
```

**자동 보정 메커니즘**:
벤더를 찾을 때 실측된 NPC 위치를 DB에 기록. 일정량 축적되면 YAML 위치 데이터를 업데이트:

```python
# 실측 데이터 축적
if vendor_found:
    _record_vendor_sighting(ctx, vendor.serial, vendor.name, vendor.x, vendor.y)

# 주기적으로 YAML 업데이트 (Claude Code 또는 자동)
if sighting_count > 10:
    avg_x, avg_y = compute_average_position(sightings)
    update_location_yaml("Minoc Blacksmith", avg_x, avg_y)
```

---

## 3. Claude Code 자동 규칙 수정

### 실현 가능성 평가

**긍정적**:
- YAML은 LLM이 잘 다루는 포맷 (코드보다 오류율 낮음)
- 수정 범위가 제한적 (우선순위, 임계값, 조건식)
- 검증이 쉬움 (JSON Schema + 테스트 실행)

**리스크 (5가지 실패 모드)**:

| 실패 모드 | 설명 | 완화 |
|-----------|------|------|
| **진동** | A 수정 → B 문제 발생 → B 수정 → A 복원 | 변경 로그 + 최소 1시간 간격 |
| **과적합** | 특정 좌표의 버그를 전역 규칙으로 수정 | root cause 분석 강제 + 범위 제한 |
| **스키마 위반** | 유효하지 않은 YAML 생성 | JSON Schema 검증 필수 |
| **과신** | 게임 메커니즘 오해 기반 수정 | 검증 단계 필수 (수정 → 실행 → 측정) |
| **연쇄 실패** | 한 규칙 변경이 여러 에이전트에 영향 | 에이전트별 규칙 + canary 배포 |

> **참고**: [7-Layer Constitutional AI Guardrails](https://dev.to/zer0h1ro/7-layer-constitutional-ai-guardrails-preventing-agent-mistakes-15i5)

### 안전 장치 (Guardrails)

```
문제 감지 (supervisor.py)
    ↓
분석 리포트 생성 (self_improve.py)
    ↓
Claude Code 호출 (YAML 수정 제안)
    ↓
┌── JSON Schema 검증 ──→ 실패 시 거부
├── Constitutional 규칙 체크:
│   - 생존 임계값 20% HP 이하로 내리지 않기
│   - procedure를 삭제하지 않기
│   - 한번에 3개 이상 규칙 변경하지 않기
├── git diff 생성 + 커밋
├── 단일 에이전트에 적용 (canary)
├── 30분 관찰
│   ├── 지표 개선 → 전체 적용
│   └── 지표 악화 → git revert
└── 결과 로그 기록 (improvements.jsonl)
```

### 구현 순서

1. **규칙 로더** (YAML → Python 객체)
2. **CEL 평가기** 연동
3. **규칙 엔진** (기존 planner 대체)
4. **기본 직업 3개 마이그레이션** (blacksmith, miner, woodworker)
5. **Claude Code YAML 수정 프롬프트**
6. **검증 파이프라인** (schema check → canary → rollback)

---

## 4. 다중 스킬 프로파일

### 새 직업 추가 예시: Woodworker

```yaml
# personas/woodworker.yaml
name: "Oakwind"
profession: woodworker

skills:
  primary: [lumberjacking, carpentry]
  secondary: [tinkering]
  locks:
    lumberjacking: up
    carpentry: up
    tinkering: up

stats:
  priority: STR

gameplay_loop:
  # 현재 구현된 procedure: chop_wood, make_boards, craft_carpentry 등
  - chop_wood:
      location: [forest]
      until: "weight > 80% OR no_tree_nearby"
  - make_boards:
      until: "inventory.count('log') == 0"
  - craft_carpentry:
      location: [carpentry_bench]
      until: "inventory.count('board') < 10"
  - sell_to_vendor:
      vendor_type: [carpenter, provisioner]
      until: "inventory.count_type('crafted_furniture') == 0"
```

이 YAML만 추가하면 규칙 엔진이 자동으로 동작하는 것이 Phase 2의 완료 기준.

---

## 5. 성공 지표

| 지표 | 목표 | 측정 방법 |
|------|------|-----------|
| 새 직업 추가 시 코드 변경 | 0 lines | YAML만 추가하고 테스트 |
| 자동 규칙 수정 성공률 | >50% | 수정 후 지표 개선 비율 |
| 사람 개입 빈도 | <1회/8시간 | 수동 개입 로그 |
| 연속 운영 시간 | 24시간+ | supervisor 로그 |
| 규칙 파일 수 | 5+ 직업 | YAML 파일 수 |

---

## 6. 참고 자료

- [RimWorld AI: How Pawns Think](https://github.com/roxxploxx/RimWorldModGuide/wiki/SHORTTUTORIAL:-How-Pawns-Think) — ThinkTree/WorkGiver 패턴
- [RimWorld Def Types](https://rimworldmodding.wiki.gg/wiki/Def_Types) — XML 기반 행동 정의
- [DwarfCorp AI Development](https://www.gamedeveloper.com/programming/how-we-developed-robust-ai-for-dwarfcorp) — GOAP+BT 하이브리드
- [Creating Worker NPCs Using Behavior Trees](https://blog.rubenwardy.com/2022/07/17/game-ai-for-colonists/) — 실용적 Worker AI
- [CEL (Common Expression Language)](https://cel.dev/) — 조건 표현식 엔진
- [Kubernetes CEL 사용](https://kubernetes.io/docs/reference/using-api/cel/) — CEL 실사용 사례
- [Factorio Prototype System](https://lua-api.factorio.com/latest/prototypes.html) — 데이터 기반 게임 엔티티
- [Constitutional AI Guardrails](https://dev.to/zer0h1ro/7-layer-constitutional-ai-guardrails-preventing-agent-mistakes-15i5) — AI 자동 수정 안전 장치
- [Self-Healing Agent Patterns](https://claudelab.net/en/articles/api-sdk/claude-api-self-healing-agent-production-patterns) — 감지→진단→치유→검증
