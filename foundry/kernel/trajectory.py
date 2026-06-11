"""Independent UO trajectory parser — the kernel's ground-truth reader.

Parses a uo_proxy JSONL trajectory (one packet per line, schema
``uo_proxy.packet.v1``) into a structured :class:`TrajectorySummary` that
fitness.py and descriptor.py consume.

This module re-implements packet decoding from raw bytes using only the stdlib.
It deliberately does NOT import anything from ``anima/`` — see
``foundry/kernel/__init__.py`` for why (anti-gaming: the mutator can edit anima,
so the kernel must read the wire itself). Byte offsets were verified against
ClassicUO and the anima reference parser, and against real captured packets.

Run standalone to inspect a trajectory:
    python3 -m foundry.kernel.trajectory data/trajectories/<file>.jsonl
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

from foundry.kernel import uoconst

# --- packet ids we decode ---------------------------------------------------
P_DAMAGE = 0x0B
P_WALK_REQ = 0x02           # C->S
P_STATUS = 0x11             # S->C full character status (hp, stats, gold, weight)
P_LOGIN_CONFIRM = 0x1B      # S->C self serial + start position
P_MOBILE_UPDATE = 0x20      # S->C position reset (self or other)
P_DENY_WALK = 0x21          # S->C
P_CONFIRM_WALK = 0x22       # S->C
P_ADD_TO_CONTAINER = 0x25   # S->C item placed in a container
P_EQUIP = 0x2E              # S->C equipped item
P_SKILLS = 0x3A             # S->C skill list / single update
P_CONTAINER_CONTENT = 0x3C  # S->C full container contents
P_ATTACK = 0x05             # both directions
P_HP = 0xA1                 # S->C current health
P_ASCII_TALK = 0x1C         # S->C speech
P_UNICODE_TALK = 0xAE       # S->C speech
P_CLILOC = 0xC1             # S->C localized speech
P_MOBILE_INCOMING = 0x78    # S->C mobile (incl. self at login) + equipment
P_MOBILE_MOVING = 0x77      # S->C other mobile moved into/within view

REGION_SHIFT = 3  # 8x8-tile region buckets for mobility (1 << 3 == 8)


class _Reader:
    """Minimal big-endian byte reader (UO is big-endian)."""

    __slots__ = ("_d", "_p")

    def __init__(self, data: bytes, start: int = 0) -> None:
        self._d = data
        self._p = start

    @property
    def remaining(self) -> int:
        return len(self._d) - self._p

    def u8(self) -> int:
        v = self._d[self._p]
        self._p += 1
        return v

    def i8(self) -> int:
        v = struct.unpack_from(">b", self._d, self._p)[0]
        self._p += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from(">H", self._d, self._p)[0]
        self._p += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from(">I", self._d, self._p)[0]
        self._p += 4
        return v

    def skip(self, n: int) -> None:
        self._p += n

    def ascii(self, n: int) -> str:
        raw = self._d[self._p : self._p + n]
        self._p += n
        return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


@dataclass
class SkillStat:
    id: int
    name: str
    first: float
    last: float

    @property
    def gain(self) -> float:
        return max(0.0, self.last - self.first)


@dataclass
class TrajectorySummary:
    """Everything fitness/descriptor need, derived purely from the wire."""

    path: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    self_serial: int | None = None

    # skills (fitness backbone + profession_focus)
    skills: dict[int, SkillStat] = field(default_factory=dict)

    # vitals
    hp_samples: list[tuple[float, int, int]] = field(default_factory=list)  # (ts, hits, hits_max)
    death_count: int = 0

    # economy
    gold_samples: list[tuple[float, int]] = field(default_factory=list)
    weight_samples: list[tuple[float, int, int]] = field(default_factory=list)

    # production: items added to a self-owned container, as (graphic, amount, ts)
    items_into_pack: list[tuple[int, int, float]] = field(default_factory=list)

    # movement (self path reconstructed from walk reqs + position anchors)
    positions: list[tuple[float, int, int]] = field(default_factory=list)  # (ts, x, y)
    steps_confirmed: int = 0
    steps_denied: int = 0

    # actions (descriptor denominators)
    action_counts: dict[str, int] = field(default_factory=dict)

    # social
    speech_sent: int = 0
    speech_recv: int = 0

    # combat
    attacks_initiated: int = 0
    damage_dealt: int = 0
    damage_taken: int = 0
    attacked_serials: set[int] = field(default_factory=set)

    # environment: distinct non-self mobiles that entered view during the
    # window (mobs + NPCs + players — the wire doesn't distinguish). 0 means
    # the workplace is EMPTY: combat/social/begging hypotheses have no targets.
    mobiles_seen: set[int] = field(default_factory=set)

    # provenance / health of the parse
    packet_counts: dict[str, int] = field(default_factory=dict)
    line_count: int = 0
    parse_errors: int = 0

    # ---- derived metrics -------------------------------------------------
    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def duration_h(self) -> float:
        return self.duration_s / 3600.0

    @property
    def skill_gain_total(self) -> float:
        return sum(s.gain for s in self.skills.values())

    @property
    def entities_seen(self) -> int:
        """Distinct non-self mobiles observed (environment, incl. setup phase)."""
        s = set(self.mobiles_seen)
        if self.self_serial is not None:
            s.discard(self.self_serial)
        return len(s)

    @property
    def gold_delta(self) -> int:
        if len(self.gold_samples) < 1:
            return 0
        return self.gold_samples[-1][1] - self.gold_samples[0][1]

    @property
    def total_actions(self) -> int:
        return sum(self.action_counts.values())

    @property
    def unique_regions(self) -> int:
        return len({(x >> REGION_SHIFT, y >> REGION_SHIFT) for _, x, y in self.positions})

    def alive_fraction(self) -> float:
        """Fraction of the eval window spent alive, from the HP timeline.

        Each HP sample at hits==0 starts a "dead" interval until the next
        sample with hits>0 (resurrection) or end of window. Graded, not binary.
        If HP is never observed at all we assume alive (no evidence of death).
        """
        if not self.hp_samples or self.duration_s <= 0:
            return 1.0
        dead_s = 0.0
        prev_ts = self.start_ts
        prev_dead = False
        for ts, hits, _ in self.hp_samples:
            if prev_dead:
                dead_s += max(0.0, ts - prev_ts)
            prev_ts = ts
            prev_dead = hits <= 0
        if prev_dead:
            dead_s += max(0.0, self.end_ts - prev_ts)
        return max(0.0, min(1.0, 1.0 - dead_s / self.duration_s))

    def profession_skill_gains(self) -> dict[str, float]:
        """Total skill gain per profession category (FOUNDRY.md §4)."""
        out: dict[str, float] = {}
        for s in self.skills.values():
            cat = uoconst.SKILL_CATEGORY.get(s.id)
            if cat and s.gain > 0:
                out[cat] = out.get(cat, 0.0) + s.gain
        return out

    def top_skill(self) -> SkillStat | None:
        gained = [s for s in self.skills.values() if s.gain > 0]
        return max(gained, key=lambda s: s.gain) if gained else None


def parse_file(path: str | Path, window_start: float = 0.0) -> TrajectorySummary:
    """Parse a uo_proxy JSONL trajectory into a TrajectorySummary.

    ``window_start`` (unix ts) marks the start of the SCORED eval window.
    Packets before it are parsed in *baseline mode*: they establish state
    (self serial, owned containers, skill/gold/position baselines) but accrue
    nothing — no actions, no skill gain, no items, no movement. This is what
    keeps GM fixed-start setup (teleport, [Set Skills.*, [AddToPack) from
    contaminating fitness: a pre-window skill set shifts the baseline instead
    of counting as gain, and a GM-given tool never enters produce_term.
    """
    summ = TrajectorySummary(path=str(path))
    # Per-trajectory mutable parse state.
    facing = 0
    cur_x: int | None = None
    cur_y: int | None = None
    owned_containers: set[int] = set()

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            summ.line_count += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                summ.parse_errors += 1
                continue
            if ev.get("schema") != "uo_proxy.packet.v1":
                continue

            ts = float(ev.get("ts", 0.0))
            baseline = ts < window_start
            if not baseline:
                if summ.start_ts == 0.0:
                    summ.start_ts = window_start if window_start > 0.0 else ts
                summ.end_ts = ts

            direction = ev.get("direction", "")
            pid_str = ev.get("pid", "0x00")
            try:
                pid = int(pid_str, 16)
            except ValueError:
                continue
            key = f"{direction}:{pid_str}"
            summ.packet_counts[key] = summ.packet_counts.get(key, 0) + 1

            try:
                data = bytes.fromhex(ev.get("hex", ""))
            except ValueError:
                summ.parse_errors += 1
                continue
            if not data:
                continue

            try:
                if direction == "C->S":
                    facing, cur_x, cur_y = _on_client(
                        summ, pid, data, ts, facing, cur_x, cur_y, baseline
                    )
                else:  # "S->C"
                    cur_x, cur_y = _on_server(
                        summ, pid, data, ts, cur_x, cur_y, owned_containers, baseline
                    )
            except (IndexError, struct.error):
                summ.parse_errors += 1
                continue

    return summ


def _on_client(
    summ: TrajectorySummary, pid: int, data: bytes, ts: float,
    facing: int, cur_x: int | None, cur_y: int | None,
    baseline: bool = False,
) -> tuple[int, int | None, int | None]:
    """Decode a client->server (agent action) packet."""
    if not baseline:
        group = uoconst.ACTION_GROUP.get(pid)
        if group:
            summ.action_counts[group] = summ.action_counts.get(group, 0) + 1

    if pid == P_WALK_REQ and len(data) >= 3:
        d = data[1] & 0x07
        # UO movement: a walk request first turns to face d; it only moves if
        # already facing d. Reconstruct self path optimistically (DenyWalk
        # reverts via the 0x21 handler).
        if d == facing and cur_x is not None and cur_y is not None:
            dx, dy = uoconst.DIR_DELTA.get(d, (0, 0))
            cur_x += dx
            cur_y += dy
            if not baseline:
                summ.positions.append((ts, cur_x, cur_y))
        facing = d
    elif pid in (uoconst.CS_SPEECH_UNICODE, uoconst.CS_SPEECH_ASCII):
        # 0xAD/0x03 are client speech here (direction disambiguates from any
        # same-numbered server packet).
        if not baseline:
            summ.speech_sent += 1
    elif pid == P_ATTACK:
        if len(data) >= 5:
            summ.attacked_serials.add(struct.unpack_from(">I", data, 1)[0])
        if not baseline:
            summ.attacks_initiated += 1

    return facing, cur_x, cur_y


def _on_server(
    summ: TrajectorySummary, pid: int, data: bytes, ts: float,
    cur_x: int | None, cur_y: int | None, owned: set[int],
    baseline: bool = False,
) -> tuple[int | None, int | None]:
    """Decode a server->client packet, updating ground-truth state.

    ``baseline`` packets (pre-window) update identity/ownership/position state
    and *baselines* (skill first==last, latest gold/weight) but accrue nothing.
    """
    if pid == P_LOGIN_CONFIRM and len(data) >= 15:
        summ.self_serial = struct.unpack_from(">I", data, 1)[0]
        cur_x = struct.unpack_from(">H", data, 11)[0]
        cur_y = struct.unpack_from(">H", data, 13)[0]
        owned.add(summ.self_serial)
        if not baseline:
            summ.positions.append((ts, cur_x, cur_y))

    elif pid == P_STATUS and len(data) >= 43:
        r = _Reader(data, 3)
        serial = r.u32()
        r.ascii(30)            # name
        hits = r.u16()
        hits_max = r.u16()
        r.skip(1)              # name change flag
        flag = r.u8()
        if summ.self_serial is None:
            summ.self_serial = serial  # status with full block is almost always self
            owned.add(serial)
        if serial == summ.self_serial:
            if not baseline:
                summ.hp_samples.append((ts, hits, hits_max))
                if hits <= 0:
                    summ.death_count += 1
            if flag >= 1 and r.remaining >= 19:
                r.skip(1)    # sex
                r.skip(6)    # str / dex / int
                r.skip(4)    # stam / stam_max
                r.skip(4)    # mana / mana_max
                gold = r.u32()
                r.u16()                          # armor
                weight = r.u16()
                weight_max = 0
                if flag >= 5 and r.remaining >= 2:
                    weight_max = r.u16()
                if baseline:
                    # keep only the latest pre-window sample as the baseline
                    summ.gold_samples[:] = [(ts, gold)]
                    summ.weight_samples[:] = [(ts, weight, weight_max)]
                else:
                    summ.gold_samples.append((ts, gold))
                    summ.weight_samples.append((ts, weight, weight_max))

    elif pid == P_HP and len(data) >= 9:
        serial = struct.unpack_from(">I", data, 1)[0]
        hits_max = struct.unpack_from(">H", data, 5)[0]
        hits = struct.unpack_from(">H", data, 7)[0]
        if serial == summ.self_serial and not baseline:
            summ.hp_samples.append((ts, hits, hits_max))
            if hits <= 0:
                summ.death_count += 1

    elif pid == P_SKILLS and len(data) >= 4:
        _decode_skills(summ, data, ts, baseline)

    elif pid == P_MOBILE_UPDATE and len(data) >= 19:
        # [0x20][serial][body u16][0][hue u16][flags][x u16@11][y u16@13][0 u16][dir][z i8@18]
        # (offsets verified against live ServUO captures; x@11/y@13, NOT 8/10)
        serial = struct.unpack_from(">I", data, 1)[0]
        if serial == summ.self_serial:
            x = struct.unpack_from(">H", data, 11)[0]
            y = struct.unpack_from(">H", data, 13)[0]
            cur_x, cur_y = x, y
            if not baseline:
                summ.positions.append((ts, x, y))

    elif pid == P_MOBILE_INCOMING and len(data) >= 19:
        # self at login carries our equipment (backpack -> owned container)
        serial = struct.unpack_from(">I", data, 3)[0]
        summ.mobiles_seen.add(serial)   # environment census (self filtered later)
        if summ.self_serial is None:
            summ.self_serial = serial
        if serial == summ.self_serial:
            owned.add(serial)
            _scan_equipment_for_pack(data, serial, owned)

    elif pid == P_MOBILE_MOVING and len(data) >= 5:
        summ.mobiles_seen.add(struct.unpack_from(">I", data, 1)[0])

    elif pid == P_EQUIP and len(data) >= 15:
        serial = struct.unpack_from(">I", data, 1)[0]
        layer = data[8]
        parent = struct.unpack_from(">I", data, 9)[0]
        if parent == summ.self_serial and layer == uoconst.LAYER_BACKPACK:
            owned.add(serial)

    elif pid == P_CONFIRM_WALK:
        if not baseline:
            summ.steps_confirmed += 1

    elif pid == P_DENY_WALK and len(data) >= 8:
        # server corrects our position
        x = struct.unpack_from(">H", data, 2)[0]
        y = struct.unpack_from(">H", data, 4)[0]
        cur_x, cur_y = x, y
        if not baseline:
            summ.steps_denied += 1

    elif pid == P_ADD_TO_CONTAINER and len(data) >= 21:
        r = _Reader(data, 1)
        item_serial = r.u32()
        graphic = r.u16()
        r.skip(1)
        amount = r.u16()
        r.skip(2)                # x, y
        r.skip(1)                # grid index
        container = r.u32()
        if container in owned:
            if not baseline:
                summ.items_into_pack.append((graphic, amount or 1, ts))
            owned.add(item_serial)  # nested bag tracking

    elif pid in (P_ASCII_TALK, P_UNICODE_TALK, P_CLILOC):
        if not baseline:
            summ.speech_recv += 1

    elif pid == P_DAMAGE and len(data) >= 7:
        # [0x0B][victim serial u32 @1][amount u16 @5] — verified on live
        # captures (the old @3 read produced garbage serials, so self-damage
        # was never recognized and EVERYTHING counted as dealt).
        serial = struct.unpack_from(">I", data, 1)[0]
        amount = struct.unpack_from(">H", data, 5)[0]
        if not baseline:
            if serial == summ.self_serial:
                summ.damage_taken += amount
            elif serial in summ.attacked_serials:
                # only damage to mobiles WE attacked counts as dealt —
                # 0x0B broadcasts every hit in view, and arena mobs mauling
                # each other once inflated damage_rate to ~370k/h.
                summ.damage_dealt += amount

    return cur_x, cur_y


def _scan_equipment_for_pack(data: bytes, self_serial: int, owned: set[int]) -> None:
    """Walk the equipment list trailing a 0x78 self-incoming to find the pack."""
    r = _Reader(data, 3)
    try:
        r.skip(11)   # serial, body, x, y, z
        r.skip(5)    # dir, hue, flags, notoriety
        while r.remaining >= 7:
            item_serial = r.u32()
            if item_serial == 0:
                break
            r.u16()                                  # graphic
            layer = r.u8()
            if layer == uoconst.LAYER_BACKPACK:
                owned.add(item_serial)
    except (IndexError, struct.error):
        pass


def _decode_skills(
    summ: TrajectorySummary, data: bytes, ts: float, baseline: bool = False
) -> None:
    """0x3A SkillUpdate — full list or single skill change.

    list_type: 0x00/0x01 full (no cap), 0x02/0x03 full (with cap),
    0xDF/0xFF single (with cap), 0xFE name list (ignored).
    Full lists are 1-based (id -= 1) and terminated by skill_id == 0.
    In ``baseline`` mode (pre-window) every observation re-anchors
    first == last, so a GM [Set Skills.*.Base during setup is a baseline
    shift, never measured gain.
    """
    r = _Reader(data, 3)
    list_type = r.u8()
    if list_type == 0xFE:
        return
    is_single = list_type in (0xDF, 0xFF)
    has_cap = list_type in (0x02, 0x03, 0xDF, 0xFF)
    adjust = list_type in (0x00, 0x01, 0x02, 0x03)

    while r.remaining >= 2:
        skill_id = r.u16()
        if not is_single and skill_id == 0:
            break
        if r.remaining < 5:
            break
        value = r.u16()
        r.u16()           # base
        r.u8()            # lock
        if has_cap and r.remaining >= 2:
            r.u16()       # cap
        if adjust:
            skill_id -= 1
        if skill_id < 0:
            continue
        val = value / 10.0
        st = summ.skills.get(skill_id)
        if st is None:
            summ.skills[skill_id] = SkillStat(
                id=skill_id,
                name=uoconst.SKILL_NAMES.get(skill_id, f"Skill {skill_id}"),
                first=val, last=val,
            )
        elif baseline:
            st.first = st.last = val
        else:
            st.last = val
        if is_single:
            break


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python3 -m foundry.kernel.trajectory <trajectory.jsonl>")
        return 2
    summ = parse_file(argv[0])
    print(f"file              {summ.path}")
    print(f"lines             {summ.line_count}  (parse_errors={summ.parse_errors})")
    print(f"duration          {summ.duration_s:.1f}s ({summ.duration_h:.3f}h)")
    print(f"self_serial       {summ.self_serial and hex(summ.self_serial)}")
    print(f"skills tracked    {len(summ.skills)}  total_gain={summ.skill_gain_total:.1f}")
    top = summ.top_skill()
    if top:
        print(f"top skill         {top.name} +{top.gain:.1f} ({top.first:.1f}->{top.last:.1f})")
    print(f"profession gains  {summ.profession_skill_gains()}")
    print(f"alive_fraction    {summ.alive_fraction():.3f}  deaths={summ.death_count}")
    print(f"gold              {len(summ.gold_samples)} samples  delta={summ.gold_delta}")
    print(f"items into pack   {len(summ.items_into_pack)}")
    print(f"positions         {len(summ.positions)}  unique_regions={summ.unique_regions}")
    print(f"steps             confirmed={summ.steps_confirmed} denied={summ.steps_denied}")
    print(f"actions           total={summ.total_actions}  {summ.action_counts}")
    print(f"speech            sent={summ.speech_sent} recv={summ.speech_recv}")
    print(f"combat            attacks={summ.attacks_initiated} "
          f"dealt={summ.damage_dealt} taken={summ.damage_taken}")
    top_pkts = sorted(summ.packet_counts.items(), key=lambda kv: -kv[1])[:12]
    print(f"top packets       {top_pkts}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
