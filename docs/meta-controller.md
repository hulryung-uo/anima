# Meta-Controller 설계 (옵션 B: LLM 상위 정책 + 진화 스킬을 도구로)

> 목표: MAP-Elites 그리드가 만든 "직업별 전문가 행동들"을, **오픈월드에서
> 하루를 살아가도록 상황에 맞게 조율·전환하는 단일 상위 정책(meta-controller)**
> 아래 묶는다. 기존 자산(procedures, planner 안전장치, 그리드 elite)을 버리지
> 않고, 그 위에 "모드 조율" 층을 한 겹 얹는다.

이 문서는 제안(설계)이다. `anima/` 쪽은 구현 가능하고, fitness/평가 환경 변경은
human-owned 커널(`foundry/kernel/`)이라 **제안 형태**로만 남긴다.

---

## 1. 지금 구조에서 무엇을 재사용하는가

이미 있는 골격 (file:line):

- **`Persona.profession`** (`anima/persona.py:30`) → **`PROFESSION_LOOPS`**
  (`anima/planner/planner.py:70`): profession 문자열 → 순서 있는 procedure 이름
  튜플. 현재 planner Priority 1.5(`planner.py:761-770`)에서 **고정**으로 선택됨.
- **`Procedure` ABC** (`anima/procedures/base.py:45`): `can_start(ctx)->bool`,
  `run(ctx)->ProcedureResult`, `timeout_s`. 이름으로 `ProcedureRegistry`에 등록.
- **`StrategySelector`** (`anima/planner/strategy.py`): **이미 LLM 상위층**.
  ~5분마다 named strategy를 골라 procedure를 *배제*(`is_excluded`). planner가
  매 tick `maybe_refresh()` 호출(`planner.py:206`), `_get_proc`에서
  `is_excluded` 확인(`planner.py:515`).
- **`GoalStack`** (`anima/planner/goals.py`): Goal 스택. `forbidden_procedures`,
  `is_satisfied`, `progress`, `is_preferred`. planner가 `is_forbidden` 확인
  (`planner.py:522`).
- **그리드 elite**: 각 셀 = (직업 × 사교성) 행동. genome = `anima/`의 git 커밋.
  그리드의 개선은 seed/HEAD 메커니즘으로 base 코드(procedures 자체)에 누적됨.

**핵심 통찰:** "스킬을 도구로 노출"은 새로 만들 필요가 없다 —
`PROFESSION_LOOPS`의 각 항목이 곧 하나의 **모드(mode)**다. 메타 컨트롤러는
"어느 모드를 지금 돌릴지"를 동적으로 정하면 된다. 그리드가 키운 직업 역량은
이미 그 procedure 안에 들어 있다.

---

## 2. 설계 원칙 (안전 불변식)

1. **컨트롤러는 *생산적 모드*만 고른다. "죽지 않기"는 절대 못 덮어쓴다.**
   planner의 Priority 0/1(사망/저HP, `planner.py:533-759`)과 deadlock 복구
   사다리(`planner.py:842~`)는 **컨트롤러 위에** 그대로 둔다. 컨트롤러는
   Priority 1.5(직업 모드 선택)만 동적화한다.
2. **느린 cadence.** 컨트롤러는 매 tick이 아니라 ~분 단위로 결정한다
   (StrategySelector의 5분과 동일 철학). 빠른 planner 루프가 실행·생존·교착을
   담당하고, 컨트롤러는 "지금 무슨 일을 하며 살까"만 정한다. → 추론 비용 통제.
3. **인터페이스로 추상화 → 나중에 LLM을 학습 모델로 교체 가능(옵션 C).**
   `MetaController.decide(state) -> ModeDecision`가 계약. v1=LLM, v2=증류 모델.
   planner는 구현을 모른다.
4. **검사 가능성 유지.** 컨트롤러 결정은 typed `ModeDecision`(모드·목표·근거·
   유효시간)으로 로깅. 보상 해킹·이상 전환을 사람이 diff/감사 가능.

---

## 3. 핵심 추상화

