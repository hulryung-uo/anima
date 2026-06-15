# Apprentice Track — 한 직업을 장기 실행하며 GM 튜터링으로 "살아가기"를 배우기

> 목표: "10분 그라인더를 진화"시키는 현재 루프 옆에, **한 직업을 장기 연속으로
> 살게 하고, 실패하면 GM이 스캐폴딩(부활 검프 등)을 제공하되, 그 도움을 *측정하고
> 점점 줄이도록* 압박해 결국 무지원 자율 생존으로 가는** 별도 학습 트랙을 둔다.

이 문서는 제안(설계)이다. `anima/`·`foundry/orchestrator.py`·`foundry/select.py`는
구현 가능하고, fitness/descriptor/GM 개입 등 `foundry/kernel/`은 HUMAN-OWNED라
**제안 형태**로 남긴다. 연관: `docs/meta-controller.md`, `docs/FOUNDRY.md`.

---

## 0. 왜 이 트랙인가 (근거)

현재 평가는 600s·고정시작·위협 무력화 아레나에서 단일 직업 루프를 채점한다.
이건 "각 일을 잘하는 검증된 기술"을 만드는 데는 정확하지만, "UO에서 산다"
(장기 horizon·오픈월드·실패 복구·통합)는 직접 겨냥하지 않는다(`docs/meta-controller.md` §0).

핵심 통찰: **실패(특히 죽음)를 episode 종료가 아니라 *학습 기회*로 바꾸면**, 장기
실행이 "무엇을 구현해야 자율적으로 살 수 있는가"를 가르쳐 준다. 교사(GM)가 실패
지점에서 시연(힐러로 데려가 부활 검프)을 제공하고, **그 개입의 빈도 자체를 줄이는
것을 성공의 척도로 삼는다.**

---

## 1. 무엇이 이미 있는가 (재사용)

