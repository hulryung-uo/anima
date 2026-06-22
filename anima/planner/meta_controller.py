"""Meta-controller — long-horizon mode arbitration (option B).

The meta-controller decides *which mode* (mining, smithing, magery, bard,
combat, rest, socialize, travel) the avatar should be in, given a long-horizon
view of its state rather than the current tick. It sits ABOVE the planner's
fast priority ladder; survival and deadlock recovery stay below it and can
never be overridden. See `docs/meta-controller.md`.

Stage P0 (this file, SHADOW): the controller runs and *logs* what it would
pick, but does NOT change procedure selection — the planner keeps using the
persona's fixed profession. This is zero-risk: behaviour is identical with the
controller on or off. P0 produces `data/meta_shadow.jsonl` so we can compare
the controller's choices against what the planner actually ran before letting
it drive (P1).

Design mirrors `StrategySelector`: `maybe_decide()` is called every planner
tick, gates on an interval, and runs the LLM call on a background single-flight
task so a slow model never stalls the planner. Degrades to a no-op when no LLM
is configured (e.g. during deterministic Foundry evals).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

from anima.planner.modes import MODES, default_mode_for_profession

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

_SHADOW_LOG = Path(__file__).parent.parent.parent / "data" / "meta_shadow.jsonl"


@dataclass
class LivingState:
    """Long-horizon state summary the controller reasons over (not a single
    tick snapshot — gold_rate, session_minutes and last_modes accumulate)."""

    hp_frac: float
    weight_frac: float
    gold: int
    gold_rate_per_min: float
    nearby_mobiles: int
    danger_nearby: bool
    inventory_text: str
    session_minutes: float
    phase: str                       # early | mid | late
    last_modes: list[str] = field(default_factory=list)
    actual_mode: str = "mining"      # what the planner is currently running
    location: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class ModeDecision:
    mode: str
    rationale: str
    goal: str | None = None          # P2 promotes this to a goals.Goal
    chosen_at: float = field(default_factory=time.time)


class ModePolicy(Protocol):
    """The replaceable decision body. v1 = LLM; v2 = distilled model (option
    C). The planner/controller never depend on the implementation."""

    async def choose(self, state: LivingState) -> ModeDecision: ...


def _phase_for(session_minutes: float) -> str:
    if session_minutes < 20:
        return "early"
    if session_minutes < 50:
        return "mid"
    return "late"


def build_living_state(
    ctx: "AgentContext",
    *,
    gold_rate_per_min: float,
    session_minutes: float,
    last_modes: list[str],
) -> LivingState:
    """Cheap, best-effort state summary from perception + persona.

    Only reads state already maintained by perception; never sends packets.
    Fields that aren't cheaply available in P0 are approximated and marked
    in docs/meta-controller.md §3.3 for tightening in later stages.
    """
    ss = ctx.perception.self_state
    # Before the first 0x11/0x16 status packet arrives ``hits_max`` is 0 (and
    # ``hits`` is 0 too). That is "HP unknown", NOT "HP zero" — the rest of the
    # codebase encodes this convention explicitly (SelfState.hp_percent returns
    # a 100.0 placeholder; combat_loop/planner gate every HP test behind
    # ``hits_max > 0``; combat_loop._target_key falls back to hp_frac=1.0). The
    # controller must follow the SAME convention or a freshly-spawned, perfectly
    # healthy avatar reads as hp_frac=0/1=0.0 and the heuristic policy fires a
    # spurious survival-defer (``hp_frac < 0.5``) on its very first decision.
    hp_known = ss.hits_max > 0
    hp_max = ss.hits_max or 1
    weight_max = ss.weight_max or 1

    # Inventory: compact top-N from backpack (mirror StrategySelector).
    backpack = ss.equipment.get(0x15) if hasattr(ss.equipment, "get") else None
    inv: list[str] = []
    if backpack is not None:
        for it in list(ctx.perception.world.items.values()):
            if getattr(it, "container", 0) == backpack:
                name = getattr(it, "name", None) or f"0x{getattr(it, 'graphic', 0):04X}"
                inv.append(f"{getattr(it, 'amount', 1)}x {name}")
            if len(inv) >= 10:
                break
    inventory_text = ", ".join(inv) or "(empty)"

    # Nearby HOSTILE mobiles as a coarse danger proxy (approximate — P0).
    # Friend/foe matters: a wounded avatar standing next to the co-located
    # survival-arena Healer (or any town NPC / vendor / other player / its own
    # pet) must NOT read as "in danger" — counting those non-foes would steer
    # the controller to `rest` mode away from a fight it has already won, the
    # exact friend/foe asymmetry the planner's flee gate fixed in
    # ``_is_live_hostile`` (corpses, bare-gray humans, invulnerable/yellow-bar
    # Healers). Reuse that same predicate so the danger proxy and the flee gate
    # count one population. Lazy import: planner imports THIS module at load, so
    # a module-level import would be circular — but planner is fully imported by
    # the time ``build_living_state`` runs at tick time.
    try:
        from anima.planner.planner import _attackable_set, _is_live_hostile

        attackable = _attackable_set()
        nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=8)
        nearby_mobiles = sum(
            1 for m in nearby if _is_live_hostile(m, ss, attackable)
        )
    except Exception:
        nearby_mobiles = 0
    # Unknown HP (no status packet yet) is treated as full/safe, so an unknown
    # reading never counts as "wounded" toward the danger proxy.
    danger_nearby = nearby_mobiles > 0 and hp_known and (ss.hits < hp_max * 0.6)

    profession = getattr(ctx.persona, "profession", "") if ctx.persona else ""
    return LivingState(
        hp_frac=(ss.hits / hp_max) if hp_known else 1.0,
        weight_frac=ss.weight / weight_max,
        gold=ss.gold,
        gold_rate_per_min=gold_rate_per_min,
        nearby_mobiles=nearby_mobiles,
        danger_nearby=danger_nearby,
        inventory_text=inventory_text,
        session_minutes=session_minutes,
        phase=_phase_for(session_minutes),
        last_modes=last_modes[-6:],
        actual_mode=default_mode_for_profession(profession),
        location=(ss.x, ss.y),
    )


class HeuristicModePolicy:
    """Deterministic, LLM-free policy — encodes the docs §5 "living" principles
    as pure priority logic over existing LivingState fields.

    This is the fallback used during deterministic Foundry evals (no LLM). It
    exists so the P0 shadow stage actually logs decisions in exactly the runs
    that produce the offline trajectory dataset — otherwise the controller
    would be inert and write zero rows. No time, no randomness: every branch is
    a pure function of `state`, so replays are reproducible.

    goal is always None — P0 does not consume goals.
    """

    # Fixed rotation of low-risk *productive* modes for the balance branch.
    # Ordering is stable so rotation is deterministic.
    _LOW_RISK_PRODUCTIVE: tuple[str, ...] = ("mining", "smithing", "magery", "bard")
    # Modes whose whole point is converting effort into gold. A flat/negative
    # gold rate in one of these means the loop has stopped paying off (mined-out
    # vein, no buyer, full pack) — the economic-sustainability lever (docs §5
    # principle 4) says relocate/offload rather than keep grinding it. Pure
    # skill-grinds (magery/bard/thief) are excluded: a flat gold rate there is
    # expected and is NOT a signal to abandon the stint.
    _GOLD_EARNING: frozenset[str] = frozenset({"mining", "smithing", "combat"})
    # How many identical recent stints trigger a balance rotation.
    _BALANCE_STINTS = 3
    # Mid-phase "economy" day-phase routine (docs/meta-controller.md §5
    # principle 5: early=grind, mid=economy, late=socialize). A heavy-but-not-
    # overweight pack in the mid phase means it is time to convert the haul to
    # gold (travel→sell/offload) rather than keep grinding. Above the
    # comfortable-grind band (0.5, pinned by tests) and below the hard
    # overweight cutoff (0.9, branch b). Skill-grinders reach economy ONLY here.
    # The late phase ("socialize/tidy up") shares this offload gate: tidying up
    # a haul before socializing is exactly the late-day routine, and without it
    # a late-phase avatar carrying a near-full pack drifts straight to socialize
    # and never converts the haul (branch b only catches >= 0.9 overweight).
    _ECONOMY_WEIGHT = 0.75
    _ECONOMY_PHASES: frozenset[str] = frozenset({"mid", "late"})

    async def choose(self, state: LivingState) -> ModeDecision:  # type: ignore[override]
        actual = state.actual_mode if state.actual_mode in MODES else "mining"

        # (a) survival-defer: low HP or danger → rest. Actual fleeing/healing is
        # still handled by the planner's Priority 0/1 below the controller; the
        # controller only steers the long-horizon mode, never the tick action.
        if state.danger_nearby or state.hp_frac < 0.5:
            return ModeDecision(
                mode="rest",
                rationale=f"survival-defer: hp={state.hp_frac:.0%} danger={state.danger_nearby}",
                goal=None,
            )

        # (b) overweight: offload before resuming productive work.
        if state.weight_frac >= 0.9:
            return ModeDecision(
                mode="travel",
                rationale=f"overweight: weight={state.weight_frac:.0%} → offload",
                goal=None,
            )

        # (c) economic sustainability: a gold-earning mode whose rate has gone
        # flat/negative past the early phase (enough samples to trust it) and
        # that is carrying a non-trivial pack is a dead loop — relocate/offload
        # via travel instead of grinding ground that no longer pays. Gated on
        # weight so we don't send an empty-handed avatar on a pointless trip.
        if (
            actual in self._GOLD_EARNING
            and state.phase != "early"
            and state.gold_rate_per_min <= 0.0
            and state.weight_frac >= 0.25
        ):
            return ModeDecision(
                mode="travel",
                rationale=(
                    f"economy: {actual} gold-rate {state.gold_rate_per_min:+.1f}/min "
                    f"flat at weight={state.weight_frac:.0%} → relocate/offload"
                ),
                goal=None,
            )

        # (d) balance: if the last N stints were all the current mode, rotate to
        # a different low-risk productive mode (deterministic next-in-list).
        recent = state.last_modes[-self._BALANCE_STINTS :]
        if (
            len(recent) >= self._BALANCE_STINTS
            and all(m == actual for m in recent)
            and actual in MODES
        ):
            rotated = self._next_low_risk(actual)
            if rotated != actual:
                return ModeDecision(
                    mode=rotated,
                    rationale=f"balance: rotated off {actual} after {self._BALANCE_STINTS} stints",
                    goal=None,
                )

        # (e) day-phase default (docs §5 principle 5): early=skill grind,
        # mid=economy, late=socialize/tidy. The economy routine was previously
        # unimplemented (mid mapped to `actual`, identical to early), so a
        # heavy-laden avatar — especially a pure skill-grinder, which branch (c)
        # deliberately excludes — never got steered to convert its haul. Both
        # the mid (economy) and late (tidy-up) phases run this offload: a
        # late-phase avatar with a near-full pack must convert/offload the haul
        # before drifting to socialize, otherwise it socializes overweight and
        # the haul never becomes gold (branch b only catches >= 0.9 overweight).
        if (
            state.phase in self._ECONOMY_PHASES
            and state.weight_frac >= self._ECONOMY_WEIGHT
        ):
            return ModeDecision(
                mode="travel",
                rationale=(
                    f"day-phase: {state.phase} economy → offload "
                    f"(weight={state.weight_frac:.0%})"
                ),
                goal=None,
            )
        phase_mode = {"early": actual, "mid": actual, "late": "socialize"}.get(
            state.phase, actual
        )
        if phase_mode not in MODES:
            phase_mode = actual
        return ModeDecision(
            mode=phase_mode,
            rationale=f"day-phase: {state.phase} → {phase_mode}",
            goal=None,
        )

    def _next_low_risk(self, actual: str) -> str:
        """Deterministically pick the next low-risk productive mode after
        `actual` in the fixed list, skipping `actual` itself."""
        order = self._LOW_RISK_PRODUCTIVE
        if actual in order:
            i = order.index(actual)
            return order[(i + 1) % len(order)]
        # actual isn't in the rotation list → return its first distinct entry.
        for m in order:
            if m != actual:
                return m
        return actual


class LlmModePolicy:
    """v1 policy — asks the LLM which mode to be in, given the living state.

    Encodes the "living" decision principles from docs/meta-controller.md §5
    in the prompt: survival-defer, balance, diminishing returns, economic
    sustainability, day-phase, social-by-response.
    """

    async def choose(self, state: LivingState) -> ModeDecision:  # type: ignore[override]
        # Bound: the policy is handed the llm via choose_with_llm; the bare
        # protocol method is unused (kept for type clarity).
        raise NotImplementedError

    async def choose_with_llm(self, state: LivingState, llm) -> ModeDecision:
        mode_lines = "\n".join(
            f"- {m.name}: {m.good_for} (risk: {m.risk})" for m in MODES.values()
        )
        recent = " → ".join(state.last_modes) or "(none yet)"
        prompt = f"""You direct an Ultima Online avatar that should LIVE in the world over a long session — not grind one task. Pick the best MODE for roughly the NEXT FEW MINUTES.