### 3.1 Mode (모드 = 도구로 노출된 그리드 행동)

```python
# anima/planner/modes.py  (신규, 편집 가능)
@dataclass(frozen=True)
class Mode:
    name: str                       # "mining" | "smithing" | "magery" | "bard" | "combat" | "rest" | "socialize"
    loop: tuple[str, ...]           # 실행할 procedure 이름들 (PROFESSION_LOOPS 항목 재사용)
    # 컨트롤러 프롬프트/정책에 노출할 메타데이터 (그리드 elite 가설에서 파생)
    good_for: str                   # "skill+gold via ore→ingot→sell"
    needs: tuple[str, ...]          # 사전조건 키워드: ("pickaxe",) ("forge",) ...
    risk: str                       # "low" | "medium" | "high" (오픈월드 위험도)
    # 적격성: 이 모드를 *지금* 돌릴 수 있나 (싼 검사, can_start 합집합의 상위 게이트)
    def eligible(self, ctx) -> bool: ...

MODES: dict[str, Mode] = { ... }    # PROFESSION_LOOPS를 흡수·확장
```

`MODES`는 `PROFESSION_LOOPS`의 상위호환이다. 기존 5개 직업 루프 + 생활용 모드
(`rest`, `socialize`, `travel`)를 추가한다. `good_for/risk` 텍스트는 그리드
elite의 가설 문자열(예: *"speak once per practice_magery run …"*)에서 뽑아
컨트롤러에게 "각 모드가 무엇을 잘하는지" 알려주는 데 쓴다.

### 3.2 ModeDecision + MetaController 계약

```python
# anima/planner/meta_controller.py  (신규, 편집 가능 — strategy.py 구조 미러)
@dataclass(frozen=True)
class ModeDecision:
    mode: str                       # MODES 키
    goal: Goal | None               # GoalStack에 push할 목표 (선택)
    rationale: str                  # 사람이 읽는 근거 (로깅/감사)
    until_s: float                  # 이 결정의 유효시간 (이후 재결정)

class MetaController:
    """하루 단위 모드 조율. planner가 maybe_decide()를 tick마다 호출하지만
    실제 결정은 interval_s(기본 120-300s)마다, 또는 '이벤트'에 의해 일어난다."""
    def __init__(self, policy: "ModePolicy", interval_s: float = 180.0): ...
    @property
    def active_mode(self) -> str: ...
    async def maybe_decide(self, ctx) -> bool: ...        # 시간/이벤트 게이트
    # 즉시 재결정을 부르는 이벤트 (안전 위임): 위험 진입, 인벤 가득, 자원 고갈,
    # 모드가 K회 연속 실패, 세션 경과시간 단계 변화(아침/낮/밤)
    def request_redecide(self, reason: str) -> None: ...
```

### 3.3 ModePolicy (교체 가능한 정책 본체)

```python
class ModePolicy(Protocol):
    async def choose(self, state: "LivingState") -> ModeDecision: ...

class LlmModePolicy:     # v1 — StrategySelector._ask_llm 재사용/확장
    ...
class LearnedModePolicy: # v2 — 옵션 C 증류 모델 (동일 인터페이스)
    ...
```

`LivingState`는 컨트롤러가 보는 **장기 horizon 상태 요약** (한 tick 스냅샷이
아니라 누적):

```python
@dataclass
class LivingState:
    hp_frac: float; danger_nearby: bool; weight_frac: float
    gold: int; gold_rate_per_min: float          # 최근 윈도 추세
    inventory: dict[str,int]                       # ore/ingot/tools/bandages/reagents
    session_minutes: float; phase: str            # "early"|"mid"|"late" (하루 단계)
    last_modes: list[tuple[str,float]]            # 최근 모드와 지속시간 (균형 판단)
    pending_social: int                            # 응답 대기 발화 수
    skill_gains_recent: dict[str,float]            # 최근 스킬 상승 (수확 체감 감지)
    location_kind: str                             # "mine"|"town"|"wild"|"unknown"
```

---

## 4. planner 통합 (최소 침습)

