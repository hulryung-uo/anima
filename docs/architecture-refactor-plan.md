# Architecture Refactor Plan — Pub/Sub Avatar System

## 현재 문제

1. **blackboard가 글로벌 상태** — 50+ 키, 타입 불안전, 모든 모듈이 직접 접근
2. **TUI/Logger가 perception 직접 참조** — 디커플링 안 됨
3. **self-improve가 로그 파싱** — 구조화된 데이터가 아님
4. **main.py가 god function** — 300줄에 모든 초기화와 연결
5. **스킬이 BrainContext에 의존** — 테스트/재사용 어려움
6. **외부에서 상태 관찰/제어 불가** — 웹 대시보드, 외부 steering 불가

## 새 구조

### Layer 1: Avatar (핵심 엔티티)

```python
class Avatar:
    """게임 세계에 존재하는 하나의 캐릭터."""

    # 상태 (읽기 전용으로 외부 노출)
    identity: Identity       # name, serial, persona
    perception: Perception   # world, self_state, social
    inventory: Inventory     # backpack items, equipment, weight
    skills: SkillSheet       # skill values, locks, caps

    # 인프라
    connection: UoConnection  # TCP
    walker: WalkerManager     # 이동 상태 머신
    map_reader: MapReader     # 맵 데이터

    # 이벤트 버스
    bus: EventBus            # pub/sub

    # 액션 실행기
    actions: ActionExecutor  # chop, craft, walk, speak 등
```

### Layer 2: EventBus (이벤트 기반 통신)

```python
class EventBus:
    """토픽 기반 pub/sub 이벤트 버스."""

    def publish(self, topic: str, data: dict) -> None: ...
    def subscribe(self, topic: str, callback) -> Subscription: ...
    def unsubscribe(self, sub: Subscription) -> None: ...

# 토픽 예시
TOPICS = {
    "avatar.position":     {"x": int, "y": int, "z": int},
    "avatar.health":       {"hp": int, "hp_max": int},
    "avatar.stats":        {"str": int, "dex": int, "int": int, "weight": int},
    "avatar.skill_change": {"id": int, "name": str, "old": float, "new": float},
    "avatar.speech_heard": {"serial": int, "name": str, "text": str},
    "avatar.speech_sent":  {"text": str},
    "avatar.inventory":    {"items": list, "weight": int},

    "action.start":        {"action": str, "target": str},
    "action.end":          {"action": str, "result": str, "reward": float},
    "action.walk":         {"direction": int, "confirmed": bool},

    "brain.think":         {"action": str, "reason": str},
    "brain.goal_set":      {"place": str, "x": int, "y": int},
    "brain.goal_arrived":  {"place": str},

    "system.error":        {"message": str, "severity": str},
    "system.metric":       {"name": str, "value": float},
}
```

### Layer 3: Subscribers (관찰자/제어자)

```python
class Subscriber(ABC):
    """Avatar 이벤트를 받아서 처리하는 관찰자."""

    @abstractmethod
    def topics(self) -> list[str]:
        """구독할 토픽 목록."""
        ...

    @abstractmethod
    async def on_event(self, topic: str, data: dict) -> None:
        """이벤트 수신 시 호출."""
        ...

# 구현체들
class LogSubscriber(Subscriber):
    """JSON 구조화 로그 기록."""
    def topics(self): return ["*"]  # 모든 이벤트

class TUISubscriber(Subscriber):
    """터미널 대시보드 렌더링."""
    def topics(self): return ["avatar.*", "action.*", "brain.*"]

class WebSubscriber(Subscriber):
    """WebSocket으로 외부 대시보드에 전송."""
    def topics(self): return ["*"]

class MetricsSubscriber(Subscriber):
    """메트릭 수집 및 분석."""
    def topics(self): return ["action.*", "avatar.walk"]

class SteeringSubscriber(Subscriber):
    """외부에서 명령 주입 (WebSocket/REST)."""
    def topics(self): return ["system.*"]
    # 추가: 명령 수신 → bus.publish("steering.command", {...})
```

### Layer 4: Brain (의사결정 엔진)

```python
class Brain:
    """Avatar를 관찰하고 행동을 지시하는 두뇌."""

    def __init__(self, avatar: Avatar):
        self.avatar = avatar
        self.goal_manager = GoalManager()
        self.planner = LLMPlanner()        # LLM 의사결정
        self.skill_selector = SkillSelector()  # Q-learning
        self.behavior_tree = BehaviorTree()    # 루틴 행동

    async def tick(self):
        # 1. 현재 상태 평가
        state = self.avatar.snapshot()

        # 2. 목표 관리
        self.goal_manager.update(state)

        # 3. 행동 결정 (BT → Q-learning → LLM 순)
        action = self.behavior_tree.evaluate(state)
        if action is None:
            action = self.skill_selector.select(state)
        if action is None:
            action = await self.planner.decide(state)

        # 4. 실행 명령
        if action:
            await self.avatar.actions.execute(action)
```

