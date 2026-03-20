# Development Plan — 2026-03-21

## 현재 상태

- **Walk 성공률**: 0% (최근 10분), 전체 980/1867 (52%)
- **Agent 위치**: (1426, 1526, z=32) — stuck
- **자기 개선 시스템**: 4개 자동 수정 커밋 (파라미터 조정)
- **핵심 문제**: 이동 자체가 안 됨 → 나머지 모든 기능이 막힘

## 자기 개선 시스템 평가

### 잘 한 것
- stuck_loop 문제 정확히 감지
- 4개 합리적인 수정:
  1. walk_sequence deny 시 리셋 ✓
  2. confirm_walk에서 predictive position update ✓
  3. escape radius 확대 ✓
  4. denied tile expiry 60초로 축소 ✓

### 못 한 것
- 맵 데이터 자체가 맞는지 검증 안 함
- Z-level 동기화 문제 감지 못함
- 파라미터만 조정, 구조적 변경 불가
- LLM think 루프가 깨진 것 감지 못함

### 결론
> 자기 개선 시스템이 고장난 엔진의 노브를 돌리고 있었음.
> 이동이 먼저 고쳐져야 파라미터 튜닝이 의미가 있음.

---

## 사람이 먼저 해결해야 할 것 (A)

### A1. 이동 디버깅 — walk deny 원인 파악 (CRITICAL)

**증상**: 서버가 모든 방향을 deny
**가능한 원인**:
1. 위치 동기화 불일치 (client vs server position)
2. walk sequence 번호 문제
3. 맵 데이터와 서버 데이터 불일치 (Z-level)
4. fastwalk key 문제

**작업**:
- [ ] 디버그 로깅 추가: 보낸 패킷 vs 서버 응답 상세 비교
- [ ] 맵 reader 없이 이동 테스트 (raw walk)
- [ ] 서버 콘솔에서 deny 이유 확인
- [ ] ClassicUO와 동일한 위치에서 동일 방향 이동 비교

**예상 시간**: 2-3시간

### A2. walk confirmation race condition 수정 (HIGH)

**증상**: `asyncio.sleep(0.5)` 후 위치 체크 — confirm이 아직 안 올 수 있음
**작업**:
- [ ] sleep 대신 event-based wait 구현
- [ ] confirm_walk에서 asyncio.Event 시그널
- [ ] step_toward에서 event.wait(timeout=1.0)

**예상 시간**: 3-4시간

### A3. static vs dynamic denied tile 구분 (HIGH)

**증상**: 60초 후 영구 장애물(벽, 나무)도 cache에서 제거됨
**작업**:
- [ ] denied tile에 reason 필드 추가 (static/dynamic/unknown)
- [ ] 같은 타일 2번 이상 deny → static으로 마킹, 만료 안 함
- [ ] dynamic (NPC가 막고 있었던 곳)만 60초 후 만료

**예상 시간**: 2-3시간

### A4. LLM think 루프 + wander 무한 루프 수정 (HIGH)

**증상**: stuck → cooldown → wander → stuck → cooldown 반복
**작업**:
- [ ] 30번 이상 위치 안 변하면 LLM 강제 rethink
- [ ] wander cooldown 사이클 탈출 로직
- [ ] "이 지역 포기, 다른 곳으로 가자" 판단

**예상 시간**: 2-3시간

### A5. 완전 stuck 시 안전 위치 복귀 (MEDIUM)

**증상**: 모든 방향 escape 실패, brute force도 실패
**작업**:
- [ ] 50번+ 위치 안 변하면 help 요청 또는 safe location으로 이동 시도
- [ ] world_knowledge에 "safe locations" 추가
- [ ] 서버에 [help stuck 같은 명령 가능하면 사용

**예상 시간**: 2-3시간

---

## 자기 개선 시스템이 처리할 수 있는 것 (B)

### B1. 파라미터 튜닝 (이동이 고쳐진 후)
- walk_delay_ms 최적화
- escape trigger threshold 조정
- denied tile expiry 최적화
- skill cooldown 미세 조정

### B2. Q-learning 보상 조정
- chop 성공/실패 보상값 최적화
- craft 보상값 최적화
- 이동 실패 페널티 조정

### B3. 위치/레시피 데이터 추가
- 새로운 나무 위치 발견
- 크래프팅 레시피 확장
- vendor 위치 추가

### B4. 메트릭 기반 이상 감지
- "정상" 기준선 설정
- 이상 발생 시 파라미터 자동 복구
- A/B 테스트 (변경 전후 비교)

---

## 잘 동작하고 있는 것 (C)

- ✅ 패킷 프로토콜 + 서버 연결
- ✅ perception 레이어 (world state, self state)
- ✅ behavior tree 프레임워크
- ✅ 스킬 시스템 인프라 (chop, craft, buy, sell)
- ✅ LLM 연동 (DeepSeek V3.1)
- ✅ 포럼 글 작성
- ✅ TUI 대시보드
- ✅ 자기 개선 루프 (분석 + 계획 + 커밋)
- ✅ 0xC1 cliloc 메시지 파싱
- ✅ 스킬 상승 감지 + 표시

---

## 우선순위 실행 계획

```
Phase 1: 이동 해결 (1-2일)
├── A1. walk deny 원인 디버깅
├── A2. race condition 수정
└── A5. stuck 복구 메커니즘

Phase 2: 이동 안정화 (1-2일)
├── A3. static/dynamic denied 구분
├── A4. think 루프 수정
└── 이동 성공률 80%+ 달성

Phase 3: 경제 사이클 검증 (1-2일)
├── 벌목 → 보드 → 크래프팅 → 판매 전체 흐름
├── 도구 구매 자동화
└── 무게 관리 + 은행

Phase 4: 자기 개선 루프 정상화 (1일)
├── B1-B4 자동화 활성화
├── 메트릭 기준선 설정
└── 파라미터 튜닝 자동화
```
