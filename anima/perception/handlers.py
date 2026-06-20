"""Packet handlers that update Perception state.

All handlers are synchronous — they mutate state in-place with no I/O.
"""

from __future__ import annotations

import re
import struct
import time
import zlib

import structlog

from anima.client.codec import PacketReader
from anima.client.handler import PacketHandler
from anima.data import cliloc_text
from anima.perception import Perception
from anima.perception.enums import Direction, Lock, MessageType, MobileFlags, NotorietyFlag
from anima.perception.event_stream import GameEventType
from anima.perception.gump import parse_layout
from anima.perception.self_state import SkillInfo, VendorBuyItem, VendorSellItem
from anima.perception.walker import WalkerManager

logger = structlog.get_logger()

# AffixType flags carried by 0xCC SendLocalizedMessageAffix.
_AFFIX_PREPEND = 0x01
_AFFIX_SYSTEM = 0x02


def _store_gump(self_state, gump_id: int, gump) -> None:
    """Store a freshly-received gump so dict insertion order tracks recency.

    ``ss.gumps`` is keyed by the gump TYPE id. ``wait_for_gump`` (and the
    craft/vendor/res flows built on it) document and rely on "the last-inserted
    key is the newest arrival" — it picks ``next(gid for gid in reversed(...))``
    as the most recent gump.

    A plain ``gumps[gump_id] = gump`` honours that ONLY for a brand-new id. When
    the id is ALREADY present it overwrites the value in place but keeps the
    key's ORIGINAL position, so a re-sent/refreshed gump is *not* recognised as
    the newest. Craft gumps in particular refresh under a fixed type id after
    each click: with another gump still on screen, the refreshed craft gump kept
    its old (earlier) slot, ``wait_for_gump`` handed back the other (stale)
    window, and ``craft_via_gump`` clicked against the wrong gump's
    serial/buttons — matching no button (ServUO "Invalid gump response,
    disconnecting...") or driving the wrong window. Pop first so a refreshed
    gump moves to the end and reads as the genuine most-recent arrival.
    """
    self_state.gumps.pop(gump_id, None)
    self_state.gumps[gump_id] = gump


def _decode_cliloc_args(raw: bytes, *, big_endian: bool) -> str:
    """Decode the UTF-16 argument blob trailing a 0xC1 / 0xCC cliloc packet.

    Mirrors ClassicUO's DisplayClilocString (PacketHandlers.cs:4768-4779):
    it reads ``remains / 2`` UTF-16 code units via ReadUnicode{LE,BE}, which
    (a) floors an odd trailing byte instead of choking on it, and (b) stops at
    the first NUL because ReadUnicode is a C-string read.

    The previous inline ``raw.decode("utf-16-le").rstrip("\\x00")`` diverged on
    both counts: a buffer with an odd length (the codec frames the variable
    packet by its declared length, which routinely leaves a 1-byte pad after
    the NUL terminator) raised UnicodeDecodeError — caught and turned into an
    EMPTY arg string, so the cliloc rendered with blank ~N~ placeholders (e.g.
    a combat / loot / skill / vendor message arrived as bare ``[cliloc N]`` or
    ``": "``). And rstrip only trims TRAILING NULs, so any padding AFTER an
    embedded NUL terminator leaked into the args. Floor to whole code units,
    decode with errors="replace" (never raises), then cut at the first NUL.
    """
    if len(raw) & 1:
        raw = raw[:-1]  # ClassicUO's `remains / 2` floors the odd tail byte
    enc = "utf-16-be" if big_endian else "utf-16-le"
    text = raw.decode(enc, errors="replace")
    # ReadUnicode{LE,BE} terminates at the first NUL — match that, don't merely
    # strip trailing NULs (which would keep post-terminator padding/garbage).
    return text.split("\x00", 1)[0]


def _resolve_cliloc_text(cliloc_num: int, args: str) -> str:
    """Resolve a cliloc id + tab-separated args into display text.

    Mirrors ClassicUO ClilocLoader.Translate (ClilocLoader.cs:121-283): each
    ``~N~`` / ``~N_label~`` placeholder is resolved by the index *embedded in
    the placeholder* (the (N-1)th tab-separated arg) — never by the positional
    order of the args — so out-of-order AND repeated placeholders all resolve.
    Drives substitution off the placeholder index exactly like the 0xD6
    MegaCliloc handler already does, so the 0xC1 / 0xCC system-message path
    (combat / loot / skill / vendor lines) decodes identically.

    The previous loop walked the args positionally and did ``re.sub(...,
    count=1)``, replacing only the FIRST occurrence of each ``~N~``. A cliloc
    that references one arg twice — e.g. 1156056 ``"~1_CHANCE~% chance to
    reduce incoming damage by ~2_DAMAGE~%. Costs ~2_DAMAGE~% of original damage
    in mana."`` — left the SECOND ``~2_DAMAGE~`` unfilled, which the trailing
    cleanup regex below then stripped to empty ("Costs % of original damage"),
    silently corrupting the message. It also did a bogus ``text.replace("#N",
    arg)`` against the BASE string; ClassicUO only treats a leading ``#`` on an
    *arg value* as a nested-cliloc ref, never the base text, so that line could
    only mangle literal ``#N`` art names. Both are dropped here.
    """
    base_text = cliloc_text(cliloc_num)
    if base_text and args:
        parts = args.split("\t")

        def _sub(m: "re.Match[str]") -> str:
            idx = int(m.group(1)) - 1
            return parts[idx] if 0 <= idx < len(parts) else m.group(0)

        text = re.sub(r"~(\d+)(?:_[^~]*)?~", _sub, base_text)
    elif base_text:
        text = base_text
    else:
        text = f"[cliloc {cliloc_num}]"
    return re.sub(r"~\d+[^~]*~", "", text).strip()


def _decode_notoriety(raw: int) -> NotorietyFlag:
    """Decode a wire notoriety byte into a NotorietyFlag, mirroring ClassicUO.

    The notoriety byte on 0x77 / 0x78 / 0x20 / 0x22 may carry the 0x40
    "temporary"/overlay bit on top of the 1..7 flag value (ClassicUO's
    ConfirmWalk strips it with ``noto & ~0x40``; UpdateGameObject assigns the
    flag unconditionally). The mobile handlers previously gated the assignment
    behind ``1 <= notoriety <= 7`` and so SILENTLY DROPPED any byte with the
    0x40 bit set (e.g. 0x46 = MURDERER|0x40, 0x44 = CRIMINAL|0x40, 0x47 =
    INVULNERABLE|0x40). The mobile then kept its stale default INNOCENT, and the
    combat / flee / social notoriety gates that read ``mob.notoriety`` saw a red
    murderer as a blue innocent. Strip the overlay and coerce an out-of-range
    value (0 or >=8) back to INNOCENT — identical to the existing 0x22
    ConfirmWalk decode for the player's own notoriety.
    """
    noto = raw & ~0x40
    if noto == 0 or noto >= 8:
        noto = 1
    return NotorietyFlag(noto)


