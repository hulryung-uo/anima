# Woodcutter Workflow

> Bjorn(나무꾼)의 전체 작업 사이클과 의사결정 흐름

## 경제 사이클

```
벌목 → 보드 변환 → 크래프팅 → 판매 → 도구 구매 → 반복
```

## 1. 벌목 (ChopWood)

### 흐름
1. 8타일 내 나무 검색 (map statics + world items)
2. depleted 나무 건너뜀 (20분 쿨다운)
3. 2타일보다 멀면 **인접 walkable 타일로 이동** (go_to)
4. 도착 못 하면 해당 나무 포기 (unreachable → depleted 등록)
5. 도끼 더블클릭 → target cursor 대기 → 나무 타겟
6. 서버 결과 메시지 대기 (journal polling)

### 결과 판정 (cliloc 메시지 기반)
| 메시지 | 의미 | 행동 |
|--------|------|------|
| "You put some logs into your backpack" | 성공 | reward +6, 같은 나무 재시도 |
| "You hack at the tree... fail to produce" | 실패 (운) | reward -0.5, 같은 나무 재시도 |
| "There's not enough wood here to harvest" | 나무 고갈 | depleted 등록, 다른 나무로 |

### 제한 조건
- 무게 제한: weight_max - 20 이상이면 벌목 안 함
- 나무 없음: 모든 근처 나무가 depleted면 can_execute=False

## 2. 보드 변환 (MakeBoards)

### 흐름
1. 배낭에 logs 있는지 확인
2. 도끼 더블클릭 → target cursor 대기
3. logs serial로 타겟 응답
4. 서버가 logs → boards 변환

### 주의사항
- 타겟 시 좌표가 아니라 **serial만** 전송 (배낭 아이템)
- 무게 제한: weight_max - 10 이상이면 안 함

## 3. 크래프팅 (CraftCarpentry)

### 의사결정 흐름
```
1. 뭘 만들지 결정 (스킬 레벨 기반)
   ├─ skill < 21: Barrel Staves (boards 5개)
   ├─ skill < 40: Barrel Lid (boards 4개)
   ├─ skill < 60: Small Crate (boards 8개)
   └─ skill >= 60: Wooden Box (boards 10개)

2. 재료 확인 (boards + logs 합계)
   ├─ 충분 → 크래프팅 진행
   └─ 부족 → LLM에 "need X more wood" 전달
              LLM이 판단: 벌목 / 이동 / 포기

3. 스킬 확인
   ├─ 충분 → 크래프팅 진행
   └─ 부족 → LLM에 "skill too low" 전달
```

### Gump 조작
```
1. saw 더블클릭 → 크래프팅 gump 열림
2. 카테고리 버튼 클릭: GetButtonID(0, group_index)
   - Other = group 0
   - Furniture = group 1
   - Containers = group 2
   - Weapons = group 3
3. 서버 응답 gump 대기 (serial 변경 감지)
4. 아이템 Create 버튼 클릭: GetButtonID(1, item_index)
5. 서버 결과 메시지 대기

GetButtonID(type, index) = 1 + type + (index * 7)
```

### 결과 판정 (cliloc 메시지 기반)
| 메시지 | 의미 | 행동 |
|--------|------|------|
| "You create the item" | 성공 | reward +5 |
| "You failed to create the item" | 실패 | reward -0.5, 재시도 |
| "You have worn out your tool" | 도구 파손 | reward -2, 도구 구매 필요 |

## 4. 판매 (SellToNpc)

### 흐름
1. 근처 NPC vendor 찾기 (notoriety=INVULNERABLE)
2. "vendor sell" speech 전송
3. sell list(0x9E) 대기
4. 전체 아이템 판매
5. gold 획득

## 5. 도구 구매 (BuyFromNpc)

### 필요 도구
| 도구 | Graphic IDs | 용도 |
|------|-------------|------|
| Hatchet | 0x0F43-0x0F4D | 벌목, 보드 변환 |
| Saw | 0x1034-0x1035 | 크래프팅 gump 열기 |

### 구매 흐름
1. vendor 더블클릭 → buy list(0x74) 대기
2. 필요 도구 graphic 매칭
3. gold 확인 후 구매

## 실패 처리

### 연속 실패 에스컬레이션
```
실패 1-4회: Q-learning이 자동 처리 (다른 스킬 시도)
실패 5회:  LLM에 상황 전달 → 전략 재수립
실패 10회: problem report 생성 (data/reports/)
```

### 이동 실패
```
deny 1-2회: denied tile 기록, A* 재계산
deny 3회:   목표 포기, wander
deny 5회:   escape_stuck (pathfind to open area)
stuck 3회:  도움 요청 speech
stuck 5회:  problem report 생성
```

### 무게 초과
- 80% 이상: LLM에 WARNING 전달
- weight_max - 20: 벌목 중단
- weight_max - 10: 크래프팅/보드변환 중단

## 스킬 상승

- UO에서 스킬은 사용 시 자동 상승
- 0x3A 패킷으로 서버가 업데이트 전송
- Journal에 "↑ Lumberjacking 50.0 → 50.1" 표시
- Activity feed에도 표시
- Lock 상태: ↑=Up(상승), ↓=Down(하락허용), •=Locked(변동없음)

## 패킷 프로토콜

| 동작 | 패킷 | 설명 |
|------|-------|------|
| 도끼 사용 | 0x06 (DoubleClick) | serial 전송 |
| 타겟 커서 | 0x6C (TargetRequest) | 서버→클라이언트 |
| 타겟 응답 | 0x6C (TargetResponse) | cursor_id 매칭 필수 |
| gump 열기 | 0xDD (CompressedGump) | 서버→클라이언트 |
| gump 응답 | 0xB1 (GumpResponse) | serial + gump_id + button_id |
| 시스템 메시지 | 0xC1 (ClilocMessage) | cliloc 번호 + args |
| 스킬 업데이트 | 0x3A (SkillUpdate) | value/base/cap/lock |
| 이동 | 0x02 (WalkRequest) | direction + seq + fastwalk |
| 이동 확인 | 0x22 (ConfirmWalk) | seq |
| 이동 거부 | 0x21 (DenyWalk) | 보정된 좌표 |