`Planner.__init__`에 `self._meta = MetaController(policy)` 추가
(`self._strategy` 옆, `planner.py:137` 부근). `_run_loop`에서
`maybe_refresh` 옆에 `await self._meta.maybe_decide(ctx)` 추가
(`planner.py:206` 부근).

`select_procedure`의 Priority 1.5(`planner.py:761-770`)를 **고정 profession →
동적 active_mode**로 교체:

```python
# 기존:
profession = getattr(ctx.persona, "profession", "") or ""
for proc_name in PROFESSION_LOOPS.get(profession, ()):
    ...
# 변경:
mode = MODES.get(self._meta.active_mode)
if mode and mode.eligible(ctx):
    for proc_name in mode.loop:
        proc = _get_proc(proc_name)
        if proc and await proc.can_start(ctx):
            _intent(f"모드 {mode.name} → {proc_name}")
            return proc
# (active_mode가 'rest'/'socialize'면 해당 procedure로; 적격 모드 없으면
#  기존 mining 사다리로 자연 fallthrough → 하위호환)
```

- **컨트롤러 OFF면 기존과 동일**하게 동작해야 한다(기본 active_mode =
  persona.profession). → 점진 도입·A/B 가능.
- 컨트롤러가 고른 goal은 `self._goals.push(decision.goal)`로 기존 GoalStack에
  올린다 → `is_preferred/is_forbidden`가 그대로 모드를 강화한다.
- `request_redecide` 이벤트는 planner가 이미 계산하는 신호에 건다:
  저HP(`planner.py:739`), 과적(`planner.py:775`), deadlock 진입
  (`planner.py:1060`), 모드 procedure starvation(`_proc_breaker`).

---

## 5. "하루를 산다"를 만드는 정책 로직 (v1 LLM)

`LlmModePolicy.choose`가 `LivingState`를 받아 모드를 고른다. 프롬프트가 인코딩할
의사결정 원칙(=이전 대화에서 짚은 간극을 직접 공략):

1. **생존 우선 위임:** danger_nearby/저HP면 컨트롤러는 'travel'(안전지대로) 또는
   'rest'를 고르되, 실제 도주/힐은 planner Priority 1이 한다.
2. **균형(통합):** last_modes가 한 모드에 치우치면 다른 생산 모드로 전환 →
   "둥근 주민". 단일 셀 그라인드 방지.
3. **수확 체감:** skill_gains_recent가 평탄해지면 그 모드를 접고 다른 일.
4. **경제 지속성:** gold_rate가 음수/0이고 소모품(reagent/bandage) 고갈 추세면
   판매·보급 모드. (produce/gold 항이 0인 현재 약점 직접 보완.)
5. **하루 단계(phase):** early=스킬 그라인드, mid=경제 활동, late=사교/정리.
6. **사교는 결과로:** pending_social이 쌓이면 'socialize'. (descriptor bin
   맞추기용 가짜 발화가 아니라 *응답 대기*에 반응 — §7 평가와 연결.)

v1은 LLM prior로 위 규칙을 유연히 수행. 결정은 `ModeDecision.rationale`에
자연어 근거로 남겨 감사 가능.

---

## 6. Foundry로 메타 컨트롤러를 진화시키기

`meta_controller.py` / `modes.py` / `LlmModePolicy`의 프롬프트·임계값은 모두
`anima/`(genome body)에 있으므로 **Foundry가 변이 대상으로 삼을 수 있다.**
변이 LLM이 "phase 전환 시점", "균형 임계값", "수확 체감 판단" 등을 점 변이로
탐색 → 라이브 평가로 검증.

단, 이를 의미 있게 채점하려면 **새 fitness/descriptor가 필요하고 그건
human-owned 커널**이다(제안):

- **새 descriptor 축:** 현재 (profession × sociability) → 메타 컨트롤러용으로
  *행동 다양성*(한 평가 안에서 서로 다른 모드를 몇 개·균형 있게 돌렸나)을 한 축으로.
- **새 fitness 항(§7):** 단일 스킬레이트 대신 다목적.

---

## 7. 평가 환경 변경 제안 (커널 — 사람 승인 필요)

