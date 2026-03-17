# Skill System & Hierarchical RL Design

## Overview

Anima의 행동 시스템은 4단계 계층으로 구성된다.

```
Level 3: Strategy    LLM이 장기 목표 설정       "대장장이가 되고 싶다"
Level 2: Activity    RL이 스킬 선택 최적화       mine → smelt → craft → sell
Level 1: Skill       코드로 구현된 행동 단위      mine_ore(), smelt(), craft()
Level 0: Packet      서버 프로토콜               double_click, target, walk
```

- **Level 0–1**: 코드로 구현 (패킷 프로토콜은 학습 대상이 아님)
- **Level 2**: RL로 학습 (어떤 스킬을 언제 어디서 실행할지)
- **Level 3**: LLM + 메모리 (장기 전략, 상황 판단)

---

## Skill Interface

모든 스킬은 동일한 인터페이스를 가진다.

```python
@dataclass
class SkillResult:
    success: bool
    reward: float
    message: str              # 결과 설명 ("Mined 3 iron ore")
    items_gained: list[int]   # 획득한 아이템 serial 목록
    items_consumed: list[int] # 소모된 아이템 serial 목록
    skill_gains: list[tuple[int, float]]  # (skill_id, gain_amount)
    duration_ms: float        # 실행 소요 시간

class Skill(ABC):
    name: str                 # "mine_ore"
    category: str             # "gathering", "crafting", "combat", ...
    description: str          # LLM/RL이 참조할 설명

    # 실행 전 자동 체크 조건
    required_items: list[int]          # 배낭에 있어야 할 아이템 graphic
    required_nearby: list[int]         # 근처에 있어야 할 오브젝트 graphic
    required_skill: tuple[int, float]  # (skill_id, min_value)
    required_stats: dict[str, int]     # {"str": 30} 등

    async def can_execute(ctx: BrainContext) -> bool
    async def execute(ctx: BrainContext) -> SkillResult
```

---

## Skill Catalog

### Category: Gathering (자원 수집)

