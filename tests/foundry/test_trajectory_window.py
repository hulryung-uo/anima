"""window_start (scored-window) semantics of the kernel trajectory parser.

The GM fixed-start setup (teleport, [Set Skills.*.Base, [AddToPack) happens
BEFORE the scored window. These tests prove setup packets shift baselines but
never accrue: no fake skill gain, no produce_term credit for GM-given tools,
no action/movement counts, and duration measured from window_start.
"""

from __future__ import annotations

import json
import struct

from foundry.kernel.trajectory import parse_file

SELF = 0x000001AF
MINING = 45  # skill id (0-based, as carried by 0xFF single updates)


def _line(ts: float, direction: str, data: bytes) -> str:
    return json.dumps({
        "schema": "uo_proxy.packet.v1",
        "ts": ts,
        "direction": direction,
        "phase": "game",
        "pid": f"0x{data[0]:02X}",
        "size": len(data),
        "hex": data.hex(),
    })


def p_login_confirm(serial: int, x: int, y: int) -> bytes:
    d = bytearray(37)
    d[0] = 0x1B
    struct.pack_into(">I", d, 1, serial)
    struct.pack_into(">H", d, 11, x)
    struct.pack_into(">H", d, 13, y)
    return bytes(d)


def p_skill_single(skill_id: int, value10: int) -> bytes:
    # [0x3A][len u16][0xFF][id u16][value u16][base u16][lock u8][cap u16]
    d = bytearray(13)
    d[0] = 0x3A
    struct.pack_into(">H", d, 1, 13)
    d[3] = 0xFF
    struct.pack_into(">H", d, 4, skill_id)
    struct.pack_into(">H", d, 6, value10)
    struct.pack_into(">H", d, 8, value10)
    struct.pack_into(">H", d, 11, 1000)
    return bytes(d)


def p_add_to_container(item: int, graphic: int, amount: int, container: int) -> bytes:
    d = bytearray(21)
    d[0] = 0x25
    struct.pack_into(">I", d, 1, item)
    struct.pack_into(">H", d, 5, graphic)
    struct.pack_into(">H", d, 8, amount)
    struct.pack_into(">I", d, 13, container)
    return bytes(d)


def p_mobile_update(serial: int, x: int, y: int) -> bytes:
    d = bytearray(19)
    d[0] = 0x20
    struct.pack_into(">I", d, 1, serial)
    struct.pack_into(">H", d, 11, x)
    struct.pack_into(">H", d, 13, y)
    return bytes(d)


def p_walk_req(direction: int, seq: int) -> bytes:
    return bytes([0x02, direction, seq, 0, 0, 0, 0])


def p_confirm_walk() -> bytes:
    return bytes([0x22, 0, 0])


def _write(tmp_path, lines: list[str]):
    f = tmp_path / "traj.jsonl"
    f.write_text("\n".join(lines) + "\n")
    return f


def _fixture_lines() -> list[str]:
    """login at t=10, GM setup t=12..14, window starts t=20, play t=21..80."""
    return [
        # --- setup phase (pre-window) ---
        _line(10.0, "S->C", p_login_confirm(SELF, 2500, 400)),
        _line(11.0, "S->C", p_skill_single(MINING, 500)),     # creation: Mining 50.0
        _line(12.0, "S->C", p_mobile_update(SELF, 2553, 496)),  # GM teleport to mine
        _line(13.0, "S->C", p_skill_single(MINING, 350)),     # GM [Set Skills.Mining.Base 35
        _line(14.0, "S->C", p_add_to_container(0x4001, 0x0E85, 1, SELF)),  # GM pickaxe
        _line(15.0, "C->S", p_walk_req(0, 1)),                # stray pre-window action
        _line(15.5, "S->C", p_confirm_walk()),
        # --- scored window (window_start=20) ---
        _line(21.0, "C->S", p_walk_req(0, 2)),
        _line(21.5, "S->C", p_confirm_walk()),
        _line(30.0, "S->C", p_skill_single(MINING, 354)),     # +0.4 real gain
        _line(60.0, "S->C", p_add_to_container(0x4002, 0x19B5, 7, SELF)),  # mined ore
        _line(80.0, "S->C", p_skill_single(MINING, 358)),     # +0.4 more
    ]