이전 대화의 "잘 산다" 간극을 메우려면 평가 자체가 바뀌어야 한다. 제안:

1. **긴/오픈월드 window:** 현재 600s·고정시작·위협 무력화(`gm.py`의
   `neutralized`) → 더 긴 window + 위협을 *남겨두고* 위험 하 생존을 채점.
2. **다목적 fitness** (`foundry/kernel/fitness.py`): 기존
   `skill_term+worth_term+produce_term`에 추가 —
   - `balance_term`: 여러 모드를 균형 있게 돌렸는가 (단일 그라인드 패널티 제거가
     아니라 보너스로).
   - `survival_under_threat`: 위협이 있는 채로 alive_fraction 유지.
   - `economic_sustainability`: 소모품/순자산이 윈도 끝에 음수가 아닌가.
   - `social_response_rate`: *발화 횟수*가 아니라 *응답받은* 상호작용 (이미
     `social_response_rate`가 trajectory에 있음 — 가중치를 올리고 발화-빈도
     기반 sociability와 분리).
3. **anti-gaming:** sociability를 "발화/총행동"이 아니라 "응답 유발"로 측정해
   §5.6의 가짜 발화 게이밍을 구조적으로 차단.

---

## 8. 단계별 도입 (위험 최소화)

- **P0 — 셰도우:** `MetaController`를 만들되 결정은 로깅만, planner는 기존
  고정 profession 사용. LLM이 *무엇을 골랐을지* vs 실제 진행을 비교 (무위험).
- **P1 — 활성(단일 페르소나):** active_mode를 Priority 1.5에 연결. 기본값=
  persona.profession이라 OFF 시 동작 불변. 한 페르소나로 라이브 검증.
- **P2 — 모드 확장:** `rest/socialize/travel` 모드 추가, `request_redecide`
  이벤트 연결, GoalStack 연동.
- **P3 — 진화:** 커널에 balance descriptor + 다목적 fitness 추가(사람 승인)
  후 Foundry가 컨트롤러를 변이·평가.
- **P4 — 증류(옵션 C):** 축적된 trajectory로 `LearnedModePolicy` 학습 →
  동일 인터페이스로 `LlmModePolicy` 교체. 연합(병렬 노드)이 데이터 공급.

---

## 9. 신규/수정 파일 요약

| 파일 | 종류 | 내용 |
|---|---|---|
| `anima/planner/modes.py` | 신규(편집가능) | `Mode`, `MODES` (PROFESSION_LOOPS 흡수+확장) |
| `anima/planner/meta_controller.py` | 신규(편집가능) | `MetaController`, `ModeDecision`, `ModePolicy`, `LlmModePolicy`, `LivingState` |
| `anima/planner/planner.py` | 수정 | `__init__`에 `_meta`, `_run_loop`에 `maybe_decide`, Priority 1.5 동적화, redecide 이벤트 후크 |
| `anima/planner/strategy.py` | 재사용 | `_ask_llm` 로직을 `LlmModePolicy`가 차용 |
| `anima/planner/goals.py` | 재사용 | 컨트롤러가 고른 goal을 push |
| `foundry/kernel/fitness.py` · `descriptor.py` · `gm.py` | **제안(사람)** | 다목적 fitness, balance descriptor, 오픈월드 평가 |

---

## 10. 한 줄 요약

메타 컨트롤러는 **새 두뇌를 만드는 게 아니라, 이미 있는 `StrategySelector`/
`GoalStack` 골격을 "procedure 배제"에서 "모드 조율"로 승격**시키는 일이다.
그리드 elite는 `MODES`로 노출되는 *도구*가 되고, 단일 LLM 정책이 오픈월드에서
하루를 균형 있게 조율한다. 안전(생존/교착)은 planner 하부에 그대로 두어 검사
가능성과 불변식을 유지한다. 인터페이스(`ModePolicy`)를 계약으로 두었기에, 충분한
데이터가 모이면 LLM 정책을 증류된 단일 모델(옵션 C)로 무중단 교체할 수 있다.