def register_handlers(
    handler: PacketHandler,
    perception: Perception,
    walker: WalkerManager,
) -> None:
    """Wire all packet handlers into the dispatch registry."""
    p = perception  # shorthand

    # ------------------------------------------------------------------
    # Entity packets
    # ------------------------------------------------------------------

    def handle_mobile_incoming(packet_id: int, data: bytes) -> None:
        """0x78 MobileIncoming — a mobile enters our view."""
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        body = r.read_u16()
        x = r.read_u16()
        y = r.read_u16()
        z = r.read_i8()
        direction = r.read_u8()
        hue = r.read_u16()
        flags = r.read_u8()
        notoriety = r.read_u8()

        is_self = serial == p.self_state.serial
        if is_self:
            # Don't track self in world mobiles, but DO keep reading: the
            # worn-item list still follows our own 0x78. ServUO emits a self
            # MobileIncoming on enter-world and whenever our appearance/gear
            # changes (Mobile.SendEverything), and ClassicUO's UpdateObject
            # parses that item loop for world.Player too — assigning each
            # worn item's Container/Layer. Returning here dropped the whole
            # equipment block, so self_state.equipment never filled from 0x78
            # and the "hand already occupied / already equipped" guards in
            # equip_weapon_from_pack / equip_item saw an empty loadout (or a
            # stale one after a gear swap) — re-equipping when already armed
            # or mis-judging a free hand mid-fight.
            walker.sync_position(x, y, z, direction & 0x07)
            p.self_state.body = body
            # ServUO pushes self 0x78 on flag changes (OnHiddenChanged) —
            # this is where the hidden bit (0x80) for our own char arrives.
            p.self_state.flags = MobileFlags(flags & 0xFF)
            # The yellow/blessed bar (flag 0x08) rides in this same byte.
            # ServUO sets it for `m_Blessed || m_YellowHealthbar`, but it only
            # emits a standalone 0x17 HealthbarYellow when the YellowHealthbar
            # *property* toggles — never for a statically Blessed mobile. So a
            # self that the server blesses (or yellow-bars) arrives here with
            # bit 0x08 set and no paired 0x17, leaving is_yellow_health stale at
            # False. Mirror ClassicUO's Mobile.IsYellowHits (= Flags &
            # Flags.YellowBar) and derive it from the flags byte too, so a flag
            # delta and a 0x17 toggle stay in agreement.
            p.self_state.is_yellow_health = bool(flags & MobileFlags.YELLOW_BAR)
            # The self 0x78 is a FULL, authoritative loadout: ClassicUO's
            # UpdateObject strips every worn item off the mobile (except the
            # backpack / opened containers) BEFORE replaying the item loop, so a
            # layer no longer present in the new list is dropped. Without an
            # equivalent purge here, a gear change that REMOVES a layer (e.g.
            # the agent drops a one-hander, or a two-hander replaces a 1H +
            # shield) left the old serial lingering in self_state.equipment[1]/
            # [2] whenever the server refreshed appearance without a paired 0x1D
            # Delete. The combat-loop "hand already occupied / already equipped"
            # guards (procedures/combat_loop.py) then saw a phantom weapon and
            # never re-armed. Clear all layers but the backpack (Layer 0x15),
            # which ClassicUO preserves and the rest of the codebase relies on
            # as the inventory root; the loop below refills the worn layers.
            for layer in list(p.self_state.equipment):
                if layer != 0x15:
                    del p.self_state.equipment[layer]
            # Clearing the equipment dict alone is NOT enough: our own worn
            # items also live in world.items with container == self.serial (set
            # by the shared item loop below). The non-self branch purges those
            # stale orphans; the self branch did not, so a removed layer left
            # the old serial lingering in world.items forever (the gear item
            # often gets no 0x1D Delete on a gear swap). That orphan then
            # resurrects the very phantom this purge exists to kill: main.py's
            # equipment-recovery fallback rebuilds equipment[it.layer] =
            # it.serial from `it.container == self.serial and it.layer > 0`, so
            # the dropped weapon walks straight back into equipment[1]/[2] and
            # the combat-loop "hand already occupied" guard sees it. Mirror the
            # non-self branch (and ClassicUO's strip-then-replay): drop our worn
            # items from world.items too, keeping only the backpack (Layer 0x15).
            for stale in [
                it.serial
                for it in p.world.items.values()
                if it.container == serial and it.layer != 0x15
            ]:
                p.world.remove(stale)
        else:
            mob = p.world.get_or_create_mobile(serial)
            mob.body = body
            mob.x = x
            mob.y = y
            mob.z = z
            mob.direction = Direction.from_byte(direction)
            mob.hue = hue
            mob.flags = MobileFlags(flags & 0xFF)
            mob.is_yellow_health = bool(flags & MobileFlags.YELLOW_BAR)
            mob.notoriety = _decode_notoriety(notoriety)
            # A 0x78 is a FULL, authoritative loadout for ANY mobile, not just
            # self. ClassicUO's UpdateObject strips every worn item off the
            # entity (except an opened container / the backpack at Layer 0x15)
            # BEFORE replaying the item loop, so a layer no longer present in
            # the new list is dropped (PacketHandlers.UpdateObject). The self
            # branch above already mirrors this for our own gear; the non-self
            # branch did not. So when an NPC/player swaps gear off-screen and
            # re-enters view — or the server refreshes its appearance on an
            # equip change — the OLD worn serials lingered in world.items with
            # container==serial forever (the gear item itself often gets no
            # 0x1D Delete). Those orphans corrupt graphic-based equipment
            # lookups for that mobile and leak memory across a long eval run,
            # the same class of bug the 0x3C container refresh already guards.
            # Purge this mobile's worn items (keep the backpack, Layer 0x15)
            # before the shared equipment loop below refills them.
            for stale in [
                it.serial
                for it in p.world.items.values()
                if it.container == serial and it.layer != 0x15
            ]:
                p.world.remove(stale)

        # Parse equipment items that follow. ServUO's NewMobileIncoming
        # (the default 0x78 format for modern/ClassicUO clients, protocol
        # 70331+) writes each worn item as a FIXED record:
        #   serial(u32) + graphic(u16) + layer(u8) + hue(u16)
        # The hue is ALWAYS present and the graphic keeps all 16 bits — there
        # is NO 0x8000 "has-hue" flag in this format (that belonged to the old
        # MobileIncomingOld/SA layout). The previous code only consumed the
        # hue when graphic & 0x8000, so for every ordinary item (graphic <
        # 0x8000) it left the 2 hue bytes on the wire and the next read_u32()
        # picked up [hue][next_serial_hi] — desyncing every following equipment
        # entry into garbage items that polluted world.items. Read hue
        # unconditionally to match ServUO MobileIncoming / ClassicUO's
        # CV_70331 branch in UpdateObject.
        while r.remaining >= 4:
            item_serial = r.read_u32()
            if item_serial == 0:
                break
            if r.remaining < 5:  # need graphic(2) + layer(1) + hue(2)
                break
            graphic = r.read_u16()
            layer = r.read_u8()
            hue = r.read_u16()
            item = p.world.get_or_create_item(item_serial)
            item.graphic = graphic
            item.hue = hue
            item.layer = layer
            item.container = serial

            # If this is our character, register equipment
            if is_self and layer > 0:
                p.self_state.equipment[layer] = item_serial

        p.emit(GameEventType.MOBILE_APPEARED, {"serial": serial, "x": x, "y": y})
        logger.debug(
            "mobile_incoming",
            serial=f"0x{serial:08X}",
            body=f"0x{body:04X}",
            pos=f"({x},{y},{z})",
        )

    handler.register(0x78, handle_mobile_incoming)

    def handle_mobile_moving(packet_id: int, data: bytes) -> None:
        """0x77 MobileMoving — a mobile moves."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        body = r.read_u16()
        x = r.read_u16()
        y = r.read_u16()
        z = r.read_i8()
        direction = r.read_u8()
        hue = r.read_u16()
        flags = r.read_u8()
        notoriety = r.read_u8()

        if serial == p.self_state.serial:
            # Position for self is tracked by confirm_walk / deny_walk / 0x20,
            # but the flags byte still matters: ServUO sends 0x77 for our own
            # mobile (Mobile.cs SendEverything `ourState`) whenever a movement
            # OR a flag delta (war mode, frozen/paralyzed, flying, hidden) is
            # processed. ClassicUO's UpdateCharacter assigns `mobile.Flags`
            # for self here too; dropping it left self_state.in_war_mode /
            # .hidden stale until the next 0x78/0x20.
            p.self_state.body = body
            p.self_state.flags = MobileFlags(flags & 0xFF)
            p.self_state.is_yellow_health = bool(flags & MobileFlags.YELLOW_BAR)
            return

        mob = p.world.get_or_create_mobile(serial)
        mob.body = body
        mob.x = x
        mob.y = y
        mob.z = z
        mob.direction = Direction.from_byte(direction)
        mob.hue = hue
        mob.flags = MobileFlags(flags & 0xFF)
        mob.is_yellow_health = bool(flags & MobileFlags.YELLOW_BAR)
        mob.notoriety = _decode_notoriety(notoriety)

        p.emit(GameEventType.MOBILE_MOVED, {"serial": serial, "x": x, "y": y})

    handler.register(0x77, handle_mobile_moving)

    def handle_mobile_update(packet_id: int, data: bytes) -> None:
        """0x20 MobileUpdate — position/appearance reset."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        body = r.read_u16()
        r.skip(1)  # graphic_inc
        hue = r.read_u16()
        flags = r.read_u8()
        x = r.read_u16()
        y = r.read_u16()
        r.skip(2)  # server_id
        direction = r.read_u8() & 0x07
        z = r.read_i8()

        if serial == p.self_state.serial:
            walker.sync_position(x, y, z, direction)
            walker.steps_count = 0
            walker.walking_failed = False
            p.self_state.body = body
            p.self_state.flags = MobileFlags(flags & 0xFF)
            p.self_state.is_yellow_health = bool(flags & MobileFlags.YELLOW_BAR)
        else:
            mob = p.world.get_or_create_mobile(serial)
            mob.body = body
            mob.x = x
            mob.y = y
            mob.z = z
            mob.direction = Direction.from_byte(direction)
            mob.hue = hue
            mob.flags = MobileFlags(flags & 0xFF)
            mob.is_yellow_health = bool(flags & MobileFlags.YELLOW_BAR)

    handler.register(0x20, handle_mobile_update)

    def handle_delete(packet_id: int, data: bytes) -> None:
        """0x1D Delete — entity removed from the world."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        was_mobile = serial in p.world.mobiles
        p.world.remove(serial)
        # If the removed item was something WE were wearing, drop its layer
        # from self_state.equipment. self_state.equipment is layer->serial and
        # lives on SelfState, so World.remove() (which only touches world.items
        # / world.mobiles) can't clear it. When the agent dies its gear is
        # moved onto the corpse and the server sends a 0x1D Delete for each worn
        # item; likewise dragging a weapon out of hand to swap/drop it deletes
        # the worn serial. Without this, equipment[layer] keeps pointing at a
        # gone serial, so the "hand already occupied" / "already equipped"
        # guards in equip_weapon_from_pack / equip_item see a phantom weapon and
        # the agent never re-equips — it walks into the next fight bare-handed.
        for layer, worn in list(p.self_state.equipment.items()):
            if worn == serial:
                del p.self_state.equipment[layer]
        # SelfState also caches the serial of the last-opened container
        # (0x24 ContainerDisplay) and the last context-menu target. World.remove
        # only touches world.items / world.mobiles, so when that container is
        # destroyed — a looted corpse despawning, a dropped bag emptied, a
        # bank box closed by the server, a vendor pack leaving view — these
        # serials keep pointing at the gone entity. _wait_for_bank_box
        # (skills/trade/banking.py) returns ss.open_container as the "bank is
        # open" signal the instant it is non-zero and != backpack, so a stale
        # corpse/bag serial makes the bank-deposit / withdraw flow target a
        # container that no longer exists and spuriously report the bank ready.
        # Clear them on the matching delete, mirroring the equipment clear above
        # and the vendor_serial-on-close logic in handle_close_vendor.
        if p.self_state.open_container == serial:
            p.self_state.open_container = 0
        if p.self_state.context_menu_serial == serial:
            p.self_state.context_menu_serial = 0
            p.self_state.context_menu = []
        if was_mobile:
            p.emit(GameEventType.MOBILE_REMOVED, {"serial": serial})
        else:
            p.emit(GameEventType.ITEM_REMOVED, {"serial": serial})
        logger.debug("entity_deleted", serial=f"0x{serial:08X}")

    handler.register(0x1D, handle_delete)

    def handle_world_item(packet_id: int, data: bytes) -> None:
        """0x1A WorldItem — item on the ground (legacy)."""
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()

        # The stack-amount flag lives in the serial's high bit, but the count
        # itself is NOT on the wire here — it is read LATER, only after the
        # graphic (and its optional graphic_inc byte). The old code read the
        # u16 amount immediately, before the graphic block, so any ground item
        # carrying BOTH a stack flag and an extended (0x8000) graphic — e.g. a
        # stacked resource/reagent in the high-graphic art range — misaligned
        # every following field (graphic_inc, x, y, z, hue). Match ClassicUO
        # PacketHandlers.UpdateItem ordering exactly. (Ref: ClassicUO 0x1A.)
        has_amount = bool(serial & 0x80000000)
        if serial & 0x80000000:
            serial &= 0x7FFFFFFF

        graphic = r.read_u16()
        if graphic & 0x8000:
            graphic &= 0x7FFF
            graphic += r.read_u8()  # graphic_inc

        # ServUO WorldItem (Packets.cs) writes the art id as `itemID & 0x3FFF`
        # for a normal item and `itemID | 0x4000` for a BaseMulti — bit 0x4000
        # is a pure "this is a multi" flag, NOT part of the graphic number.
        # ClassicUO's UpdateItem treats `graphic >= 0x4000` as a multi and uses
        # the low bits as the real art id. The old code stored the raw u16 with
        # the multi bit still set, so every ground multi (house signs, addons,
        # deco/house components) landed in world.items with a graphic offset by
        # 0x4000 — corrupting any graphic-based item identification and
        # nearest-item lookups. Strip it so item.graphic is the true art id.
        is_multi = bool(graphic & 0x4000)
        graphic &= 0x3FFF

        amount = r.read_u16() if has_amount else 0

        x = r.read_u16()
        y = r.read_u16()

        if x & 0x8000:
            x &= 0x7FFF
            r.read_u8()  # direction

        z = r.read_i8()

        hue = 0
        if y & 0x8000:
            y &= 0x7FFF
            hue = r.read_u16()

        # flags
        if y & 0x4000:
            y &= 0x3FFF
            r.read_u8()  # flags

        item = p.world.get_or_create_item(serial)
        item.graphic = graphic
        item.x = x
        item.y = y
        item.z = z
        item.hue = hue
        item.amount = amount if amount else 1
        item.container = 0
        item.is_multi = is_multi

        p.emit(GameEventType.ITEM_APPEARED, {"serial": serial, "x": x, "y": y})

    handler.register(0x1A, handle_world_item)

    def handle_update_item_sa(packet_id: int, data: bytes) -> None:
        """0xF3 UpdateItemSA — modern item update."""
        r = PacketReader(data[1:])
        r.skip(2)  # unknown
        # ServUO WorldItemHS (Packets.cs) tags the record with a data_type:
        #   0x00 = ordinary item, 0x02 = BaseMulti, 0x03 = IDamageable.
        # ClassicUO's UpdateItemSA forwards `type` to UpdateGameObject, which
        # routes 0x02 to multi handling instead of the item list. We were
        # discarding it, so every ground multi (house addons, signs, deco/house
        # components) landed in world.items as a normal item — corrupting
        # graphic-based identification and nearest-item lookups, exactly the
        # regression the 0x1A handler already guards against. Surface it.
        data_type = r.read_u8()
        serial = r.read_u32()
        graphic = r.read_u16()
        graphic_inc = r.read_u8()
        amount = r.read_u16()
        r.skip(2)  # amount again
        x = r.read_u16()
        y = r.read_u16()
        z = r.read_i8()
        r.skip(1)  # light / direction
        hue = r.read_u16()
        r.skip(1)  # flags

        item = p.world.get_or_create_item(serial)
        item.graphic = graphic + graphic_inc
        item.x = x
        item.y = y
        item.z = z
        item.hue = hue
        item.amount = amount if amount else 1
        item.container = 0
        item.is_multi = data_type == 0x02

        p.emit(GameEventType.ITEM_APPEARED, {"serial": serial, "x": x, "y": y})

    handler.register(0xF3, handle_update_item_sa)

    # ------------------------------------------------------------------
    # Self packets
    # ------------------------------------------------------------------

    def handle_character_status(packet_id: int, data: bytes) -> None:
        """0x11 CharacterStatus — full stat update."""
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        name = r.read_ascii(30)
        hits = r.read_u16()
        hits_max = r.read_u16()
        r.skip(1)  # name_change_flag
        flag = r.read_u8()

        if serial == p.self_state.serial:
            p.self_state.name = name
            p.self_state.hits = hits
            p.self_state.hits_max = hits_max

            if flag >= 1:
                # Gender bool (ServUO m.Female / ClassicUO mobile.IsFemale).
                # Written immediately after the type byte for every type >= 1.
                p.self_state.is_female = r.read_u8() != 0
                p.self_state.strength = r.read_u16()
                p.self_state.dexterity = r.read_u16()
                p.self_state.intelligence = r.read_u16()
                p.self_state.stam = r.read_u16()
                p.self_state.stam_max = r.read_u16()
                p.self_state.mana = r.read_u16()
                p.self_state.mana_max = r.read_u16()
                p.self_state.gold = r.read_u32()
                # Physical resist / armor rating: ServUO writes it as a signed
                # (short) and ClassicUO decodes it signed — a negative resist
                # must not wrap to ~65500. (Ref: ServUO MobileStatus 0x11.)
                p.self_state.armor = r.read_i16()
                p.self_state.weight = r.read_u16()

            if flag >= 5 and r.remaining >= 2:
                p.self_state.weight_max = r.read_u16()
                if r.remaining >= 1:
                    # Race id on the wire is RaceID + 1; the server emits 0 for a
                    # non-ML-enabled account, which ClassicUO coerces to Human(1).
                    race = r.read_u8()
                    p.self_state.race = race if race != 0 else 1
            else:
                # Calculate weight_max from STR (UOR formula)
                p.self_state.weight_max = 7 * (p.self_state.strength // 2) + 40

            # stat_cap + followers form one contiguous block that ServUO
            # writes for every self-status (type >= 1), regardless of
            # expansion. They must be read together or the resist block
            # below misaligns. (Ref: ServUO MobileStatus, ClassicUO 0x11.)
            if r.remaining >= 4:
                # StatCap is written as a signed (short) on the wire (ServUO
                # Packets.cs `(short)beheld.StatCap`) and ClassicUO reads it
                # signed (`StatsCap = (short)ReadUInt16BE`). A stat-loss curse
                # can push it below zero; reading unsigned wrapped it to ~65500.
                p.self_state.stat_cap = r.read_i16()
                p.self_state.followers = r.read_u8()
                p.self_state.followers_max = r.read_u8()

            # AOS+ block (type >= 4): four resists, luck, and the damage
            # range are a single contiguous run on the wire — luck/damage
            # are NOT a separate type-6 field. The old code gated them on
            # flag >= 6, so on a stock AOS/ML shard (type 4 or 5) luck and
            # damage_min/max were never decoded. A trailing tithing u32
            # follows and must be consumed so any type-6 tail stays aligned.
            if flag >= 4 and r.remaining >= 8:
                # The four AOS resists are signed (short) on the wire (ServUO
                # casts the int resist, which a negative ResistanceMod can push
                # below zero, to short; ClassicUO reads them signed). Reading
                # them unsigned turned e.g. -5 into 65531.
                p.self_state.resist_fire = r.read_i16()
                p.self_state.resist_cold = r.read_i16()
                p.self_state.resist_poison = r.read_i16()
                p.self_state.resist_energy = r.read_i16()
                if r.remaining >= 6:
                    # Luck is unsigned on the wire; damage min/max are signed
                    # (short) — ServUO writes `(short)min`/`(short)max` and
                    # ClassicUO reads `DamageMin/Max = (short)ReadUInt16BE`.
                    # A negative weapon damage modifier must stay negative
                    # rather than wrap to ~65500.
                    p.self_state.luck = r.read_u16()
                    p.self_state.damage_min = r.read_i16()
                    p.self_state.damage_max = r.read_i16()
                if r.remaining >= 4:
                    r.skip(4)  # tithing points (u32)

            p.emit(GameEventType.STATS_CHANGED, {"serial": serial})
            logger.debug(
                "self_stats",
                hp=f"{hits}/{hits_max}",
                str=p.self_state.strength,
                dex=p.self_state.dexterity,
                int=p.self_state.intelligence,
                wt=f"{p.self_state.weight}/{p.self_state.weight_max}",
                flag=flag,
            )
        else:
            # ClassicUO's CharacterStatus (PacketHandlers.cs:483) reads the
            # serial, fetches `world.Get(serial)`, and returns when it is null —
            # a status reply NEVER creates an entity, because 0x11 carries NO
            # position (the spatial packets 0x78/0x77/0x20 are the only
            # mobile-creating packets). ServUO emits 0x11 in response to a
            # status request (single-click / health-bar query) and its
            # AttributeNormalizer pins a non-self bar to hits_max=25 with
            # hits=(cur*25//max), so a reply for a foe at <2% HP — or one killed
            # between our 0x78 and this 0x11 — arrives as hits=0, hits_max=25
            # for a serial we no longer have in view. get_or_create_mobile then
            # spawned a brand-new MobileInfo at (0,0) whose `is_dead`
            # (hits_max>0 and hits<=0) is immediately True: the same positionless
            # phantom corpse the 0xA1 and 0x2D handlers already guard against.
            # Only update a mobile that already exists. touch_existing_mobile
            # also re-stamps last_seen so a status reply keeps the foe fresh
            # against prune_stale_mobiles.
            mob = p.world.touch_existing_mobile(serial)
            if mob is None:
                return
            mob.name = name
            # Persist the status-reply name to the durable OPL cache, exactly
            # like the 0x1C single-click LABEL (handle_ascii_talk) and 0xD6
            # MegaCliloc paths already do. world.opl_names survives a 0x1D
            # Delete (see WorldState.get_or_create_mobile): when this mobile
            # leaves view and re-enters, MobileIncoming recreates a BLANK
            # MobileInfo that is re-seeded ONLY from opl_names. 0x11 is the
            # reply to a single-click / health-bar status request — a primary
            # foe/NPC name source — yet it alone wrote the name only onto the
            # live mob and never to the cache, so a name learned this way was
            # silently lost on re-entry (the synchronous name-keyed gates —
            # _is_vendor / _is_refused / huntable-name scans — then missed it
            # until a fresh OPL round-trip). Mirror the other name sources so a
            # status-learned name is just as durable. (Skip an empty field.)
            if name:
                p.world.remember_opl_name(serial, name)
            mob.hits = hits
            mob.hits_max = hits_max

    handler.register(0x11, handle_character_status)

    def handle_hp_update(packet_id: int, data: bytes) -> None:
        """0xA1 UpdateCurrentHealth."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        hits_max = r.read_u16()
        hits = r.read_u16()
        if serial == p.self_state.serial:
            p.self_state.hits = hits
            p.self_state.hits_max = hits_max
            p.emit(GameEventType.HP_CHANGED, {"hits": hits, "hits_max": hits_max})
        else:
            # ClassicUO's UpdateHitpoints (PacketHandlers.cs:3402) bails when
            # `world.Get(serial) == null` — a bare health update NEVER creates
            # an entity, because 0xA1 carries no position (the spatial packets
            # 0x78/0x77/0x20 are the only mobile-creating packets). ServUO sends
            # 0xA1 (MobileHitsN) for foes continuously during combat, and its
            # AttributeNormalizer pins a non-self bar to hits_max=25 with
            # hits=(cur*25//max), so a foe at <2% HP — or one killed between our
            # 0x78 and its 0xA1 — arrives here as hits_max=25, hits=0 for a
            # serial we have not yet (or no longer) seen in view. get_or_create
            # then spawned a brand-new MobileInfo at (0,0) whose `is_dead`
            # (hits_max>0 and hits<=0) is immediately True: a positionless
            # phantom corpse that leaks into world.mobiles and pollutes the
            # swarm tally / _find_target / heal-target scans until pruned. Match
            # ClassicUO and the 0xAF DisplayDeath invariant: only update a
            # mobile that already exists. touch_existing_mobile also re-stamps
            # last_seen — during a stationary melee fight the streaming 0xA1
            # is the only signal keeping the foe alive against the stale-mobile
            # pruner, so dropping the stamp would let it be reaped mid-combat.
            mob = p.world.touch_existing_mobile(serial)
            if mob is None:
                return
            mob.hits = hits
            mob.hits_max = hits_max

    handler.register(0xA1, handle_hp_update)

    def handle_mana_update(packet_id: int, data: bytes) -> None:
        """0xA2 UpdateCurrentMana."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        mana_max = r.read_u16()
        mana = r.read_u16()
        if serial == p.self_state.serial:
            p.self_state.mana = mana
            p.self_state.mana_max = mana_max
            p.emit(GameEventType.MANA_CHANGED, {"mana": mana, "mana_max": mana_max})

    handler.register(0xA2, handle_mana_update)

    def handle_stam_update(packet_id: int, data: bytes) -> None:
        """0xA3 UpdateCurrentStamina."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        stam_max = r.read_u16()
        stam = r.read_u16()
        if serial == p.self_state.serial:
            p.self_state.stam = stam
            p.self_state.stam_max = stam_max
            p.emit(GameEventType.STAM_CHANGED, {"stam": stam, "stam_max": stam_max})

    handler.register(0xA3, handle_stam_update)

    def handle_mobile_attributes(packet_id: int, data: bytes) -> None:
        """0x2D MobileAttributes — full vitals refresh for one mobile.

        Fixed 17-byte packet that carries hp, mana, and stamina (max then
        current for each) in a single message. ServUO emits it on resurrect,
        login, and bulk attribute changes (Server/Mobile.cs), so without a
        handler our self_state hits/mana/stam silently go stale until the next
        0xA1/0xA2/0xA3 trickle. Field order matches ClassicUO MobileAttributes
        (PacketHandlers.cs) and ServUO's MobileAttributes packet: serial,
        hits_max, hits, mana_max, mana, stam_max, stam.
        """
        r = PacketReader(data[1:])
        serial = r.read_u32()
        hits_max = r.read_u16()
        hits = r.read_u16()
        mana_max = r.read_u16()
        mana = r.read_u16()
        stam_max = r.read_u16()
        stam = r.read_u16()
        if serial == p.self_state.serial:
            p.self_state.hits = hits
            p.self_state.hits_max = hits_max
            p.self_state.mana = mana
            p.self_state.mana_max = mana_max
            p.self_state.stam = stam
            p.self_state.stam_max = stam_max
            p.emit(GameEventType.HP_CHANGED, {"hits": hits, "hits_max": hits_max})
            p.emit(GameEventType.MANA_CHANGED, {"mana": mana, "mana_max": mana_max})
            p.emit(GameEventType.STAM_CHANGED, {"stam": stam, "stam_max": stam_max})
        else:
            # ClassicUO's MobileAttributes (PacketHandlers.cs:1765) fetches
            # `world.Get(serial)` and bails when it is null — a bare attribute
            # refresh NEVER creates an entity, because 0x2D carries no position
            # (the spatial packets 0x78/0x77/0x20 are the only mobile-creating
            # packets). This is the same divergence the 0xA1 handler already
            # guards: ServUO's AttributeNormalizer pins a non-self bar to
            # hits_max=25 with hits=(cur*25//max), so a foe at <2% HP — or one
            # killed/resurrected between our 0x78 and its 0x2D (ServUO emits 0x2D
            # on resurrect/login/bulk attribute changes for any mobile in view) —
            # arrives here as hits_max=25, hits=0 for a serial we have not yet (or
            # no longer) seen. get_or_create then spawned a brand-new MobileInfo
            # at (0,0) whose `is_dead` (hits_max>0 and hits<=0) is immediately
            # True: a positionless phantom corpse that leaks into world.mobiles
            # and pollutes the swarm tally / _find_target / heal-target scans
            # until pruned. Match ClassicUO and the 0xA1/0xAF invariant: only
            # update a mobile that already exists. (mana/stam are only meaningful
            # for mobiles and we don't track them per-foe.) touch_existing_mobile
            # also re-stamps last_seen so a stationary foe whose only signal is
            # the periodic 0x2D refresh stays fresh against prune_stale_mobiles.
            mob = p.world.touch_existing_mobile(serial)
            if mob is None:
                return
            mob.hits = hits
            mob.hits_max = hits_max

    handler.register(0x2D, handle_mobile_attributes)

    def handle_skill_update(packet_id: int, data: bytes) -> None:
        """0x3A SkillUpdate — skill list or single skill change.

        list_type values (per ClassicUO PacketHandlers.cs):
          0x00 = Full list (no caps), terminated by skill_id=0
          0x01 = Full list variant (no caps)
          0x02 = Full list WITH caps, terminated by skill_id=0
          0x03 = Full list variant WITH caps
          0xDF = Single skill update (with cap)
          0xFF = Single skill update (with cap)
          0xFE = Skill name list (ignored)
        """
        r = PacketReader(data[3:])  # variable: skip id + length
        list_type = r.read_u8()

        if list_type == 0xFE:
            return  # skill name list metadata — not needed

        is_single = list_type in (0xDF, 0xFF)
        # Match ClassicUO UpdateSkills (PacketHandlers.cs):
        #   haveCap = (type != 0 && type <= 0x03) || type == 0xDF
        #   -> caps present for 0x01, 0x02, 0x03 and 0xDF; 0xFF carries NO cap
        has_cap = list_type in (0x01, 0x02, 0x03, 0xDF)
        # ClassicUO decrements the (1-based) skill_id only for `type == 0 || type == 0x02`.
        # Types 0x01/0x03 are already 0-based — decrementing them shifts every
        # skill to the wrong id and silently corrupts skill-progress tracking.
        adjust_id = list_type in (0x00, 0x02)

        while r.remaining >= 2:
            skill_id = r.read_u16()

            # ClassicUO breaks on a zero id only for the 1-based full list (type 0x00).
            # The 0-based variants (0x01/0x03) have a legitimate skill_id 0 (Alchemy)
            # and run until the buffer is exhausted instead.
            if list_type == 0x00 and skill_id == 0:
                break

            if r.remaining < 5:
                break

            value = r.read_u16()
            base = r.read_u16()
            lock = r.read_u8()
            cap = 1000  # default 100.0
            if has_cap and r.remaining >= 2:
                cap = r.read_u16()

            # Adjust skill ID for full list types (server sends 1-based)
            if adjust_id:
                skill_id -= 1

            if skill_id < 0:
                continue

            skill = p.self_state.skills.get(skill_id)
            if skill is None:
                skill = SkillInfo(id=skill_id)
                p.self_state.skills[skill_id] = skill

            old_value = skill.value
            old_base = skill.base
            skill.value = value / 10.0
            skill.base = base / 10.0
            skill.cap = cap / 10.0
            if 0 <= lock <= 2:
                skill.lock = Lock(lock)

            p.emit(GameEventType.SKILL_CHANGED, {"skill_id": skill_id, "value": skill.value})

            # Log skill gains/losses to journal and activity feed.
            # Skill *progression* registers on `base` (the trainable value);
            # `value` = base + transient item/buff bonuses. A gear or buff swap
            # sends a single 0x3A with an unchanged base but a shifted value, so
            # keying the gain message/event off `value` produced phantom
            # "skill gain/loss" feed entries and SKILL_CHANGED diffs. Use base.
            diff = skill.base - old_base
            if abs(diff) >= 0.1 and is_single:
                _skill_names = {
                    0: "Alchemy", 1: "Anatomy", 4: "Arms Lore", 5: "Parrying",
                    7: "Blacksmith", 8: "Bowcraft", 9: "Peacemaking",
                    11: "Carpentry", 13: "Cooking", 17: "Healing", 18: "Fishing",
                    25: "Magery", 26: "Resist Spells", 27: "Tactics",
                    29: "Musicianship", 31: "Archery", 34: "Tailoring",
                    37: "Tinkering", 40: "Swordsmanship", 41: "Mace Fighting",
                    42: "Fencing", 43: "Wrestling", 44: "Lumberjacking",
                    45: "Mining", 46: "Meditation",
                }
                sname = _skill_names.get(skill_id, f"Skill {skill_id}")
                arrow = "\u2191" if diff > 0 else "\u2193"
                msg = f"{arrow} {sname} {old_base:.1f} \u2192 {skill.base:.1f}"
                p.social.add_speech(0xFFFFFFFF, "System", msg, 0)
                logger.info(
                    "skill_gain", skill=sname,
                    old=old_base, new=skill.base, diff=f"{diff:+.1f}",
                )
                p.emit(GameEventType.SKILL_CHANGED, {
                    "skill_id": skill_id, "value": skill.value,
                    "name": sname, "diff": diff,
                })

            if is_single:
                break  # single skill update — only one entry

    handler.register(0x3A, handle_skill_update)

    def handle_equipment(packet_id: int, data: bytes) -> None:
        """0x2E Equipped item (worn by a mobile)."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        # ClassicUO EquipItem: graphic = ReadUInt16BE() + ReadInt8().
        # The byte after the base graphic is a SIGNED graphic-id increment,
        # not a padding/unknown byte. Discarding it (the old r.skip(1)) left
        # item.graphic wrong for every worn item shipped with a nonzero
        # increment (e.g. dyed/variant art). Add it and wrap to u16 to mirror
        # the C# (ushort) cast.
        graphic = (r.read_u16() + r.read_i8()) & 0xFFFF
        layer = r.read_u8()
        parent_serial = r.read_u32()
        hue = r.read_u16()

        item = p.world.get_or_create_item(serial)
        item.graphic = graphic
        item.hue = hue
        item.layer = layer
        item.container = parent_serial

        if parent_serial == p.self_state.serial:
            p.self_state.equipment[layer] = serial
        else:
            # 0x2E re-parents an existing item. If a serial WE were wearing is
            # now equipped on ANOTHER mobile (a thief re-equips a stolen weapon
            # on themselves, gear handed to / worn by a pet or another player),
            # the server reports the move with THIS 0x2E and parent != self,
            # NOT a 0x1D Delete (the item still exists). self_state.equipment is
            # layer->serial on SelfState, so get_or_create_item / world.remove
            # never touch it. Without a symmetric clear, equipment[layer] keeps
            # pointing at the serial that left our paperdoll, so the combat
            # re-arm guards (ss.equipment.get(1)/.get(2)) and equip_weapon_from_pack
            # see a phantom worn weapon and never re-arm — exactly the stranded
            # reference the 0x1D Delete and 0x25 AddItemToContainer handlers
            # already guard. ClassicUO's EquipItem moves the item off the old
            # owner's layer slot; mirror that for our own loadout.
            for worn_layer, worn in list(p.self_state.equipment.items()):
                if worn == serial:
                    del p.self_state.equipment[worn_layer]

    handler.register(0x2E, handle_equipment)

    def handle_container_content(packet_id: int, data: bytes) -> None:
        """0x3C ContainerContent — items inside a container.

        ServUO sends 0x3C as a complete refresh of a container's contents.
        We must drop stale items that previously lived in this container
        but aren't in the new payload — otherwise the vendor buy-list
        correlation in 0x74 (which collects items by container_serial and
        sorts by (x,y)) gets polluted by old serials, causing the picked
        item's name/price to mismatch its graphic and the server to reply
        "Thou hast bought nothing!"
        """
        r = PacketReader(data[3:])  # variable: skip id + length
        count = r.read_u16()
        new_items: list[tuple[int, int, int, int, int, int, int]] = []
        for _ in range(count):
            if r.remaining < 20:
                break
            serial = r.read_u32()
            graphic = r.read_u16()
            # ClassicUO: graphic = ReadUInt16BE() + ReadUInt8() — the increment
            # byte is ADDED to the base graphic, not discarded (animated/variant ids).
            graphic = (graphic + r.read_u8()) & 0xFFFF
            amount = r.read_u16()
            x = r.read_u16()
            y = r.read_u16()
            r.skip(1)  # grid_index
            container = r.read_u32()
            hue = r.read_u16()
            new_items.append((serial, graphic, amount, x, y, container, hue))

        containers_in_packet: dict[int, set[int]] = {}
        for serial, _g, _a, _x, _y, container, _h in new_items:
            containers_in_packet.setdefault(container, set()).add(serial)

        for container_serial, fresh_serials in containers_in_packet.items():
            stale = [
                it.serial for it in p.world.items.values()
                if it.container == container_serial
                and it.serial not in fresh_serials
            ]
            for s in stale:
                p.world.remove(s)

        for serial, graphic, amount, x, y, container, hue in new_items:
            item = p.world.get_or_create_item(serial)
            item.graphic = graphic
            item.hue = hue
            item.amount = amount if amount else 1
            item.container = container
            item.x = x
            item.y = y

            # An item that this bulk refresh re-parents INTO a container is no
            # longer worn. When the agent disarms / swaps gear, the server may
            # report the now-in-pack weapon via a 0x3C container refresh (e.g.
            # the backpack is re-opened after a swap) rather than a single 0x25
            # AddItemToContainer or a 0x1D Delete. The single-add (0x25), the
            # delete (0x1D), and the re-equip-elsewhere (0x2E) handlers already
            # drop a stranded self_state.equipment slot; this bulk-refresh
            # sibling silently did not, so equipment[layer] kept pointing at the
            # serial that just moved into the bag. The combat re-arm guards
            # (ss.equipment.get(1)/.get(2)) and equip_weapon_from_pack then saw a
            # phantom worn weapon and never re-armed. Mirror the siblings.
            for worn_layer, worn in list(p.self_state.equipment.items()):
                if worn == serial:
                    del p.self_state.equipment[worn_layer]

    handler.register(0x3C, handle_container_content)

    def handle_add_item_to_container(packet_id: int, data: bytes) -> None:
        """0x25 AddItemToContainer — single item added to container."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        graphic = r.read_u16()
        # ClassicUO: graphic = ReadUInt16BE() + ReadUInt8() — the increment
        # byte is ADDED to the base graphic, not discarded.
        graphic = (graphic + r.read_u8()) & 0xFFFF
        amount = r.read_u16()
        x = r.read_u16()
        y = r.read_u16()
        r.skip(1)  # grid_index
        container = r.read_u32()
        hue = r.read_u16()

        item = p.world.get_or_create_item(serial)
        item.graphic = graphic
        item.hue = hue
        item.amount = amount if amount else 1
        item.container = container
        item.x = x
        item.y = y

        # An item moving INTO a container is no longer worn. When the agent
        # disarms / swaps a weapon by dragging it from a hand layer into the
        # backpack, the server does NOT 0x1D-Delete the item (it still exists)
        # — it sends THIS 0x25 with the item now parented to the bag. ClassicUO
        # mirrors this in UpdateContainedItem via World.RemoveItemFromContainer,
        # which drops a player-worn item off the paperdoll and clears its Layer.
        # Without the symmetric clear here, self_state.equipment[layer] keeps
        # pointing at the now-in-pack serial, so the "hand already occupied /
        # already equipped" guards in combat_loop (ss.equipment.get(1/2)) and
        # equip_weapon_from_pack see a phantom worn weapon and never re-arm —
        # the agent fights bare-handed after the first disarm/swap. Drop any
        # equipment slot whose serial just entered a container.
        for worn_layer, worn in list(p.self_state.equipment.items()):
            if worn == serial:
                del p.self_state.equipment[worn_layer]

    handler.register(0x25, handle_add_item_to_container)

    def handle_container_display(packet_id: int, data: bytes) -> None:
        """0x24 ContainerDisplay — server opens a container on the client.

        Records the container serial so skills (like banking) know the
        container is open. For bank boxes (layer 0x1D), this confirms
        the bank is ready.
        """
        if len(data) < 7:
            return
        r = PacketReader(data[1:])
        serial = r.read_u32()
        gump_graphic = r.read_u16()
        p.self_state.open_container = serial
        logger.debug(
            "container_display",
            serial=f"0x{serial:08X}",
            gump=f"0x{gump_graphic:04X}",
        )

    handler.register(0x24, handle_container_display)

    # ------------------------------------------------------------------
    # Social packets
    # ------------------------------------------------------------------

    def handle_ascii_talk(packet_id: int, data: bytes) -> None:
        """0x1C ASCII Talk."""
        if len(data) <= 8:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        r.skip(2)  # graphic
        msg_type = r.read_u8()
        hue = r.read_u16()
        r.skip(2)  # font
        name = r.read_ascii(30)
        text = r.read_ascii_remaining()

        # msg_type 6 = Label (single-click response with name + title)
        if msg_type == 6 and serial and text:
            mob = p.world.mobiles.get(serial)
            if mob is not None:
                mob.name = text  # e.g. "Hastin the baker"
            item = p.world.items.get(serial)
            if item is not None:
                item.name = text
            # Persist a labeled MOBILE name to the durable OPL cache.
            # world.opl_names survives a 0x1D Delete (see
            # WorldState.get_or_create_mobile): when a labeled NPC leaves view
            # and re-enters, MobileIncoming recreates a blank MobileInfo that is
            # re-seeded from this cache. The 0xD6 MegaCliloc handler already
            # caches its resolved name here, but the single-click LABEL — a
            # primary name source for any NPC the agent clicks — did not, so a
            # labeled vendor's name was silently lost on re-entry and the
            # synchronous name-keyed gates (_is_vendor / _is_refused) missed
            # until a fresh OPL round-trip. Only mobiles are re-seeded from this
            # cache, so scope the write to a known mobile serial. Mirror the
            # 0xD6 cache write so a label-learned name is just as durable.
            if mob is not None:
                p.world.remember_opl_name(serial, text)

        p.social.add_speech(serial, name, text, msg_type, hue)
        p.emit(
            GameEventType.SPEECH_HEARD,
            {"serial": serial, "name": name, "text": text, "type": msg_type},
        )
        if msg_type != 6:  # don't log labels as speech
            logger.info("speech", name=name, text=text, type=msg_type)

    handler.register(0x1C, handle_ascii_talk)

    def handle_unicode_talk(packet_id: int, data: bytes) -> None:
        """0xAE UnicodeTalk."""
        if len(data) <= 48:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        r.skip(2)  # graphic
        msg_type = r.read_u8()
        hue = r.read_u16()
        r.skip(2)  # font
        lang = r.read_ascii(4)
        name = r.read_ascii(30)
        text = r.read_unicode_remaining()

        p.social.add_speech(serial, name, text, msg_type, hue)
        p.emit(
            GameEventType.SPEECH_HEARD,
            {"serial": serial, "name": name, "text": text, "lang": lang, "type": msg_type},
        )
        logger.info("speech", name=name, text=text, lang=lang, type=msg_type)

    handler.register(0xAE, handle_unicode_talk)

    def handle_cliloc_message(packet_id: int, data: bytes) -> None:
        """0xC1 ClilocMessage — localized system message with optional args.

        Format: [0xC1][len:u16][serial:u32][graphic:u16][msg_type:u8]
                [hue:u16][font:u16][cliloc_num:u32][name:ascii 30]
                [args:utf16-le null-terminated]
        """
        if len(data) < 48:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        r.skip(2)  # graphic
        msg_type = r.read_u8()
        hue = r.read_u16()
        r.skip(2)  # font
        cliloc_num = r.read_u32()
        name_bytes = data[3 + r.position: 3 + r.position + 30]
        r.skip(30)
        name = name_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")

        # Args are UTF-16 LE, null-terminated
        args = _decode_cliloc_args(data[3 + r.position:], big_endian=False)

        text = _resolve_cliloc_text(cliloc_num, args)

        if not name:
            name = "System"

        p.social.add_speech(serial, name, text, msg_type, hue)
        # The SPEECH_HEARD event MUST carry the on-wire ``type``. Brain._poll_events
        # and respond_to_speech both classify chatter via ``data.get("type", 0)``,
        # and a MISSING key defaults to 0 == MessageType.REGULAR — so a SYSTEM/LABEL
        # cliloc line (a server notice, a single-click name label) slipped past the
        # non-conversational filter, queued a bogus pending reply AND stamped
        # last_player_speech, freezing fresh strategising for CONVERSATION_TIMEOUT.
        # The 0x1C/0xAE talk handlers already forward ``type``; the cliloc emits must
        # too (the journal write above already records the correct msg_type).
        p.emit(GameEventType.SPEECH_HEARD, {
            "serial": serial, "name": name, "text": text, "type": msg_type,
        })
        logger.info("speech_cliloc", name=name, text=text, cliloc=cliloc_num)

    handler.register(0xC1, handle_cliloc_message)

    def handle_cliloc_affix(packet_id: int, data: bytes) -> None:
        """0xCC SendLocalizedMessageAffix — localized message with an affix.

        Differs from 0xC1: after the cliloc id there is a 1-byte AffixType
        flag, then the 30-byte name, then a NUL-terminated ASCII affix string,
        and finally the args are UTF-16 *Big-Endian* (not LE).

        Format: [0xCC][len:u16][serial:u32][graphic:u16][msg_type:u8]
                [hue:u16][font:u16][cliloc_num:u32][flags:u8][name:ascii 30]
                [affix:ascii NUL-terminated][args:utf16-be]
        """
        if len(data) < 49:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        r.skip(2)  # graphic
        msg_type = r.read_u8()
        hue = r.read_u16()
        r.skip(2)  # font
        cliloc_num = r.read_u32()
        flags = r.read_u8()
        name_bytes = data[3 + r.position: 3 + r.position + 30]
        r.skip(30)
        name = name_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")

        # Affix is a NUL-terminated ASCII string of unknown length.
        affix_start = 3 + r.position
        nul = data.find(b"\x00", affix_start)
        if nul == -1:
            nul = len(data)
        affix = data[affix_start:nul].decode("ascii", errors="replace")
        r.skip((nul - affix_start) + 1)  # consume affix + its NUL terminator

        # Args are UTF-16 BE on 0xCC (ClassicUO ReadUnicodeBE).
        args = _decode_cliloc_args(data[3 + r.position:], big_endian=True)

        text = _resolve_cliloc_text(cliloc_num, args)

        # Prepend or append the affix per the AffixType flag.
        if affix:
            if flags & _AFFIX_PREPEND:
                text = f"{affix}{text}"
            else:
                text = f"{text}{affix}"
        # System flag forces a system message. ClassicUO sets
        # `type = MessageType.System` here (PacketHandlers.cs DisplayClilocString,
        # `if ((flags & AffixType.System) != 0) type = MessageType.System`), and
        # MessageType.System is 1 — NOT 6 (which is Label, the single-click name
        # response). The old `msg_type = 6` mis-tagged every System-affixed
        # localized message (ServUO delivers many gameplay notices via
        # SendLocalizedMessageAffix) as a LABEL in the speech journal / SPEECH_HEARD
        # event, conflating server system text with NPC name labels.
        if flags & _AFFIX_SYSTEM:
            msg_type = MessageType.SYSTEM

        if not name:
            name = "System"

        p.social.add_speech(serial, name, text, msg_type, hue)
        # Forward the (possibly System-forced) ``type`` so the poll/reply filters
        # see it — see handle_cliloc_message. Without it the carefully-resolved
        # ``msg_type = MessageType.SYSTEM`` above is dropped at the event layer.
        p.emit(GameEventType.SPEECH_HEARD, {
            "serial": serial, "name": name, "text": text, "type": int(msg_type),
        })
        logger.info("speech_cliloc_affix", name=name, text=text,
                    cliloc=cliloc_num, affix=affix)

    handler.register(0xCC, handle_cliloc_affix)

    # ------------------------------------------------------------------
    # Movement packets
    # ------------------------------------------------------------------

    _dir_names = {0: "N", 1: "NE", 2: "E", 3: "SE", 4: "S", 5: "SW", 6: "W", 7: "NW"}

    def handle_confirm_walk(packet_id: int, data: bytes) -> None:
        """0x22 ConfirmWalk."""
        r = PacketReader(data[1:])
        seq = r.read_u8()
        # The 0x22 ConfirmWalk is [0x22][seq][notoriety] (3 bytes). The second
        # byte is OUR OWN notoriety — the authoritative source for the player's
        # criminal/gray/murderer status, refreshed by the server on every
        # accepted step (e.g. the moment a freshly-committed crime flips us
        # gray, or a count drops us to murderer/red). ClassicUO decodes it in
        # ConfirmWalk (PacketHandlers.cs) as ``noto = ReadUInt8() & ~0x40`` and
        # assigns ``world.Player.NotorietyFlag``, coercing an out-of-range
        # 0 / >=8 byte back to 0x01 (Innocent). We previously read only ``seq``
        # and left self_state.notoriety pinned at its INNOCENT default forever,
        # so the agent could never tell it had gone criminal — the same
        # notoriety gate the combat/social code already reads off *foes* was
        # blind for the player. Decode and store it to match ClassicUO.
        if r.remaining >= 1:
            noto = r.read_u8() & ~0x40
            if noto == 0 or noto >= 8:
                noto = 1
            p.self_state.notoriety = NotorietyFlag(noto)
        pending = walker._pending_step_tile
        walker.confirm_walk(seq)
        ss = p.self_state
        if pending:
            logger.debug(
                "walk_confirmed", seq=seq,
                pos=f"({ss.x},{ss.y},{ss.z})",
                dir=_dir_names.get(ss.direction, "?"),
            )
        else:
            logger.debug("walk_turn_confirmed", seq=seq, dir=_dir_names.get(ss.direction, "?"))

    handler.register(0x22, handle_confirm_walk)

    def handle_deny_walk(packet_id: int, data: bytes) -> None:
        """0x21 DenyWalk."""
        r = PacketReader(data[1:])
        seq = r.read_u8()
        x = r.read_u16()
        y = r.read_u16()
        direction = r.read_u8() & 0x07
        z = r.read_i8()
        denied_tile = walker._pending_step_tile
        walker.deny_walk(seq, x, y, z, direction)
        logger.info(
            "walk_denied", seq=seq,
            pos=f"({x},{y},{z})",
            dir=_dir_names.get(direction, "?"),
            blocked=f"({denied_tile[0]},{denied_tile[1]})" if denied_tile else "turn",
            denials=walker.consecutive_denials,
        )

    handler.register(0x21, handle_deny_walk)

    # ------------------------------------------------------------------
    # System packets
    # ------------------------------------------------------------------

    def handle_general_info(packet_id: int, data: bytes) -> None:
        """0xBF GeneralInfo — subcmd dispatch for fastwalk keys etc."""
        if len(data) < 5:
            return
        subcmd = struct.unpack(">H", data[3:5])[0]

        if subcmd == 0x01 and len(data) >= 29:
            # Set fastwalk keys (6 keys)
            keys = []
            for i in range(6):
                off = 5 + i * 4
                keys.append(struct.unpack(">I", data[off : off + 4])[0])
            walker.set_fast_walk_keys(keys)
            logger.info("fastwalk_keys_set", keys=[f"0x{k:08X}" for k in keys[:5]])

        elif subcmd == 0x02 and len(data) >= 9:
            key = struct.unpack(">I", data[5:9])[0]
            walker.add_fast_walk_key(key)
            logger.debug("fastwalk_key_added", key=f"0x{key:08X}")

    handler.register(0xBF, handle_general_info)

    # ------------------------------------------------------------------
    # OPL (Object Property List) packets
    # ------------------------------------------------------------------

    def handle_opl_info(packet_id: int, data: bytes) -> None:
        """0xDC OPLInfo — entity has properties available (9 bytes).

        We just record the revision hash; the actual OPL data
        comes via 0xD6 when we request it.
        """
        if len(data) < 9:
            return
        r = PacketReader(data[1:])
        serial = r.read_u32()
        revision = r.read_u32()
        # Store revision so we know OPL exists for this entity (bounded cache —
        # see WorldState.OPL_CACHE_MAX; raw serials stream unboundedly over a soak)
        p.world.remember_opl_revision(serial, revision)

    handler.register(0xDC, handle_opl_info)

    def handle_mega_cliloc(packet_id: int, data: bytes) -> None:
        """0xD6 MegaCliloc — full OPL property list for an entity."""
        import re

        if len(data) < 15:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        r.skip(2)  # unknown (0x0001)
        serial = r.read_u32()
        r.skip(2)  # unknown
        r.skip(4)  # list_id / hash

        properties: list[str] = []
        while r.remaining >= 4:
            cliloc_num = r.read_u32()
            if cliloc_num == 0:
                break
            if r.remaining < 2:
                break
            text_len = r.read_u16()  # byte length of unicode args
            args = ""
            if text_len > 0 and r.remaining >= text_len:
                raw = data[3 + r.position : 3 + r.position + text_len]
                # ClassicUO's MegaCliloc reads each arg with ReadUnicodeLE(length/2)
                # (PacketHandlers.cs:4947) — a C-string read that floors an odd
                # tail byte and STOPS at the first NUL code unit. The old inline
                # ``raw.decode("utf-16-le", errors="replace")`` did neither: an odd
                # text_len produced a trailing U+FFFD, and an embedded NUL leaked
                # the post-terminator padding/garbage straight into the property.
                # Since MegaCliloc is anima's only mobile-name source, that garbage
                # became mob.name / opl_names[serial] (e.g. "Healer\x00GHOST"),
                # breaking every name-keyed gate. Route through the shared decoder
                # so 0xD6 matches 0xC1/0xCC.
                args = _decode_cliloc_args(raw, big_endian=False)
                r.skip(text_len)
            elif text_len > 0:
                break  # not enough data

            base_text = cliloc_text(cliloc_num)
            if base_text and args:
                # ClassicUO (ClilocLoader.Translate) substitutes each
                # placeholder by the index embedded in it: ~N~ or
                # ~N_label~ is replaced with the (N-1)th tab-separated
                # argument. Drive substitution by that index — not by the
                # positional order of the args — so out-of-order, repeated,
                # and underscore-less (~1~~2~~3~, e.g. cliloc 1041522)
                # placeholders all resolve correctly.
                parts = args.split("\t")

                def _sub(m: "re.Match[str]") -> str:
                    idx = int(m.group(1)) - 1
                    return parts[idx] if 0 <= idx < len(parts) else m.group(0)

                text = re.sub(r"~(\d+)(?:_[^~]*)?~", _sub, base_text)
                properties.append(text)
            elif base_text:
                properties.append(base_text)
            elif args:
                properties.append(args)

        # Apply to mobile or item
        name = properties[0] if properties else ""
        mob = p.world.mobiles.get(serial)
        if mob is not None:
            mob.properties = properties
            # Reflect the LATEST OPL name, not just the first one ever seen.
            # MegaCliloc is anima's only mobile-name source (there is no 0x98
            # NameRequest handler), so the old `not mob.name` pin froze a
            # mobile to its first OPL name forever. That breaks two cases:
            # (1) the name on a mobile genuinely changes (renamed pet/NPC), and
            # (2) the server recycles a freed mobile serial for a DIFFERENT
            # mobile — get_or_create_mobile re-seeds the live mob from the
            # opl_names cache, and the pin then blocked the fresh OPL's correct
            # name from ever landing. The cache below already overwrites
            # unconditionally, so pinning the live entity also left mob.name and
            # opl_names[serial] disagreeing. Overwrite when non-empty to match
            # ClassicUO (entity.Name = latest OPL string) and keep them in sync.
            if name:
                mob.name = name
        item = p.world.items.get(serial)
        if item is not None:
            item.properties = properties
            if name:
                item.name = name
        # Cache so a later 0x1D/0x78 round-trip for the same NPC doesn't
        # blank out the name we just learned.
        if name:
            p.world.remember_opl_name(serial, name)
        if properties:
            p.world.remember_opl_properties(serial, properties)

    handler.register(0xD6, handle_mega_cliloc)

    # ------------------------------------------------------------------
    # Target cursor + combat packets
    # ------------------------------------------------------------------

    def handle_target_cursor(packet_id: int, data: bytes) -> None:
        """0x6C TargetCursor — server asks us to select a target."""
        if len(data) < 19:
            return
        r = PacketReader(data[1:])
        target_type = r.read_u8()  # 0=object, 1=ground
        cursor_id = r.read_u32()
        cursor_flag = r.read_u8()  # 0=neutral, 1=harmful, 2=helpful, 3=cancel

        # A 0x6C with cursor_flag == 3 is the server WITHDRAWING the cursor,
        # not a new request. ClassicUO's TargetManager.SetTargeting gates on
        # ``IsTargeting = cursorType < TargetType.Cancel`` (TargetType.Cancel
        # == 3) and, when a cancel arrives, runs CancelTarget()/Reset() rather
        # than presenting a cursor to answer. We previously stored every 0x6C
        # as ``pending_target`` regardless of flag, so a server cancel:
        #   1. made ``wait_for_target`` return success on a dead cursor, and
        #   2. lingered in ``pending_target`` to falsely satisfy the *next*
        #      ``wait_for_target`` immediately, firing a response against the
        #      already-cancelled cursor_id.
        # Treat it as a clear of any in-flight target instead.
        if cursor_flag == 3:
            p.emit(
                GameEventType.TARGET_REQUESTED,
                {
                    "target_type": target_type,
                    "cursor_id": cursor_id,
                    "cursor_flag": cursor_flag,
                },
            )
            p.self_state.pending_target = None
            logger.debug("target_cursor_cancelled", cursor_id=f"0x{cursor_id:08X}")
            return

        p.emit(
            GameEventType.TARGET_REQUESTED,
            {
                "target_type": target_type,
                "cursor_id": cursor_id,
                "cursor_flag": cursor_flag,
            },
        )
        # Store cursor in blackboard-equivalent so skills can respond
        p.self_state.pending_target = {
            "target_type": target_type,
            "cursor_id": cursor_id,
            "cursor_flag": cursor_flag,
        }
        logger.debug(
            "target_cursor",
            type=target_type,
            cursor_id=f"0x{cursor_id:08X}",
            flag=cursor_flag,
        )

    handler.register(0x6C, handle_target_cursor)

    def handle_damage(packet_id: int, data: bytes) -> None:
        """0x0B Damage — damage dealt to an entity."""
        if len(data) < 7:
            return
        # FIXED 7-byte packet: [0x0B][victim serial u32][amount u16] — there
        # is no length field. Reading data[3:] here crashed the session on
        # every combat exchange (4-byte buffer, u32+u16 reads).
        r = PacketReader(data[1:])
        serial = r.read_u32()
        amount = r.read_u16()

        # A zero-amount 0x0B is a fully parried / absorbed / blocked swing:
        # the server still emits the packet, but no hit landed. ClassicUO's
        # Damage handler (PacketHandlers.cs) only registers damage when
        # `damage > 0`. Treating amount==0 as a hit reset
        # last_damage_taken_at on every parry — which keeps a *defensive*
        # melee agent (MeleeAttack.can_execute gates on that timestamp)
        # locked in combat off a swing that never hurt it — and leaked
        # phantom DAMAGE_TAKEN/DAMAGE_DEALT(amount=0) events downstream.
        if amount == 0:
            return

        if serial == p.self_state.serial:
            p.self_state.last_damage_taken_at = time.monotonic()
            p.emit(
                GameEventType.DAMAGE_TAKEN,
                {"amount": amount},
            )
        else:
            p.emit(
                GameEventType.DAMAGE_DEALT,
                {"serial": serial, "amount": amount},
            )

    handler.register(0x0B, handle_damage)

    # ------------------------------------------------------------------
    # Gump packets
    # ------------------------------------------------------------------

    def _log_gump(gump, event_name: str) -> None:
        """Log full gump contents — labels, buttons, positions."""
        labels = []
        for t in gump.texts:
            if 0 <= t.text_id < len(gump.text_lines):
                text = gump.text_lines[t.text_id][:40]
                labels.append(f"({t.x},{t.y})={text}")
        # Full per-button detail: (id, type, x, y). button_type=1 is a
        # reply; 0 is a page switch. Critical for diagnosing gumps whose
        # label/button mapping we're guessing at (e.g. ResurrectGump).
        button_details = [
            (b.button_id, b.button_type, b.x, b.y) for b in gump.buttons
        ]
        logger.info(
            event_name,
            serial=f"0x{gump.serial:08X}",
            gump_id=f"0x{gump.gump_id:08X}",
            buttons=len(gump.buttons),
            labels=labels,
            button_ids=[b.button_id for b in gump.buttons],
            button_details=button_details,
        )

    def handle_open_gump(packet_id: int, data: bytes) -> None:
        """0xB0 OpenGump — server sends a generic gump (uncompressed)."""
        if len(data) < 21:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        gump_id = r.read_u32()
        gx = r.read_u32()
        gy = r.read_u32()
        layout_len = r.read_u16()
        if r.remaining < layout_len:
            logger.warning("gump_truncated_layout", gump_id=f"0x{gump_id:08X}")
            return
        layout_bytes = data[3 + r.position : 3 + r.position + layout_len]
        r.skip(layout_len)
        layout = layout_bytes.decode("ascii", errors="replace")

        # Text lines
        text_lines: list[str] = []
        if r.remaining >= 2:
            line_count = r.read_u16()
            for _ in range(line_count):
                if r.remaining < 2:
                    break
                line_len = r.read_u16()  # char count
                if r.remaining < line_len * 2:
                    break
                line_bytes = data[3 + r.position : 3 + r.position + line_len * 2]
                r.skip(line_len * 2)
                text_lines.append(line_bytes.decode("utf-16-be", errors="replace"))

        gump = parse_layout(layout, text_lines)
        gump.serial = serial
        gump.gump_id = gump_id
        gump.x = gx
        gump.y = gy

        _store_gump(p.self_state, gump_id, gump)
        p.emit(
            GameEventType.GUMP_OPENED,
            {"serial": serial, "gump_id": gump_id, "buttons": len(gump.buttons)},
        )
        _log_gump(gump, "gump_opened")

    handler.register(0xB0, handle_open_gump)

    def handle_compressed_gump(packet_id: int, data: bytes) -> None:
        """0xDD CompressedGump — zlib-compressed variant of 0xB0."""
        if len(data) < 27:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        gump_id = r.read_u32()
        gx = r.read_u32()
        gy = r.read_u32()

        # Layout section
        layout_compressed_len = r.read_u32()  # includes 4-byte decompressed len
        layout_decompressed_len = r.read_u32()

        if layout_compressed_len < 4:
            logger.warning("gump_bad_layout_len", gump_id=f"0x{gump_id:08X}")
            return

        compressed_data_len = layout_compressed_len - 4
        if r.remaining < compressed_data_len:
            logger.warning("gump_truncated_compressed", gump_id=f"0x{gump_id:08X}")
            return

        layout_compressed = data[3 + r.position : 3 + r.position + compressed_data_len]
        r.skip(compressed_data_len)

        try:
            layout_bytes = zlib.decompress(layout_compressed)
        except zlib.error:
            logger.warning("gump_layout_decompress_failed", gump_id=f"0x{gump_id:08X}")
            return
        layout = layout_bytes[:layout_decompressed_len].decode("ascii", errors="replace")

        # Text section
        text_lines: list[str] = []
        if r.remaining >= 4:
            text_line_count = r.read_u32()
            if r.remaining >= 8:
                text_compressed_len = r.read_u32()  # includes 4-byte decompressed len
                r.read_u32()  # text decompressed length (informational)

                if text_compressed_len >= 4:
                    text_cdata_len = text_compressed_len - 4
                    if r.remaining >= text_cdata_len and text_cdata_len > 0:
                        text_compressed = data[3 + r.position : 3 + r.position + text_cdata_len]
                        r.skip(text_cdata_len)

                        try:
                            text_raw = zlib.decompress(text_compressed)
                        except zlib.error:
                            logger.warning(
                                "gump_text_decompress_failed",
                                gump_id=f"0x{gump_id:08X}",
                            )
                            text_raw = b""

                        # Parse text lines: each is u16 BE char_count + utf16-be data
                        pos = 0
                        for _ in range(text_line_count):
                            if pos + 2 > len(text_raw):
                                break
                            char_count = struct.unpack_from(">H", text_raw, pos)[0]
                            pos += 2
                            byte_count = char_count * 2
                            if pos + byte_count > len(text_raw):
                                break
                            line = text_raw[pos : pos + byte_count].decode(
                                "utf-16-be", errors="replace"
                            )
                            text_lines.append(line)
                            pos += byte_count

        gump = parse_layout(layout, text_lines)
        gump.serial = serial
        gump.gump_id = gump_id
        gump.x = gx
        gump.y = gy

        _store_gump(p.self_state, gump_id, gump)
        p.emit(
            GameEventType.GUMP_OPENED,
            {"serial": serial, "gump_id": gump_id, "buttons": len(gump.buttons)},
        )
        _log_gump(gump, "gump_compressed_opened")

    handler.register(0xDD, handle_compressed_gump)

    # Extend the existing 0xBF handler to also handle gump close (sub 0x04)
    _original_general_info = handler._handlers.get(0xBF)

    def handle_general_info_extended(packet_id: int, data: bytes) -> None:
        """0xBF GeneralInfo — extended to handle CloseGump and ContextMenu."""
        if _original_general_info:
            _original_general_info(packet_id, data)

        if len(data) < 5:
            return
        subcmd = struct.unpack(">H", data[3:5])[0]

        if subcmd == 0x04 and len(data) >= 13:
            # CloseGump: subcmd(2) + gump_id(4) + button_id(4)
            gump_id = struct.unpack(">I", data[5:9])[0]
            button_id = struct.unpack(">I", data[9:13])[0]
            removed = p.self_state.gumps.pop(gump_id, None)
            if removed:
                p.emit(
                    GameEventType.GUMP_CLOSED,
                    {"gump_id": gump_id, "button_id": button_id},
                )
                logger.debug(
                    "gump_closed_by_server",
                    gump_id=f"0x{gump_id:08X}",
                    button_id=button_id,
                )

        elif subcmd == 0x14 and len(data) >= 12:
            # DisplayContextMenu: subcmd(2) + mode(2) + serial(4) + count(1)
            # The entry layout depends on `mode` (ClassicUO PopupMenuData.Parse):
            #   mode >= 2 (new cliloc): cliloc:u32, index:u16, flags:u16
            #   mode <  2 (old cliloc): index:u16, cliloc:u16(+3_000_000),
            #                           flags:u16, then optional flag-gated
            #                           words (0x84 -> +2, 0x40 -> +2,
            #                           0x20 -> +2). Decoding an old-format
            #                           packet with the new layout shifts
            #                           every field and corrupts the cliloc.
            from anima.perception.self_state import ContextMenuEntry as CMEntry

            raw = data[5:]
            mode = struct.unpack(">H", raw[0:2])[0]
            is_new_cliloc = mode >= 2
            serial = struct.unpack(">I", raw[2:6])[0]
            count = raw[6]
            off = 7
            entries: list[CMEntry] = []
            for _ in range(count):
                if is_new_cliloc:
                    if off + 8 > len(raw):
                        break
                    cliloc = struct.unpack(">I", raw[off : off + 4])[0]
                    index = struct.unpack(">H", raw[off + 4 : off + 6])[0]
                    flags = struct.unpack(">H", raw[off + 6 : off + 8])[0]
                    off += 8
                else:
                    if off + 6 > len(raw):
                        break
                    index = struct.unpack(">H", raw[off : off + 2])[0]
                    cliloc = struct.unpack(">H", raw[off + 2 : off + 4])[0] + 3_000_000
                    flags = struct.unpack(">H", raw[off + 4 : off + 6])[0]
                    off += 6
                    # Flag-gated trailing words — consume them so the next
                    # entry stays byte-aligned (values themselves unused).
                    if flags & 0x84:
                        off += 2
                    if flags & 0x40:
                        off += 2
                    if flags & 0x20:
                        off += 2
                entries.append(CMEntry(cliloc=cliloc, index=index, flags=flags))

            p.self_state.context_menu_serial = serial
            p.self_state.context_menu = entries
            logger.debug(
                "context_menu_received",
                serial=f"0x{serial:08X}",
                count=len(entries),
                entries=[(e.cliloc, e.index) for e in entries],
            )

    handler.register(0xBF, handle_general_info_extended)

    # ------------------------------------------------------------------
    # Vendor packets
    # ------------------------------------------------------------------

    def handle_vendor_buy_list(packet_id: int, data: bytes) -> None:
        """0x74 VendorBuyList — prices for items in a vendor's buy container.

        Format: [0x74][length:u16][container_serial:u32][item_count:u8]
        Per item: [price:u32][name_length:u8][name:ascii]

        The actual items were already received via 0x3C (ContainerContent).
        This packet assigns prices/names and correlates them with the container.
        """
        if len(data) < 8:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        container_serial = r.read_u32()
        count = r.read_u8()

        # Find the vendor who owns this container
        container_item = p.world.items.get(container_serial)
        vendor_serial = container_item.container if container_item else 0

        # Gather items that are inside this container (from 0x3C).
        # ServUO sends 0x3C in REVERSE order (list.Count-1 .. 0) while
        # 0x74 is sent FORWARD (0 .. list.Count-1). Each 0x3C item has
        # x=(original_index + 1), y=1, so sorting by (x, y) ascending
        # recovers the canonical order that the price list expects.
        # See ServUO Packets.cs:306-310.
        container_items = sorted(
            (it for it in p.world.items.values() if it.container == container_serial),
            key=lambda it: (it.x, it.y),
        )

        buy_items: list[VendorBuyItem] = []
        for i in range(count):
            if r.remaining < 5:
                break
            price = r.read_u32()
            name_len = r.read_u8()
            name = ""
            if name_len > 0 and r.remaining >= name_len:
                name = r.read_ascii(name_len)

            # ServUO's GenericBuyInfo names a stock item
            # ``(1020000 + itemID).ToString()`` whenever it has no custom Name
            # (Scripts/VendorInfo/GenericBuy.cs:65-66) — i.e. for the vast
            # majority of vendor goods (ingots, ore, regs, tools), so the
            # BuyItemState.Description that lands in this 0x74 field very
            # commonly arrives as a bare cliloc id string like "1020386", NOT a
            # readable label. ClassicUO's BuyList handler
            # (PacketHandlers.cs:2758-2765) resolves it: `int.TryParse(name) ->
            # Clilocs.Translate(num)`. Without this we stored the raw digits, so
            # the buy flow's name-based reporting/journal (buy_from_vendor's
            # target_name + buy_list_received feed) showed "1020386" instead of
            # "iron ingot" — the same regression already fixed for the 0x9E
            # sell list (commit 94d8e11). Mirror ClassicUO: when the name is all
            # digits and resolves to a cliloc string, swap in the resolved text;
            # otherwise keep the literal (custom-named items stay as-is).
            if name.isdigit():
                resolved = cliloc_text(int(name))
                if resolved:
                    name = resolved

            # Match with container item by index
            if i < len(container_items):
                ci = container_items[i]
                buy_items.append(
                    VendorBuyItem(
                        serial=ci.serial,
                        graphic=ci.graphic,
                        amount=ci.amount,
                        price=price,
                        name=name or ci.name or f"item_0x{ci.graphic:04X}",
                    )
                )

        if buy_items:
            p.self_state.vendor_serial = vendor_serial
            p.self_state.vendor_buy_list = buy_items
            p.emit(
                GameEventType.VENDOR_BUY_LIST,
                {"vendor_serial": vendor_serial, "count": len(buy_items)},
            )
            logger.info(
                "vendor_buy_list",
                vendor=f"0x{vendor_serial:08X}",
                items=len(buy_items),
            )

    handler.register(0x74, handle_vendor_buy_list)

    def handle_vendor_sell_list(packet_id: int, data: bytes) -> None:
        """0x9E VendorSellList — items the vendor will buy from us.

        Format: [0x9E][length:u16][vendor_serial:u32][item_count:u16]
        Per item: [serial:u32][graphic:u16][hue:u16][amount:u16][price:u16]
                  [name_length:u16][name:ascii]
        """
        if len(data) < 9:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        vendor_serial = r.read_u32()
        count = r.read_u16()

        sell_items: list[VendorSellItem] = []
        for _ in range(count):
            if r.remaining < 14:
                break
            serial = r.read_u32()
            graphic = r.read_u16()
            r.skip(2)  # hue
            amount = r.read_u16()
            price = r.read_u16()
            name_len = r.read_u16()
            name = ""
            if name_len > 0 and r.remaining >= name_len:
                name = r.read_ascii(name_len)

            # ServUO's VendorSellList writes the per-item name as
            # SellItemState.Name, which GenericSellInfo.GetNameFor returns as
            # `item.LabelNumber.ToString()` whenever the item has no custom
            # Name (Scripts/VendorInfo/GenericSell.cs:131-137) — i.e. for the
            # vast majority of stock goods (ingots, ore, regs, tools). So the
            # name field very commonly arrives as a bare cliloc id string like
            # "1027154", NOT a readable label. ClassicUO's SellList handler
            # (PacketHandlers.cs:3375-3382) resolves it: `int.TryParse(name) ->
            # Clilocs.GetString(num)`. We previously stored the raw digits, so
            # the sell flow's name-based item identification (e.g.
            # sell_to_vendor keeping the cheapest of a tool group) compared
            # against "1027154" instead of "iron ingot" — never matching by
            # display name. Mirror ClassicUO: when the name is all digits and
            # resolves to a cliloc string, swap in the resolved text; otherwise
            # keep the literal (custom-named items stay as-is).
            if name.isdigit():
                resolved = cliloc_text(int(name))
                if resolved:
                    name = resolved

            sell_items.append(
                VendorSellItem(
                    serial=serial,
                    graphic=graphic,
                    amount=amount,
                    price=price,
                    name=name or f"item_0x{graphic:04X}",
                )
            )

        p.self_state.vendor_serial = vendor_serial
        p.self_state.vendor_sell_list = sell_items
        p.emit(
            GameEventType.VENDOR_SELL_LIST,
            {"vendor_serial": vendor_serial, "count": len(sell_items)},
        )
        logger.info(
            "vendor_sell_list",
            vendor=f"0x{vendor_serial:08X}",
            items=len(sell_items),
        )

    handler.register(0x9E, handle_vendor_sell_list)

    # ------------------------------------------------------------------
    # Cosmetic / informational packets — log and ignore
    # ------------------------------------------------------------------

    def handle_play_sound(packet_id: int, data: bytes) -> None:
        """0x54 PlaySound."""
        r = PacketReader(data[1:])
        r.skip(1)  # flags
        sound_id = r.read_u16()
        logger.debug("play_sound", sound_id=f"0x{sound_id:04X}")

    handler.register(0x54, handle_play_sound)

    def handle_char_animation(packet_id: int, data: bytes) -> None:
        """0x6E CharacterAnimation."""
        pass  # silently ignore

    handler.register(0x6E, handle_char_animation)

    def handle_weather(packet_id: int, data: bytes) -> None:
        """0x65 WeatherChange."""
        r = PacketReader(data[1:])
        weather_type = r.read_u8()
        particle_count = r.read_u8()
        temperature = r.read_u8()
        logger.debug("weather", type=weather_type, particles=particle_count, temp=temperature)

    handler.register(0x65, handle_weather)

    def handle_light_level(packet_id: int, data: bytes) -> None:
        """0x4F OverallLightLevel."""
        pass  # silently ignore

    handler.register(0x4F, handle_light_level)

    def handle_personal_light(packet_id: int, data: bytes) -> None:
        """0x4E PersonalLightLevel."""
        pass  # silently ignore

    handler.register(0x4E, handle_personal_light)

    def handle_play_music(packet_id: int, data: bytes) -> None:
        """0x6D PlayMusic."""
        r = PacketReader(data[1:])
        music_id = r.read_u16()
        logger.debug("play_music", music_id=music_id)

    handler.register(0x6D, handle_play_music)

    def handle_season(packet_id: int, data: bytes) -> None:
        """0xBC SeasonChange."""
        r = PacketReader(data[1:])
        season = r.read_u8()
        r.skip(1)  # cursor
        logger.debug("season_change", season=season)

    handler.register(0xBC, handle_season)

    def handle_war_mode(packet_id: int, data: bytes) -> None:
        """0x72 WarMode."""
        r = PacketReader(data[1:])
        war_mode = r.read_u8()
        # War mode lives in the mobile flags (MobileFlags.WAR_MODE, 0x40), NOT
        # in `direction`. `direction` is a 0-7 movement facing that the walker,
        # movement and combat-facing code read directly; OR-ing 0x80 (the
        # RUNNING bit, never WAR_MODE) into it corrupted the facing and lost the
        # war state entirely. Route it through `flags` so `in_war_mode` is
        # queryable, mirroring how `hidden` reads MobileFlags.HIDDEN.
        if war_mode:
            p.self_state.flags |= MobileFlags.WAR_MODE
        else:
            p.self_state.flags &= ~MobileFlags.WAR_MODE
        logger.debug("war_mode", enabled=bool(war_mode))

    handler.register(0x72, handle_war_mode)

    def handle_game_time(packet_id: int, data: bytes) -> None:
        """0x5B GameTime."""
        r = PacketReader(data[1:])
        hour = r.read_u8()
        minute = r.read_u8()
        second = r.read_u8()
        logger.debug("game_time", time=f"{hour:02d}:{minute:02d}:{second:02d}")

    handler.register(0x5B, handle_game_time)

    def handle_open_paperdoll(packet_id: int, data: bytes) -> None:
        """0x88 OpenPaperDoll."""
        pass  # silently ignore

    handler.register(0x88, handle_open_paperdoll)

    def handle_health_bar_status(packet_id: int, data: bytes) -> None:
        """0x16/0x17 NewHealthbarUpdate — color flags on a mobile's health bar.

        Variable packet. Format (ServUO HealthbarPoison/HealthbarYellow,
        Packets.cs:3792-3838):
            [0x17][len:u16][serial:u32][count:u16]
            then `count` entries of [status_type:u16][flag:u8]
        status_type 1 = Poison (flag = poison level + 1, 0 = cured),
        status_type 2 = Yellow/Blessed bar.

        Without this handler the poison flag never reaches WorldState, so the
        brain cannot tell a poisoned target (or itself) apart from a healthy
        one. We translate status_type 1 into both a boolean is_poisoned and a
        0-based poison_level (ServUO Poison.Level: 0=Lesser .. 4=Lethal; the
        wire flag is level + 1, so flag 0 = cured -> level -1) on the mobile /
        self_state. Severity matters: a basic cure spell can fail against a
        higher-level poison, so the cure trigger needs the level, not just a
        boolean.  status_type 2 (the yellow/blessed bar) is the dynamic
        invulnerable signal ClassicUO exposes as Mobile.IsYellowHits
        (Mobile.cs:118): ServUO sends it as a separate HealthbarYellow packet
        (Packets.cs:3817, count=1, status_type=2, flag=1 set / 0 cleared) for
        m.Blessed || m.YellowHealthbar.  Dropping it left is_yellow_health
        stuck at False, so a town-healer / blessed NPC that flips its bar mid-
        session was indistinguishable from an ordinary attackable/healable
        mobile by anything that doesn't already see the static INVULNERABLE
        notoriety.  We record it alongside the poison decode.
        """
        if len(data) < 9:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        if r.remaining < 2:
            return
        count = r.read_u16()

        # poison_level: None = this packet carried no poison entry; -1 = cured;
        # 0..N = ServUO Poison.Level (flag - 1).
        # yellow: None = no yellow/blessed entry in this packet; else the bool.
        poison_level: int | None = None
        yellow: bool | None = None
        for _ in range(count):
            if r.remaining < 3:
                break
            status_type = r.read_u16()
            flag = r.read_u8()
            if status_type == 1:  # poison bar
                poison_level = flag - 1  # flag 0 (cured) -> -1
            elif status_type == 2:  # yellow / blessed bar
                yellow = flag != 0

        if poison_level is None and yellow is None:
            return  # unrecognized status entry — nothing to do

        if serial == p.self_state.serial:
            target = p.self_state
        else:
            # ClassicUO's NewHealthbarUpdate (PacketHandlers.cs:722-727) fetches
            # ``world.Mobiles.Get(serial)`` and RETURNS when it is null — a bare
            # poison/yellow-bar status update NEVER creates a mobile, because the
            # 0x16/0x17 packet carries NO position (the spatial packets
            # 0x78/0x77/0x20 are the only mobile-creating packets). This is the
            # same divergence the 0xA1 / 0x2D / 0x11 handlers already guard:
            # get_or_create_mobile spawned a brand-new MobileInfo at (0,0) for a
            # serial we have not yet (or no longer) seen — e.g. ServUO sends a
            # HealthbarPoison/Yellow for a foe that left view, or whose status
            # update races ahead of its 0x78 — leaving a positionless phantom in
            # world.mobiles (default INNOCENT notoriety, body 0) that pollutes
            # the swarm tally / nearby_mobiles(0,0) / _find_target scans and,
            # with its freshly-stamped last_seen, survives prune_stale_mobiles.
            # Match ClassicUO and the rest of the phantom-guard family: only
            # update a mobile that already exists. touch_existing_mobile also
            # re-stamps last_seen so a stationary foe whose only ongoing signal
            # is a periodic status refresh stays fresh against the pruner.
            target = p.world.touch_existing_mobile(serial)
            if target is None:
                return

        # Each ServUO Healthbar* packet carries exactly ONE status kind, so
        # only update the field its entry was present for — never clobber the
        # other from a packet that didn't mention it.
        if poison_level is not None:
            target.is_poisoned = poison_level >= 0
            target.poison_level = poison_level
        if yellow is not None:
            target.is_yellow_health = yellow

    # ClassicUO routes BOTH 0x16 and 0x17 to the same NewHealthbarUpdate handler
    # (PacketHandlers.cs:197-198) with an identical wire layout. ServUO sends the
    # 0x16 EC variants (HealthbarPoisonEC / HealthbarYellowEC, Packets.cs:3840-3886)
    # to Enhanced-Client sessions and the 0x17 variants otherwise. Registering only
    # 0x17 leaves 0x16 unhandled, so any EC-flagged session loses all poison /
    # yellow-bar status sync. Worse, 0x16 must be VARIABLE-length for CV_500A+
    # clients (ClassicUO PacketsTable.cs:273-276 flips it from 1 -> -1); declaring it
    # fixed-length 1 (the legacy default) mis-frames the rest of the stream and
    # corrupts every subsequent self/world status update.
    handler.register(0x17, handle_health_bar_status)
    handler.register(0x16, handle_health_bar_status)

    def handle_close_vendor(packet_id: int, data: bytes) -> None:
        """0x3B CloseVendorInterface — vendor buy window closed."""
        r = PacketReader(data[3:])  # skip packet id + length
        serial = r.read_u32()
        # Invalidate the cached buy/sell lists once the vendor window closes.
        # The buy/sell procedures poll a *non-empty* vendor_buy_list /
        # vendor_sell_list as the "this vendor's list is ready" signal. If we
        # leave the previous vendor's list populated after it closes, opening a
        # second vendor races: the poll sees the stale list before the new 0x74
        # / 0x9E arrives and trades against the wrong (already-closed) vendor
        # via the stale vendor_serial. Only clear when the close is for the
        # vendor we are currently tracking, so a late 0x3B for an old vendor
        # cannot wipe a freshly-opened one's list.
        if serial == p.self_state.vendor_serial:
            p.self_state.vendor_serial = 0
            p.self_state.vendor_buy_list = []
            p.self_state.vendor_sell_list = []
        logger.debug("vendor_closed", serial=f"0x{serial:08X}")

    handler.register(0x3B, handle_close_vendor)

    def handle_display_death(packet_id: int, data: bytes) -> None:
        """0xAF DisplayDeath — a mobile just died (the authoritative kill signal).

        Fixed 13-byte packet (ServUO DeathAnimation, Packets.cs:467; PACKET_LENGTHS
        already declares 0xAF = 13):
            [0xAF][killed_serial:u32][corpse_serial:u32][running:u32]

        ServUO sends this the instant ``Mobile.OnDeath`` fires — BEFORE the corpse
        item (0x2006) or the 0x1D Delete for the mobile arrives. ClassicUO's
        DisplayDeath (PacketHandlers.cs:3693) consumes it to register the corpse
        and play the death animation. We were dropping it entirely, so the only
        way perception learned a foe died was its last-seen health bar reading
        ``hits <= 0``. That signal is unreliable: ServUO normalises a NON-self
        mobile's hits to a 0-25 scale (AttributeNormalizer, Packets.cs:3348), so a
        foe at 1-3% HP truncates to ``(cur*25)//max == 0`` (false "dead" mid-fight)
        while a foe killed between 0xA1 updates can leave its last bar at hits>0
        and never flip ``MobileInfo.is_dead`` until a laggy 0x1D Delete — which
        ``_confirm_kill`` (combat_loop.py) then cannot distinguish from a kiter
        that simply left view, so a real kill is mis-credited as an escape (no
        loot pass, no kill credit -> depressed COMBAT throughput).

        Mirror ClassicUO: ignore self-death (handled by the resurrect flow) and
        a mobile we have never seen (``owner == null`` there). For a known foe,
        force its health bar to a real zero so ``MobileInfo.is_dead`` and
        ``_confirm_kill``'s ``last_hits_max > 0 and last_hits <= 0`` arm both
        fire deterministically, and emit MOBILE_DIED carrying the corpse serial
        so the loot path can target the body.
        """
        if len(data) < 13:
            return
        r = PacketReader(data[1:])
        serial = r.read_u32()
        corpse_serial = r.read_u32()
        # running (u32) — death-animation variant; unused by perception.

        if serial == p.self_state.serial:
            return  # own death is driven by the death screen / resurrect flow
        mob = p.world.mobiles.get(serial)
        if mob is None:
            return  # never in view — nothing to mark (ClassicUO: owner == null)

        # Authoritative death: pin the bar to zero. hits_max may be 0 if we never
        # queried this foe; bump it to 1 so the `hits_max > 0` death guards fire.
        if mob.hits_max <= 0:
            mob.hits_max = 1
        mob.hits = 0
        p.emit(
            GameEventType.MOBILE_DIED,
            {"serial": serial, "corpse_serial": corpse_serial},
        )
        logger.debug(
            "mobile_died",
            serial=f"0x{serial:08X}",
            corpse=f"0x{corpse_serial:08X}",
        )

    handler.register(0xAF, handle_display_death)