### Layer 5: Actions (행동 실행)

```python
class ActionExecutor:
    """Brain의 명령을 받아 실제 게임 패킷으로 변환."""

    async def walk_to(self, x: int, y: int) -> bool: ...
    async def chop_tree(self, tree_x: int, tree_y: int) -> ActionResult: ...
    async def craft(self, recipe: Recipe) -> ActionResult: ...
    async def sell_to_vendor(self) -> ActionResult: ...
    async def buy_from_vendor(self, items: list) -> ActionResult: ...
    async def speak(self, text: str) -> None: ...
    async def use_item(self, serial: int) -> None: ...
```

## 마이그레이션 계획

### Phase 1: EventBus 도입 (현재 코드 위에)

기존 코드를 깨지 않으면서 EventBus를 추가합니다.

```
1. EventBus 클래스 생성
2. 기존 perception.emit() → bus.publish() 브릿지
3. 기존 ActivityFeed → bus 구독으로 전환
4. 기존 MetricsCollector → bus 구독으로 전환
5. TUI → bus 구독으로 전환
```

**파일 변경:**
- `anima/core/bus.py` (신규) — EventBus
- `anima/core/subscriber.py` (신규) — Subscriber ABC
- `anima/perception/__init__.py` — emit → bus.publish 브릿지

**예상 시간:** 3-4시간

### Phase 2: Avatar 클래스 추출

main.py의 초기화를 Avatar 클래스로 이동합니다.

```
1. Avatar 클래스 생성 (상태 + bus + connection)
2. main.py 리팩토링 → Avatar.create() + Brain.create()
3. blackboard → Avatar 속성으로 마이그레이션
4. BrainContext → Avatar 참조로 전환
```

**파일 변경:**
- `anima/core/avatar.py` (신규) — Avatar
- `anima/main.py` — 대폭 간소화
- `anima/brain/behavior_tree.py` — BrainContext → Avatar

**예상 시간:** 4-5시간

### Phase 3: Subscriber 구현

```
1. LogSubscriber — JSON 구조화 로그
2. TUISubscriber — 기존 TUI 리팩토링
3. MetricsSubscriber — 기존 metrics 리팩토링
4. SteeringSubscriber — 외부 명령 수신 (미래)
```

**예상 시간:** 3-4시간

### Phase 4: Brain 분리

```
1. Brain을 Avatar에서 완전 분리
2. GoalManager 추출
3. SkillExecutor를 ActionExecutor로 전환
4. LLMPlanner 추출
```

**예상 시간:** 4-5시간

### Phase 5: Self-Improve 연동

```
1. Self-Improver가 MetricsSubscriber 데이터를 사용
2. 구조화된 이벤트 → 로그 파싱 제거
3. 파라미터 자동 조정 → bus를 통해 Avatar에 전달
```

**예상 시간:** 2-3시간

## 총 예상 시간

- Phase 1: 3-4시간 (기존 코드 위에 추가)
- Phase 2: 4-5시간 (핵심 리팩토링)
- Phase 3: 3-4시간 (subscriber 구현)
- Phase 4: 4-5시간 (brain 분리)
- Phase 5: 2-3시간 (self-improve 연동)

**합계: 16-21시간 (2-3일)**

## 점진적 마이그레이션 원칙

1. **한 번에 하나의 모듈만 변경** — 매 단계 pytest 통과
2. **기존 인터페이스 유지** — 브릿지 패턴으로 하위 호환
3. **blackboard 키를 하나씩 제거** — 한 번에 전부 안 바꿈
4. **테스트 먼저** — 각 단계에 테스트 추가
5. **커밋 단위 작게** — 롤백 가능하도록

## Self-Improve에 최적화된 구조

새 구조가 self-improve에 좋은 이유:

1. **구조화된 이벤트** — 로그 파싱 대신 EventBus 데이터 직접 사용
2. **토픽 기반 관찰** — "action.end" 이벤트만 구독하면 스킬 성공률 계산
3. **파라미터 외부 주입** — SteeringSubscriber로 런타임 파라미터 변경
4. **A/B 테스트** — 파라미터 변경 전후 메트릭 비교 자동화
5. **모듈 교체** — Brain의 Planner만 교체해서 다른 LLM 테스트
6. **다중 Avatar** — 같은 Bus로 여러 Avatar 관찰/제어
