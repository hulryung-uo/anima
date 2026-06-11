# Handoff: action-capabilities 개선 머지 (2026-06-11)

`improve/action-capabilities` 브랜치(10 커밋)가 main에 머지되었습니다.
직전 10-cycle run은 자연 종료되었고 (grid 4/21, best 8.27, GATHERING/NONE
밖 진입 실패), 이 개선은 정확히 그 병목을 풀기 위한 것입니다.

## 무엇이 바뀌었나

**Planner 견고화 (anti-freeze)** — g_00008(0.39)류 붕괴 방지:
- procedure 실행 타임아웃 (기본 300s, `timeout_s` 클래스 속성으로 override)
- liveness watchdog: 90초 무진행 시 stalled procedure 취소 + 60초간
  fallback 활동 강제 (직업 작업 → 알려진 장소 이동 → 랜덤 걷기)
- starvation breaker: 3연속 실패 procedure를 120초 demote

**달리기**: `go_to`가 8타일 이상 + 스태미나 30% 이상이면 0x80 run 비트
사용 (이동 2배속). `movement.run_enabled`로 끌 수 있음.

**직업 프리미티브** — 빈 셀 진입로 (전부 라이브 검증 완료, window 300s):

| persona/fixed-start | fitness | cell | 비고 |
|---|---|---|---|
| thief | 15.85 (gate 1.0) | THIEF-STEALTH | Hiding 그라인드, 아이템 불필요 |
| bard | 56.74 (gate 1.0) | BARD-SOCIAL | 악기 연주 + 주기 발화 |
| mage | 208.68 (gate 1.0) | MAGIC | Greater Heal 셀프캐스트 + 명상 |
| warrior(adventurer) | 28.17 (gate 0.58) | COMBAT | HeadlessOne 아레나 + 붕대 |

새 procedure: practice_hiding / practice_music / practice_magery /
bandage_self / hunt_nearby. 새 액션: anima/actions/{skills,spells,equip}.py.
planner는 `persona.profession`(yaml 필드)으로 직업 루프를 우선 선택.

**Kernel 변경 (human-owned, 의도적):**
- `uoconst.ACTION_GROUP`에 0x12(UseSkill/CastSpell) = "skill" 추가 —
  스킬 전용 직업이 frozen으로 오심되던 ruler 버그. 기존 채굴 genome
  점수는 불변(0x12를 안 보냄); g_00008(Spirit Speak 변이)류는 재평가 시
  점수가 오를 수 있음.
- `gm.py`: fixed-start 프로필 4종(mage/warrior/bard/thief), 아레나 몹
  스폰(command_at, GM 타일에 스택 — 산비탈 오프셋은 절벽 z로 감), eval
  전 `[WipeNPCs` ±12타일 (이전 eval 잔존 몹 제거), 크로스 프로세스 GM
  flock (`/tmp/foundry-gm.flock`).
- `eval.py`: GM 단계가 flock으로 보호됨 — 수동 프로브와 orchestrator
  동시 실행 안전.

**변이 에이전트 연동**: `docs/actions.md` (전체 액션/procedure 카탈로그,
변이 LLM 대상), `anima.actions` façade (단일 import 지점), mutate.py
프롬프트에 문서 포인터("if present" 가드 — 구 계보 안전).

**버그 수정 (main에도 있던 것):**
- `test_deny_walk` — walker가 `asyncio.get_event_loop().time()`을 루프
  밖에서 호출 (Python 3.14에서 RuntimeError) → `time.monotonic()`.
- PERSONA_SKILLS 스킬 ID 다수 오류 (bard가 Meditation+RemoveTrap을
  받고 있었음). PERSONA_STATS 합계 80 → ServUO가 전원 10/10/10으로
  리셋하고 있었음 (합계 90 필요).

## 재시작 방법

```bash
cd ~/dev/uo/anima

# 1) 기존 GATHERING 계보 계속 (개선된 base 자동 상속: HEAD 기준 시드)
uv run python -m foundry.orchestrator --cycles 10 --parallel 2 --window 600 --seeds 3 --backend claude --model sonnet

# 2) 새 직업 셀 시딩 (직업당 짧은 run — eval persona가 셀을 결정)
uv run python -m foundry.orchestrator --cycles 3 --parallel 1 --window 600 --seeds 3 --backend claude --model sonnet --persona mage  --fixed-start mage
#   ... thief / bard / adventurer(+--fixed-start warrior)도 동일 패턴
```

주의:
- orchestrator는 시작 시점 HEAD에 kernel을 pin하므로 재시작만으로 모든
  개선이 반영됩니다. 기존 genome의 code_ref 계보는 그대로 재현 가능.
- 수동 프로브를 orchestrator와 동시에 돌릴 때: 새 코드끼리는 flock으로
  안전하지만, 프로브는 가급적 순차 실행 권장.
- run 활성화 후 첫 eval에서 `steps_denied` 비율을 한 번 확인 (loop
  penalty 영향). 악화 시 config `movement.run_delay_ms`를 220-250으로.

## 다음 단계 제안
- orchestrator에 target-cell 기반 persona 자동 선택 (현재는 run당 고정).
- warrior gate 0.58 개선: 전투-붕대 사이클 사이 유휴 구간 추적.
- 경제 루프 (produce/gold 항이 여전히 0) — 계획 Phase 4.