def test_window_excludes_gm_setup(tmp_path):
    f = _write(tmp_path, _fixture_lines())
    s = parse_file(f, window_start=20.0)

    # skill baseline re-anchored by the GM set: gain measured from 35.0
    assert abs(s.skill_gain_total - 0.8) < 1e-9
    # GM pickaxe excluded; only the in-window ore counts
    assert [(g, a) for g, a, _ in s.items_into_pack] == [(0x19B5, 7)]
    # pre-window walk/confirm not counted
    assert s.steps_confirmed == 1
    assert s.action_counts.get("move", 0) == 1
    # duration measured from window_start, not first packet
    assert s.start_ts == 20.0
    assert s.end_ts == 80.0
    assert abs(s.duration_s - 60.0) < 1e-9
    # identity/ownership still established from baseline packets
    assert s.self_serial == SELF


def test_without_window_start_setup_contaminates(tmp_path):
    """Control: same file parsed without a window shows why we need one."""
    f = _write(tmp_path, _fixture_lines())
    s = parse_file(f)

    # gain measured from the 50.0 creation value -> clamped to 0 per skill,
    # but the GM pickaxe leaks into production and duration starts at login.
    graphics = [g for g, _a, _ts in s.items_into_pack]
    assert 0x0E85 in graphics
    assert s.start_ts == 10.0
    assert s.steps_confirmed == 2


def test_baseline_position_anchors_but_does_not_count(tmp_path):
    f = _write(tmp_path, _fixture_lines())
    s = parse_file(f, window_start=20.0)
    # pre-window teleport/login positions are anchors, not visited regions:
    # only the in-window walk contributes a position
    assert len(s.positions) == 1
    ts, x, y = s.positions[0]
    assert ts == 21.0
    # anchor carried over from the pre-window teleport (2553,496), walk dir 0
    assert (x, y) != (2500, 400)


def test_no_packets_in_window_means_zero_duration(tmp_path):
    f = _write(tmp_path, _fixture_lines()[:5])  # setup only
    s = parse_file(f, window_start=20.0)
    assert s.duration_s == 0.0
    assert s.total_actions == 0
    assert s.skill_gain_total == 0.0


def test_behavior_bonus_is_cell_aligned(tmp_path):
    """Tier 3 (FOUNDRY.md §5): exploration bonus carries NONE-profession
    archetypes only — a skill-gaining miner cannot fund its score by walking."""
    from foundry.kernel.fitness import WB_EXPLORE, compute_fitness
    from foundry.kernel.trajectory import TrajectorySummary

    explorer = TrajectorySummary(path="synthetic", start_ts=0.0, end_ts=600.0)
    explorer.positions = [(0.0, x * 8, 0) for x in range(200)]
    explorer.action_counts = {"move": 200, "use": 5}
    fb_explorer = compute_fitness(explorer)
    assert fb_explorer.behavior_bonus > 0.0  # NONE profession keeps the bonus

    f = _write(tmp_path, _fixture_lines())
    miner = parse_file(f, window_start=20.0)   # has Mining gain
    fb_miner = compute_fitness(miner)
    # the miner's bonus must not include the exploration term
    assert fb_miner.behavior_bonus < WB_EXPLORE * 1.0 + 1e-6


def p_damage(victim: int, amount: int) -> bytes:
    return struct.pack(">BIH", 0x0B, victim, amount)


def p_attack_req(target: int) -> bytes:
    return struct.pack(">BI", 0x05, target)


def p_mobile_moving(serial: int) -> bytes:
    d = bytearray(17)
    d[0] = 0x77
    struct.pack_into(">I", d, 1, serial)
    return bytes(d)


def test_damage_attribution_and_entity_census(tmp_path):
    """Dealt damage counts only against serials WE attacked; arena mobs
    mauling each other (and our own taken hits) stay out of damage_dealt.
    mobiles_seen counts distinct non-self entities incl. setup phase."""
    MOB_A, MOB_B = 0x300, 0x301
    lines = [
        _line(10.0, "S->C", p_login_confirm(SELF, 2500, 400)),
        _line(12.0, "S->C", p_mobile_moving(MOB_A)),     # setup-phase sighting
        # window
        _line(21.0, "C->S", p_attack_req(MOB_A)),        # we attack A
        _line(22.0, "S->C", p_damage(MOB_A, 11)),        # dealt
        _line(23.0, "S->C", p_damage(SELF, 7)),          # taken
        _line(24.0, "S->C", p_damage(MOB_B, 25)),        # mob-vs-mob: ignored
        _line(25.0, "S->C", p_mobile_moving(MOB_B)),
    ]
    s = parse_file(_write(tmp_path, lines), window_start=20.0)
    assert s.damage_dealt == 11
    assert s.damage_taken == 7
    assert s.entities_seen == 2          # A and B, self excluded