#### `mine_ore` — 채광
- **설명**: 곡괭이로 바위를 캐서 광석을 획득한다.
- **필요 아이템**: Pickaxe (0x0E86) 또는 Shovel (0x0F39)
- **필요 근처**: Cave/Rock tile 또는 mountain terrain
- **UO Skill**: Mining (#45)
- **패킷 시퀀스**:
  1. 배낭에서 곡괭이 찾기
  2. `double_click(pickaxe_serial)` — 곡괭이 사용
  3. `target(rock_tile)` — 바위 타겟팅 (패킷 0x6C)
  4. 시스템 메시지 대기: "You dig some ore" / "There is no metal here"
  5. 결과 파싱
- **보상**: 광석 획득 (+5), 스킬 상승 (+3), 실패 (-1)
- **소요 시간**: ~2-3초

#### `chop_wood` — 벌목
- **설명**: 도끼로 나무를 베어 통나무를 획득한다.
- **필요 아이템**: Hatchet (0x0F43) 또는 Axe류
- **필요 근처**: Tree tile
- **UO Skill**: Lumberjacking (#44)
- **패킷 시퀀스**:
  1. 배낭에서 도끼 찾기
  2. `double_click(axe_serial)`
  3. `target(tree_tile)` — 나무 타겟팅
  4. 시스템 메시지 대기: "You chop some logs"
  5. 결과 파싱
- **보상**: 통나무 획득 (+5), 스킬 상승 (+3), 실패 (-1)

#### `fish` — 낚시
- **설명**: 낚싯대로 물에서 물고기를 잡는다.
- **필요 아이템**: Fishing Pole (0x0DBF)
- **필요 근처**: Water tile
- **UO Skill**: Fishing (#18)
- **패킷 시퀀스**:
  1. 배낭에서 낚싯대 찾기
  2. `double_click(pole_serial)`
  3. `target(water_tile)`
  4. 시스템 메시지 대기
- **보상**: 물고기 획득 (+3), 보물지도/메시지병 (+10), 실패 (-1)

#### `harvest_reagent` — 시약 채집
- **설명**: 필드에서 자라는 시약 재료를 수확한다. (Nightshade, Ginseng 등)
- **필요 근처**: Reagent plant graphic
- **UO Skill**: 없음 (누구나 가능)
- **패킷 시퀀스**:
  1. 근처 시약 식물 찾기
  2. `double_click(plant_serial)`
  3. 결과 확인

---

### Category: Crafting (제작)

#### `smelt_ore` — 제련
- **설명**: 광석을 용광로에서 녹여 주괴로 만든다.
- **필요 아이템**: Ore (0x19B7~0x19BA)
- **필요 근처**: Forge (0x0FB1) 또는 fire pit
- **UO Skill**: Mining (#45)
- **패킷 시퀀스**:
  1. 배낭에서 광석 찾기
  2. `double_click(ore_serial)`
  3. 용광로 자동 감지 또는 `target(forge)`
  4. 시스템 메시지 대기: "You smelt the ore"
- **보상**: 주괴 획득 (+5), 실패 (-2)

#### `craft_blacksmith` — 대장간 제작
- **설명**: 주괴와 대장간 도구로 무기/방어구를 제작한다.
- **필요 아이템**: Smith Hammer (0x13E3) + Ingots (0x1BF2)
- **필요 근처**: Anvil (0x0FAF)
- **UO Skill**: Blacksmith (#8)
- **패킷 시퀀스**:
  1. `double_click(hammer_serial)` — 제작 메뉴(Gump) 열기
  2. Gump 응답: 카테고리 선택 → 아이템 선택
  3. 제작 결과 대기
- **보상**: 아이템 제작 성공 (+10), 예외적 품질 (+20), 실패 (-3)
- **참고**: Gump 패킷 (0xB1) 응답 구현 필요

#### `craft_tailor` — 재봉
- **설명**: 천/가죽으로 옷/방어구를 제작한다.
- **필요 아이템**: Sewing Kit (0x0F9D) + Cloth/Leather
- **UO Skill**: Tailoring (#57)
- **패킷 시퀀스**: craft_blacksmith과 유사 (Gump 기반)

#### `craft_alchemy` — 연금술
- **설명**: 시약과 빈 병으로 포션을 제작한다.
- **필요 아이템**: Mortar & Pestle (0x0E9B) + Reagents + Empty Bottles
- **UO Skill**: Alchemy (#0)
- **패킷 시퀀스**: Gump 기반 제작

#### `craft_tinker` — 팅커링
- **설명**: 주괴로 도구를 만든다 (곡괭이, 톱 등).
- **필요 아이템**: Tinker Tools (0x1EB8) + Ingots
- **UO Skill**: Tinkering (#58)
- **패킷 시퀀스**: Gump 기반 제작
- **참고**: 다른 제작 스킬의 도구를 만들 수 있어서 기반 스킬

#### `craft_carpentry` — 목공
- **설명**: 통나무로 가구/무기를 만든다.
- **필요 아이템**: Saw (0x1034) + Logs/Boards
- **UO Skill**: Carpentry (#11)

#### `craft_cooking` — 요리
- **설명**: 식재료를 조리한다.
- **필요 아이템**: 식재료 + Heat Source (화덕, 오븐)
- **UO Skill**: Cooking (#13)

#### `craft_inscription` — 주문서 작성
- **설명**: 마법 스크롤을 제작한다.
- **필요 아이템**: Blank Scroll + Reagents
- **UO Skill**: Inscription (#23)

---

### Category: Combat (전투)

#### `melee_attack` — 근접 공격
- **설명**: 무기를 들고 대상을 공격한다.
- **필요 아이템**: 무기 장비 (Layer.ONE_HANDED 또는 TWO_HANDED)
- **필요 근처**: 공격 가능한 대상 (Notoriety: ATTACKABLE, CRIMINAL, ENEMY, MURDERER)
- **UO Skill**: Swordsmanship (#40), Mace Fighting (#41), Fencing (#42), Archery (#31) 중 해당
- **패킷 시퀀스**:
  1. `war_mode(True)` — 전투 모드 전환
  2. `attack(target_serial)` — 공격 대상 지정
  3. 전투 중: HP 모니터링, 이동으로 위치 조정
  4. 대상 사망 또는 도주 시 종료
  5. `war_mode(False)` — 평화 모드 복귀
- **보상**: 대상 처치 (+15), 전리품 획득 (+5), 피해 받음 (-3/hit)

#### `cast_spell` — 주문 시전
- **설명**: 마법 주문을 시전한다.
- **필요**: 마나 + 시약 + Magery 스킬
- **UO Skill**: Magery (#25)
- **패킷 시퀀스**:
  1. `cast_spell(spell_id)` — 주문 시전 (0xBF sub=0x1C 또는 매크로)
  2. `target(target_serial)` — 대상 타겟팅 (공격 주문의 경우)
  3. 결과 대기
- **참고**: UseSkill 패킷 (0x12) 필요

#### `heal_self` — 자가 치유
- **설명**: 붕대나 치유 주문으로 HP를 회복한다.
- **필요 아이템**: Bandage (0x0E21) 또는 마나 + 시약
- **UO Skill**: Healing (#17) 또는 Magery
- **패킷 시퀀스**:
  1. `double_click(bandage_serial)`
  2. `target(self_serial)`
  3. 치유 진행 대기 (~5-10초)
- **보상**: HP 회복량 비례 (+1 per 10 HP healed)

#### `heal_other` — 타인 치유
- **패킷 시퀀스**: heal_self와 동일하나 target이 타인
- **보상**: HP 회복 (+3) + 관계 향상 (+disposition)

#### `loot_corpse` — 시체 루팅
- **설명**: 전투 후 시체에서 전리품을 수거한다.
- **필요 근처**: Corpse item
- **패킷 시퀀스**:
  1. `double_click(corpse_serial)` — 시체 열기
  2. 내용물 확인
  3. `pick_up(item_serial)` + `drop_to_backpack(item_serial)` — 아이템 수거
- **참고**: Pick up (0x07) + Drop (0x08) 패킷 필요

---

### Category: Taming (테이밍)

#### `tame_animal` — 동물 길들이기
- **설명**: 야생 동물을 길들여 펫으로 만든다.
- **필요 근처**: Tameable animal
- **UO Skill**: Animal Taming (#14)
- **패킷 시퀀스**:
  1. `use_skill(14)` — Animal Taming 스킬 사용
  2. `target(animal_serial)` — 대상 지정
  3. 길들이기 진행 (~10-30초, 여러 번 시도)
  4. 시스템 메시지: "You have tamed the creature" / "You fail to tame"
- **보상**: 테이밍 성공 (+20), 실패 (-2), 공격당함 (-5)
- **참고**: followers/followers_max 체크 필요

#### `command_pet` — 펫 명령
- **설명**: 길들인 펫에게 명령을 내린다 (follow, attack, stay, come).
- **패킷 시퀀스**: 말하기 기반 ("all follow me", "all kill")
- **참고**: 펫 serial 추적 필요

---

### Category: Trade (거래)

#### `buy_from_npc` — NPC 구매
- **설명**: NPC 상인에게서 아이템을 구매한다.
- **필요**: 골드 + NPC 상인 근처
- **패킷 시퀀스**:
  1. `double_click(npc_serial)` — 상점 열기
  2. Buy Gump 표시 (0x24) → 아이템 목록 파싱
  3. Buy 패킷 (0x3B) — 구매할 아이템과 수량 전송
  4. 결과 확인
- **보상**: 필요한 아이템 구매 (+5), 불필요한 구매 (-3)

#### `sell_to_npc` — NPC 판매
- **설명**: NPC 상인에게 아이템을 판매한다.
- **패킷 시퀀스**:
  1. 상점 열기
  2. Sell Gump → 판매 가능 아이템 목록
  3. Sell 패킷 (0x9F) — 판매할 아이템 전송
- **보상**: 골드 획득량 비례

#### `trade_player` — 플레이어 거래
- **설명**: 다른 플레이어와 아이템/골드를 교환한다.
- **패킷 시퀀스**:
  1. 아이템을 플레이어에게 드래그 → Secure Trade 창 열림
  2. 아이템 배치
  3. 체크박스 확인 → 거래 완료
- **참고**: Secure Trade 패킷 (0x6F) 구현 필요

---

### Category: Social (사회)

#### `speak` — 말하기 (구현 완료)
- 이미 `anima/action/speech.py`에 구현됨

#### `join_guild` — 길드 가입
- 길드 Gump 응답으로 처리

#### `party_invite` — 파티 초대/수락
- **패킷**: Party system (0xBF sub=0x06)

#### `emote` — 감정 표현
- **패킷**: Unicode speech with MessageType.EMOTE

---

### Category: Utility (유틸리티)

#### `recall` — 리콜 (텔레포트)
- **설명**: 룬을 사용해서 기록된 장소로 순간이동한다.
- **필요 아이템**: Recall Rune (marked) + Reagents (또는 Recall Scroll)
- **UO Skill**: Magery (#25, 최소 35.0)
- **패킷 시퀀스**:
  1. `cast_spell(RECALL)` 또는 `double_click(recall_scroll)`
  2. `target(rune_serial)`

#### `mark_rune` — 룬 마킹
- **설명**: 현재 위치를 룬에 기록한다.
- **필요 아이템**: Blank Rune + Reagents
- **UO Skill**: Magery (#25, 최소 45.0)

#### `open_bank` — 은행 열기
- **설명**: 은행원에게 "bank"라고 말하면 은행 상자가 열린다.
- **필요 근처**: Banker NPC
- **패킷 시퀀스**: `unicode_speech("bank")` (NPC 근처에서)

#### `use_gate` — 게이트/포탈 사용
- **패킷**: `double_click(gate_serial)`

---

## 구현 우선순위

스킬을 한꺼번에 다 만들 수 없으므로 단계적으로 구현한다.

### Phase 1: 기반 인프라 + 기본 스킬 (3–4개)
1. Skill 인터페이스 (`anima/skills/base.py`)
2. SkillRegistry — 사용 가능한 스킬 목록 관리
3. SkillSelector — Q-table 기반 스킬 선택
4. 기본 스킬 구현:
   - `mine_ore` — 자원 수집의 대표
   - `heal_self` — 생존의 기본
   - `melee_attack` — 전투의 기본
   - `buy_from_npc` / `sell_to_npc` — 경제 활동의 기본

### Phase 2: 제작 체인
5. `smelt_ore` — 채광 → 제련 연결
6. `craft_blacksmith` — 제련 → 제작 연결
7. `craft_tinker` — 도구 제작 (자립 기반)
8. Gump 패킷 핸들러 구현

### Phase 3: 확장
9. `chop_wood` + `craft_carpentry`
10. `fish`
11. `tame_animal` + `command_pet`
12. `cast_spell` — 마법 시스템
13. 추가 전투 스킬

### Phase 4: 고급
14. `trade_player` — 플레이어 간 거래
15. `recall` / `mark_rune` — 이동 효율화
16. 파티/길드 시스템
17. 집짓기 (Advanced housing)

---

## 필요한 새 패킷

현재 구현되지 않은 패킷 중 스킬 시스템에 필요한 것:

| 패킷 | ID | 용도 | 우선순위 |
|---|---|---|---|
| **Target Response** | 0x6C | 타겟 커서 응답 (스킬/아이템 사용 후 대상 지정) | **필수** |
| **Use Skill** | 0x12 | 스킬 직접 사용 (Taming, Hiding 등) | **필수** |
| **Pick Up Item** | 0x07 | 아이템 집기 | **필수** |
| **Drop Item** | 0x08 | 아이템 놓기 (바닥/컨테이너) | **필수** |
| **Equip Item** | 0x13 | 아이템 장착 | 높음 |
| **Buy Items** | 0x3B | NPC 구매 | 높음 |
| **Sell Items** | 0x9F | NPC 판매 | 높음 |
| **Gump Response** | 0xB1 | Gump(UI) 버튼 클릭 응답 | 높음 (제작 필수) |
| **Cast Spell** | 0xBF(1C) | 주문 시전 | 중간 |
| **Drag Item** | 0x07+0x08 | 아이템 이동 (인벤토리 정리, 거래) | 중간 |
| **Secure Trade** | 0x6F | 플레이어 간 거래 | 낮음 |

---

## Hierarchical RL Design

### State Space (Level 2)

스킬 선택을 위한 상태는 다음 요소들의 조합:

```
state = {
    "location_type": "mine" | "town" | "forest" | "dungeon" | "water" | "field",
    "has_players": bool,
    "has_enemies": bool,
    "hp_level": "full" | "healthy" | "wounded" | "critical",
    "inventory": "empty" | "has_ore" | "has_ingots" | "has_tools" | "full",
    "top_skill": "mining" | "blacksmith" | "combat" | ...,
    "time_context": "active" | "idle",
}
```

상태 키는 문자열로 직렬화: `"mine|no_players|healthy|has_ore|mining|active"`

### Action Space (Level 2)

등록된 스킬 중 `can_execute() == True`인 것들이 현재 가능한 액션.

### Q-Table

```sql
CREATE TABLE q_values (
    agent_name TEXT,
    state_key TEXT,
    action TEXT,        -- skill name
    q_value REAL DEFAULT 0.0,
    visit_count INTEGER DEFAULT 0,
    last_updated REAL,
    PRIMARY KEY (agent_name, state_key, action)
);
```

### 업데이트 규칙

```python
# 스킬 실행 후
result = await skill.execute(ctx)
reward = result.reward

# 다음 상태
next_state = encode_state(ctx)
max_next_q = max(q_table[next_state].values(), default=0)

# Q-learning update
alpha = 0.1   # 학습률
gamma = 0.9   # 할인율
q_table[state][skill.name] += alpha * (reward + gamma * max_next_q - q_table[state][skill.name])
```

### 탐험 전략: UCB1

```python
def select_skill(state, available_skills):
    total_visits = sum(q_table[state][s].visit_count for s in available_skills)

    best_score = -inf
    for skill in available_skills:
        q = q_table[state][skill.name].q_value
        n = q_table[state][skill.name].visit_count

        # UCB1: exploitation + exploration
        if n == 0:
            score = float('inf')  # 한 번도 안 해본 스킬 우선
        else:
            exploration_bonus = C * sqrt(ln(total_visits) / n)
            score = q + exploration_bonus

        if score > best_score:
            best_score = score
            best_skill = skill

    return best_skill
```

### Location-Activity Value Map

```sql
CREATE TABLE location_values (
    agent_name TEXT,
    region_x INTEGER,     -- x // 32
    region_y INTEGER,     -- y // 32
    activity TEXT,        -- skill category or name
    total_reward REAL DEFAULT 0.0,
    visit_count INTEGER DEFAULT 0,
    last_visited REAL,
    PRIMARY KEY (agent_name, region_x, region_y, activity)
);
```

"이 위치에서 이 활동의 평균 보상" 추적.
LLM 프롬프트에 주입: "Mining near Minoc: avg reward +6.2 (15 visits)"

### Goal Sequence

```sql
CREATE TABLE goal_transitions (
    agent_name TEXT,
    from_activity TEXT,
    to_activity TEXT,
    avg_reward REAL DEFAULT 0.0,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (agent_name, from_activity, to_activity)
);
```

"이 활동 다음에 저 활동을 하면 보상이 좋았다" 추적.
LLM 프롬프트에 주입: "After mining, smelting works well (avg +7.2)"

---

## Brain 통합

### 현재 Brain Tick

```
poll_events → Selector(Survival → Social → Forum → Think)
```

### 스킬 시스템 추가 후

```
poll_events → Selector(
    Survival      -- HP < 30% → heal_self or flee
    Social        -- 대화 요청 → respond
    Forum         -- 포럼 읽기/쓰기
    SkillExec     -- 실행 중인 스킬 계속 진행 (RUNNING 상태)
    SkillSelect   -- RL이 다음 스킬 선택 → 실행
    Think         -- 할 일 없으면 LLM이 전략 결정
)
```

`SkillSelect` 노드가 핵심:
1. 현재 state 인코딩
2. `can_execute()` 필터링
3. Q-table + UCB1로 스킬 선택
4. `skill.execute()` 호출
5. 결과로 Q-table 업데이트 + 에피소드 기록

### LLM과의 협업

LLM은 RL의 **상위 레벨 가이드** 역할:
- "오늘은 Mining에 집중하자" → SkillSelector에 bias 주입
- "이 지역은 위험하다" → 전투/도주 스킬 가중치 증가
- "돈이 필요하다" → 판매 관련 스킬 가중치 증가

프롬프트에 RL 통계 주입:
```
Your skill stats:
- Mining: 45.2 (last gained at Minoc mine, avg reward +6.2)
- Blacksmith: 12.0 (rarely practiced, avg reward +2.1)
- Swordsmanship: 30.5 (used near cemetery, avg reward +4.0)

Recent activity rewards:
- mine_ore at mine: 8/10 success, avg +5.5
- smelt_ore at town: 6/8 success, avg +4.2
- sell_to_npc at market: 5/5 success, avg +3.0

What should we focus on next?
```

---

## File Structure

```
anima/skills/
├── __init__.py
├── base.py              # Skill ABC, SkillResult, SkillRegistry
├── selector.py          # Q-table SkillSelector, UCB1
├── state.py             # State encoder (ctx → state_key)
├── gathering/
│   ├── __init__.py
│   ├── mine.py          # mine_ore
│   ├── lumber.py        # chop_wood
│   └── fish.py          # fish
├── crafting/
│   ├── __init__.py
│   ├── smelt.py         # smelt_ore
│   ├── blacksmith.py    # craft_blacksmith
│   └── tinker.py        # craft_tinker
├── combat/
│   ├── __init__.py
│   ├── melee.py         # melee_attack
│   ├── magic.py         # cast_spell
│   └── healing.py       # heal_self, heal_other
├── trade/
│   ├── __init__.py
│   ├── buy.py           # buy_from_npc
│   └── sell.py          # sell_to_npc
└── taming/
    ├── __init__.py
    └── tame.py          # tame_animal, command_pet
```