- **GM 명령 어휘** (`foundry/kernel/gm.py`): `go_to`([Go), `command_on`
  ([Set Skills / [AddToPack / [Set X Y Z 텔레포트), `command_at`([Add 스폰),
  `command_area`([WipeNPCs), `say`. → 텔레포트·아이템지급·스폰은 *이미 됨*;
  부활 보조는 같은 `say("[…")` 패턴의 확장(신규, 커널).
- **자가구조 사다리** (`anima/planner/planner.py`): `seek_resurrection`
  (`_SeekResurrection` :3859), `_YellForHelp`(:2786), `_RelocateToCity`(:2927),
  `_DeathEscalate`, 포럼 도움요청, deadlock Lv0–7. → GM 튜터는 이것을 대체하지
  않는다. **이것들이 *실패했을 때*의 교사 백스톱**이고, 백스톱 1회 = "이 자가구조
  경로가 부족하다"는 신호.
- **fitness 게이트** (`foundry/kernel/fitness.py`): `gate = alive_fraction ×
  liveness × (1−loop_penalty)`, `fitness = gate × (skill+worth+produce+behavior)`.
  → 죽음이 fitness를 직접 무너뜨림. **이래서 채점 eval 중 GM 구조는 치명적 오염**
  (§3 가드레일 1).
- **focus 가능 인프라**: orchestrator `--persona/--fixed-start`, target-cell
  선택(`foundry/select.py`).

---

## 2. 세 가지 구성요소

### 2.1 Focus 모드 — 한 직업에 세션 집중 (editable)
- MAP-Elites는 21셀에 compute를 얇게 편다. 한 직업을 *제대로* 끌어올리거나 장기·
  GM튜터 실험의 통제 변수를 잡으려면 그 직업 row에 집중하는 게 빠르다.
- **구현:** `foundry/select.py`에 focus 바이어스 — `suggest_target_cell`이 지정
  직업(예: COMBAT) row만 고르도록. orchestrator에 `--focus-profession COMBAT`
  플래그. 그리드는 유지(다양성 손실은 캠페인 동안만).
- 위험 낮음, 비용 거의 없음. **즉시 가능.**

### 2.2 Soak 트랙 — 장기 연속 실행 (editable, 별도 트랙)
- 10분이 숨기는 실패 모드(소모품 고갈, 죽음/부활, 자원에서 멀어져 갇힘, 스킬 정체,
  누적 과적)는 장기 실행에서만 드러난다.
- **핵심 원칙 — 짧은 window를 *대체하지 않는다*:**
  - 짧은 window = **fitness 신호**(싸고 비교 가능, 다중 시드/confirm 유지).
  - 장기 연속 실행 = **"문제 발견/soak 트랙"** — 채점이 아니라 *실패 모드 수집*.
    여기서 나온 실패가 새 테스트 시나리오·fitness 항·GM 트리거가 된다.
- **구현:** orchestrator에 `--soak` 모드 — 단일(또는 소수) 세션을 긴 시간(예
  수 시간) 돌리고, fitness 채점 대신 **이벤트 로그**(사망/교착/개입/스킬 변화)를
  수집. §4의 GM 튜터가 이 트랙에서만 활성.

### 2.3 GM 튜터 — 실패 시 스캐폴딩 (트리거/로깅 editable, 실제 개입은 커널 제안)
실패 지점에서 교사가 시연을 제공해 episode를 잇고 능력 공백을 라벨링한다. §4 상세.

---

## 3. 두 가지 가드레일 (이게 핵심 — 안 지키면 독)

### 가드레일 1 — 채점 eval과 분리하라 (anti-gaming)
커널이 잠긴 이유가 "구조받아 fitness를 부풀리지 못하게"다. 채점 eval 중 GM이
부활시키면 `alive_fraction`이 더는 genome의 실제 생존력을 측정하지 못한다 — 계속
죽지만 구조받는 genome이 생존자처럼 점수받아 **선택압이 붕괴**한다.

**규칙:**
- GM 튜터는 **Soak/apprentice 트랙에서만** 활성. **채점 진화 eval(`foundry.kernel.eval`)
  에선 절대 비활성** — 죽음은 거기서 여전히 비용.
- 두 트랙은 서로 다른 entrypoint/플래그로 구분하고, eval은 GM 개입 코드 경로를
  아예 타지 않도록 한다(커널 불변식).

### 가드레일 2 — 도움은 점점 줄어야 한다 (학습된 무기력 방지)
GM이 늘 구조하면 에이전트는 자가구조를 *배울* 압력이 없어져 "죽고→구조받기"를
학습한다(고전적 스캐폴딩 함정).

**규칙: GM 개입을 *목표가 아니라 측정 대상*으로 두고, 그 빈도를 줄이는 것을 보상.**
- apprentice 트랙의 성공 척도(§5) = **개입/시간 우하향 + 무지원 최장 생존시간 증가**.
- 진화가 이 트랙의 산출(자가구조 procedure 개선)을 채점할 때, **개입 횟수에
  페널티**를 준다(제안: 새 fitness 항 또는 descriptor 축, §6).
- **fading 스케줄:** 같은 실패 유형에 대한 개입을 점차 지연/축소(즉시→N초 대기→
  부분 도움→무개입). 에이전트의 자가구조가 먼저 성공하면 개입 안 함.

---

## 4. GM 튜터 설계

### 4.1 트리거 (editable — soak 트랙 감시자)
soak 트랙 감시자가 trajectory/상태를 보고 "자력 복구 불가" 상태를 감지:
- **사망 미복구:** HP=0가 N초 지속 + `seek_resurrection`이 K회 실패(planner의
  `_repeat_counter["seek_resurrection"]` 재사용).
- **정지/교착:** 위치 변화 0 + 행동 0이 N초(liveness watchdog 신호 재사용).
- **자원 데드엔드:** 도구·소모품 0 + deadlock 사다리가 최고 레벨에서 M회 공회전.
- 트리거는 **보수적으로** — 자가구조 사다리가 먼저 시도하고 *실패한 뒤*에만 발동
  (가드레일 2). 너무 적극적이면 다 가려버린다.

### 4.2 개입 (PROPOSE — 커널 gm.py 확장)
GM이 실제로 가서 도움. 기존 명령 패턴 재사용:
- **부활:** 가장 가까운 힐러 좌표로 `command_on("[Set X Y Z", eval_serial)`
  텔레포트 → 부활 검프 유발(`[Resurrect` 또는 힐러 상호작용). 에이전트가 검프
  수락 인터랙션을 *경험*하도록.
- **최소 키트 복원:** `[AddToPack`로 붕대/무기 등 직업 최소 장비.
- **안전지대 이동:** 위험 지속 시 작업장/마을로 텔레포트.
- 각 개입은 **원자적·로깅됨**(§4.3). 개입 자체는 fitness를 주지 않는다.

### 4.3 개입 로그 = 능력 공백 백로그 (editable)
개입 1건마다 구조화 기록(`data/apprentice/interventions.jsonl`):
```json
{"ts":…, "cause":"death_unrecovered", "profession":"warrior",
 "self_rescue_tried":["seek_resurrection×5","yell_for_help×2"],
 "gm_action":"teleport_to_healer+resurrect_gump",
 "location":[x,y], "session_minutes":42.3}
```
→ 이 로그가 **다음에 구현/개선할 procedure의 정확한 to-do 리스트**가 된다(예:
"warrior가 부활 후 무기 재장착을 못 함" → equip procedure 보강). 진화 변이 LLM의
입력으로도 쓸 수 있다.

---

## 5. 성공의 척도 (게이밍에 강한 목표)

apprentice 트랙은 fitness가 아니라 **자율성 지표**로 평가:
- **interventions_per_hour** — 우하향이 진보. (1순위 지표)
- **longest_unassisted_survival_s** — 우상향이 진보.
- **self_rescue_success_rate** — 자가구조 사다리가 GM 없이 복구한 비율 ↑.
- 직업 능력(스킬 상승률 등)은 부차 — 무지원으로 유지될 때만 인정.

성공 = "소드 전사가 죽어도 *스스로* 힐러를 찾아 부활하고 키트를 갖춰 복귀하기까지의
GM 개입이 0으로 수렴". 개입에 페널티가 걸려 있어 "구조 의존" 게이밍이 구조적으로
억제된다.

---

## 6. 진화 연동 (PROPOSE — 커널)

apprentice 트랙이 만든 자가구조 procedure 개선을 진화가 채점하려면:
- **새 descriptor 축(제안):** 기존 (직업 × 사교성)에 *자율성 bin*(개입률 구간)을
  추가하거나, apprentice 전용 평가 모드.
- **새 fitness 항(제안):** `autonomy_term` = f(개입률↓, 무지원 생존↑). 단 이건
  **채점 eval(GM 비활성)에서 측정** — soak 트랙의 개입 로그는 *목표 설정*에 쓰고,
  채점은 GM 없는 환경에서 자력 생존을 본다(가드레일 1 유지).

---

## 7. 전체 그림 (다른 설계와의 결합)

```
focus 모드 ─┐
            ├─► soak 장기 연속 실행 ─► 실패 ─► [자가구조 사다리 시도]
장기 실행 ──┘                                     │ 실패 시
                                          GM 튜터 스캐폴딩 + 개입 로깅
                                                   │
                                          능력 공백 백로그 ─► planner 자가구조 보강
                                                   │
              채점 eval(GM 없음) ◄── autonomy_term: 개입률↓ 보상 ──┘
                                                   │
                            메타 컨트롤러(docs/meta-controller.md)가
                            완성된 능력들을 "모드"로 조율 → 오픈월드 생활
```

"10분 그라인더 진화" → **"실패를 통해 튜터링받다가 무지원으로 사는 견습생"**.
성공의 척도가 *GM 개입률 하락*이라 게이밍에 강하다.

---

## 8. 단계별 도입

- **A0 — Focus(editable, 즉시):** `--focus-profession`로 한 직업 row 집중. 무위험.
- **A1 — Soak 트랙(editable):** `--soak` 장기 단일 세션 + 이벤트 로깅(채점 없음).
  GM 개입은 *아직 로깅만*(셰도우): "여기서 GM이 개입했을 것" 기록, 실제 개입 X.
- **A2 — GM 튜터 활성(PROPOSE 커널):** gm.py에 부활/키트복원/안전이동 개입 추가,
  보수적 트리거 + fading. 개입 로그 → 능력 공백 백로그.
- **A3 — 자가구조 보강(editable):** 백로그를 보고 planner 자가구조 procedure를
  채움(또는 진화가 변이로 탐색).
- **A4 — autonomy 채점(PROPOSE 커널):** GM 없는 채점 eval에 `autonomy_term`/
  descriptor 추가 → 진화가 자율성을 직접 보상.

A1은 메타 컨트롤러 P0(셰도우)와 같은 철학 — **먼저 관찰·로깅, 행동 변경은 검증 후.**

---

## 9. 파일별 (편집 가능 vs 사람 승인)

| 파일 | 종류 | 내용 |
|---|---|---|
| `foundry/select.py` | 수정(편집가능) | focus 바이어스: 한 직업 row만 target |
| `foundry/orchestrator.py` | 수정(편집가능) | `--focus-profession`, `--soak` 모드, soak 감시자 |
| `foundry/apprentice.py` | 신규(편집가능) | soak 루프, 트리거 감시, 개입 로깅, 자율성 지표 집계 |
| `anima/planner/planner.py` | 수정(편집가능) | 자가구조 사다리 보강(백로그 기반) |
| `data/apprentice/interventions.jsonl` | 산출물 | 개입 로그 = 능력 공백 백로그 |
| `foundry/kernel/gm.py` | **제안(사람)** | 부활/키트복원/안전이동 개입 + fading |
| `foundry/kernel/eval.py` | **제안(사람)** | apprentice 평가 모드; 채점 eval은 GM 비활성 유지 |
| `foundry/kernel/fitness.py`·`descriptor.py` | **제안(사람)** | `autonomy_term`, 자율성 descriptor |

---

## 10. 위험 / 열린 질문

- **트리거 보정:** 너무 적극→다 가림(가드2 위반), 너무 보수→무용. soak 셰도우
  (A1)에서 "언제 개입했을지" 분포를 먼저 보고 튜닝.
- **비용:** 장기 튜터 실행은 비쌈. 가치는 raw 학습량이 아니라 *백로그(무엇을 구현
  할지)*에 있음 — 소수 세션이면 충분.
- **채점 분리의 엄격성:** GM 개입 코드가 채점 eval 경로에 *절대* 안 닿도록 커널
  레벨에서 보장(별도 entrypoint/플래그). 이 불변식이 깨지면 전체 선택압이 오염.
- **fading 설계:** 개입 지연/축소 스케줄을 어떻게 둘지(시간 기반 vs 성공 기반)는
  A2에서 실험 필요.

---

## 11. 한 줄 요약

한 직업을 장기 연속으로 살게 하고, 자가구조가 실패하면 GM이 시연(부활 검프 등)을
제공하되 — **(1) 채점 eval과 분리해 fitness 오염을 막고, (2) 개입을 *목표가 아니라
측정 대상*으로 두어 그 빈도 하락을 보상** — 이 두 규칙 하에서, 실패가 "무엇을 구현해야
자율적으로 사는가"를 가르치는 커리큘럼이 된다. 성공의 척도는 *GM 개입률이 0으로
수렴하는 것*이고, 그 결과 자가구조 능력이 채워지면 메타 컨트롤러가 그것들을 모드로
조율해 오픈월드 생활로 이어진다.