Living state:
- HP: {state.hp_frac:.0%}   Weight: {state.weight_frac:.0%}   Gold: {state.gold} (rate {state.gold_rate_per_min:+.1f}/min)
- Nearby mobiles: {state.nearby_mobiles}{' (DANGER)' if state.danger_nearby else ''}
- Inventory: {state.inventory_text}
- Session: {state.session_minutes:.0f} min ({state.phase})   Recent modes: {recent}

Available modes:
{mode_lines}

Principles:
1. If in danger or low HP, prefer rest/travel (actual fleeing/healing is handled below you).
2. Balance — don't stay in one mode forever; a rounded resident varies work.
3. If a mode's returns have flattened, switch.
4. Keep the economy sustainable (sell/restock when gold rate is flat and consumables run low).
5. Day phase: early=skill grind, mid=economy, late=socialize/tidy up.
6. Socialize when there is presence to respond to — value is in responses, not chatter.

Respond in EXACTLY this format:
MODE: <one of the mode names above>
GOAL: <short goal for this stint, or "none">
REASONING: <one short sentence>"""

        response = await llm.chat([{"role": "user", "content": prompt}])
        text = (getattr(response, "text", "") or "").strip()
        mode = ""
        goal: str | None = None
        reasoning = "LLM parse failed"
        for line in text.splitlines():
            line = line.strip()
            up = line.upper()
            if up.startswith("MODE:"):
                mode = line.split(":", 1)[1].strip().lower()
            elif up.startswith("GOAL:"):
                g = line.split(":", 1)[1].strip()
                goal = None if g.lower() in {"none", ""} else g
            elif up.startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()
        if mode not in MODES:
            # keep the avatar's current behaviour; record the miss
            mode = state.actual_mode
            reasoning = f"unparseable/unknown mode → keep {mode}; raw={text[:60]!r}"
        return ModeDecision(mode=mode, rationale=reasoning, goal=goal)


class MetaController:
    """Holds the current (shadow) mode decision and refreshes it periodically.

    P0: `active_mode` is computed but NOT consumed by the planner. Decisions
    are logged to structlog and appended to data/meta_shadow.jsonl alongside
    the mode the planner actually ran, so P0 can be evaluated offline.
    """

    MIN_INTERVAL_S = 60.0
    # Decisions accrue one mode string per interval for the entire (open-ended)
    # lifetime of the agent process. Only the last few are ever consumed
    # (``build_living_state`` slices ``last_modes[-6:]``), so an unbounded list
    # is a pure soak leak — keep a bounded ring with comfortable headroom.
    _MODE_HISTORY_CAP = 32

    def __init__(
        self,
        policy: LlmModePolicy | None = None,
        interval_s: float = 180.0,
        shadow: bool = True,
    ) -> None:
        self.policy = policy or LlmModePolicy()
        self.interval_s = max(interval_s, self.MIN_INTERVAL_S)
        self.shadow = shadow
        self._decision: ModeDecision | None = None
        # Monotonic timestamp of the last decision spawn. ``None`` means "never
        # decided yet" so the first tick is always eligible. The cadence gate
        # MUST run on a MONOTONIC clock, not wall time (see ``should_decide``):
        # this mirrors the identical fix already shipped for
        # ``StrategySelector`` (commit c1bef3d / strategy.py §should_refresh),
        # whose docstring explicitly names THIS controller as still carrying
        # the wall-clock pattern. ``_started_at`` and ``_gold_rate_per_min``
        # are already monotonic; the decision cadence was the one straggler.
        self._last_decide: float | None = None
        self._decide_task: asyncio.Task | None = None
        self._started_at: float = time.monotonic()
        self._mode_history: deque[str] = deque(maxlen=self._MODE_HISTORY_CAP)
        # gold-rate tracking
        # Samples track NET WORTH (backpack gold + last-known bank balance),
        # not backpack gold, so a bank deposit is a wash not a phantom loss.
        self._gold_samples: list[tuple[float, int]] = []  # (monotonic, gold)
        # Last bank balance we ever observed *fresh*. Carried forward when the
        # blackboard ``bank_balance`` cache ages out so net worth never
        # phantom-collapses by the banked amount just because the agent hasn't
        # re-polled the bank inside the freshness window (see ``_net_worth``).
        self._last_known_bank: int = 0

    @property
    def active_mode(self) -> str | None:
        """The controller's current choice. In P0 this is advisory only."""
        return self._decision.mode if self._decision else None

    def should_decide(self, now: float | None = None) -> bool:
        # Cadence gate runs on a MONOTONIC clock, not wall time. The interval
        # is a "seconds since the last decision" throttle, and ``time.time()``
        # is not monotonic: an NTP correction or a VM resume can step it
        # BACKWARD by minutes, making ``now - last`` go negative and the gate
        # stay closed for the whole backward step — silently freezing the P0
        # shadow-decision log (and, once P1 promotes the controller to drive
        # selection, freezing mode arbitration itself). ``time.monotonic()`` is
        # immune to clock steps. Mirrors ``StrategySelector.should_refresh``.
        if self._last_decide is None:
            return True  # never decided → always eligible on the first tick
        now = now if now is not None else time.monotonic()
        return now - self._last_decide >= self.interval_s

    async def maybe_decide(self, ctx: "AgentContext") -> bool:
        """Spawn a background mode-decision task if eligible. Returns True if
        spawned. Never raises; never blocks the planner tick."""
        if not self.should_decide():
            return False
        if self._decide_task is not None and not self._decide_task.done():
            return False
        self._last_decide = time.monotonic()
        self._decide_task = asyncio.create_task(self._do_decide(ctx))
        return True

    async def close(self) -> None:
        """Cancel and reap any in-flight background decision task.

        ``maybe_decide`` spawns ``_do_decide`` with a bare
        ``asyncio.create_task``, so the task is NOT a child of the planner's
        ``TaskGroup`` and is therefore not torn down when the session ends
        (disconnect, eval-window close, reconnect). With an LLM configured,
        ``_do_decide`` may be suspended for the whole LLM timeout inside
        ``policy.choose_with_llm``; left orphaned it keeps the ``ctx``/``llm``
        references alive, may emit a stray LLM call or shadow-log write after
        the world has been torn down, and trips asyncio's "Task was destroyed
        but it is pending" warning. The planner calls this from its ``run``
        ``finally`` so shutdown is deterministic. Idempotent and never raises.
        """
        task = self._decide_task
        self._decide_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("meta_close_task_error", error=str(e))

    def _net_worth(self, ctx: "AgentContext") -> int:
        """Backpack gold + fresh banked gold.

        The gold-earning loop ends in a bank deposit, which zeroes
        ``ss.gold``. Measuring the rate on backpack gold alone therefore
        reports a large *negative* rate immediately after a *successful*
        economic cycle, which falsely trips the "economy: gold-rate flat →
        relocate/offload" branch (HeuristicModePolicy, branch c). Counting
        banked gold keeps a deposit a wash. The bank balance comes from the
        ``bank_balance`` cache that ``check_bank_balance`` writes
        (``{"amount", "ts"}``); we honour the same 10-minute freshness
        window the buy/restock path uses (buy_from_vendor._BANK_CACHE_TTL).

        When the cache ages out we DON'T drop the banked amount to zero —
        that would phantom-collapse net worth by thousands of gold the moment
        the agent simply stops re-polling the bank, and the rate window
        (``_gold_rate_per_min``) would then read a huge *negative* slope
        between an in-window sample that still saw the fresh balance and the
        post-staleness sample that did not, spuriously firing the very
        relocate/offload branch this whole accounting exists to suppress.
        Instead we carry forward the last balance we ever saw fresh, so a
        stale cache is a hold (net worth unchanged), a real deposit/withdraw
        updates it, and only a controller that has NEVER seen a fresh balance
        falls back to backpack-only.
        """
        ss = ctx.perception.self_state
        bal_cache = ctx.blackboard.get("bank_balance") or {}
        amount = bal_cache.get("amount")
        ts = bal_cache.get("ts", 0)
        if amount is not None and time.time() - ts <= 600:
            # Fresh reading — trust it and remember it for the stale path.
            bank = int(amount)
            self._last_known_bank = bank
        else:
            # Stale/absent cache: hold the last fresh balance rather than
            # collapsing it to zero (0 only before any fresh reading exists).
            bank = self._last_known_bank
        return ss.gold + int(bank)

    def _gold_rate_per_min(self, net_worth: int) -> float:
        now = time.monotonic()
        self._gold_samples.append((now, net_worth))
        # keep a ~10-minute window
        self._gold_samples = [(t, g) for (t, g) in self._gold_samples if now - t <= 600]
        if len(self._gold_samples) < 2:
            return 0.0
        t0, g0 = self._gold_samples[0]
        dt_min = (now - t0) / 60.0
        return (net_worth - g0) / dt_min if dt_min > 0 else 0.0

    async def _do_decide(self, ctx: "AgentContext") -> None:
        # The living state (gold-rate / session / history) is LLM-independent,
        # so build it first. With no LLM (deterministic Foundry evals) we fall
        # back to the pure HeuristicModePolicy so the shadow log is still
        # written — that path is precisely the one that produces the offline
        # trajectory dataset, so an inert controller would defeat P0's purpose.
        llm = getattr(ctx, "llm", None)
        policy_kind = "llm" if llm is not None else "heuristic"
        try:
            ss = ctx.perception.self_state
            state = build_living_state(
                ctx,
                gold_rate_per_min=self._gold_rate_per_min(self._net_worth(ctx)),
                session_minutes=(time.monotonic() - self._started_at) / 60.0,
                last_modes=list(self._mode_history),
            )
            if llm is None:
                decision = await HeuristicModePolicy().choose(state)
            else:
                decision = await self.policy.choose_with_llm(state, llm)
        except Exception as e:  # never let the controller break the planner
            logger.warning("meta_decide_failed", error=str(e))
            return

        prev = self._decision.mode if self._decision else None
        self._decision = decision
        self._mode_history.append(decision.mode)
        agree = decision.mode == state.actual_mode

        logger.info(
            "meta_shadow_decision",
            would_pick=decision.mode,
            actual_mode=state.actual_mode,
            agree=agree,
            phase=state.phase,
            gold_rate=round(state.gold_rate_per_min, 1),
            goal=decision.goal,
            rationale=decision.rationale[:100],
            policy=policy_kind,
            shadow=self.shadow,
        )
        self._append_shadow_log(state, decision, prev, agree, policy_kind)

    def _append_shadow_log(
        self,
        state: LivingState,
        decision: ModeDecision,
        prev: str | None,
        agree: bool,
        policy_kind: str = "llm",
    ) -> None:
        """Best-effort JSONL artifact for offline P0 evaluation. Never raises.

        `policy` distinguishes heuristic (LLM-less eval) rows from llm rows so
        offline analysis can separate the two populations.
        """
        try:
            _SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.time(),
                "policy": policy_kind,
                "would_pick": decision.mode,
                "actual_mode": state.actual_mode,
                "agree": agree,
                "prev": prev,
                "goal": decision.goal,
                "rationale": decision.rationale,
                "hp_frac": round(state.hp_frac, 3),
                "weight_frac": round(state.weight_frac, 3),
                "gold": state.gold,
                "gold_rate_per_min": round(state.gold_rate_per_min, 2),
                "nearby_mobiles": state.nearby_mobiles,
                "danger_nearby": state.danger_nearby,
                "session_minutes": round(state.session_minutes, 1),
                "phase": state.phase,
                "recent_modes": state.last_modes,
                "location": list(state.location),
            }
            with _SHADOW_LOG.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
