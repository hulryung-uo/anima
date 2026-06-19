"""Packet handlers that update Perception state.

All handlers are synchronous — they mutate state in-place with no I/O.
"""

from __future__ import annotations

import struct
import time
import zlib

import structlog

from anima.client.codec import PacketReader
from anima.client.handler import PacketHandler
from anima.data import cliloc_text
from anima.perception import Perception
from anima.perception.enums import Direction, Lock, MobileFlags, NotorietyFlag
from anima.perception.event_stream import GameEventType
from anima.perception.gump import parse_layout
from anima.perception.self_state import SkillInfo, VendorBuyItem, VendorSellItem
from anima.perception.walker import WalkerManager

logger = structlog.get_logger()


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

        if serial == p.self_state.serial:
            # Don't track self in world mobiles
            walker.sync_position(x, y, z, direction & 0x07)
            p.self_state.body = body
            # ServUO pushes self 0x78 on flag changes (OnHiddenChanged) —
            # this is where the hidden bit (0x80) for our own char arrives.
            p.self_state.flags = MobileFlags(flags & 0xFF)
            return

        mob = p.world.get_or_create_mobile(serial)
        mob.body = body
        mob.x = x
        mob.y = y
        mob.z = z
        mob.direction = Direction.from_byte(direction)
        mob.hue = hue
        mob.flags = MobileFlags(flags & 0xFF)
        if 1 <= notoriety <= 7:
            mob.notoriety = NotorietyFlag(notoriety)

        # Parse equipment items that follow
        while r.remaining >= 4:
            item_serial = r.read_u32()
            if item_serial == 0:
                break
            if r.remaining < 3:  # need graphic(2) + layer(1)
                break
            graphic = r.read_u16()
            layer = r.read_u8()
            hue = 0
            if graphic & 0x8000:
                graphic &= 0x7FFF
                if r.remaining < 2:
                    break
                hue = r.read_u16()
            item = p.world.get_or_create_item(item_serial)
            item.graphic = graphic
            item.hue = hue
            item.layer = layer
            item.container = serial

            # If this is our character, register equipment
            if serial == p.self_state.serial and layer > 0:
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
            return  # self position tracked by confirm_walk / deny_walk / 0x20

        mob = p.world.get_or_create_mobile(serial)
        mob.body = body
        mob.x = x
        mob.y = y
        mob.z = z
        mob.direction = Direction.from_byte(direction)
        mob.hue = hue
        mob.flags = MobileFlags(flags & 0xFF)
        if 1 <= notoriety <= 7:
            mob.notoriety = NotorietyFlag(notoriety)

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
        else:
            mob = p.world.get_or_create_mobile(serial)
            mob.body = body
            mob.x = x
            mob.y = y
            mob.z = z
            mob.direction = Direction.from_byte(direction)
            mob.hue = hue
            mob.flags = MobileFlags(flags & 0xFF)

    handler.register(0x20, handle_mobile_update)

    def handle_delete(packet_id: int, data: bytes) -> None:
        """0x1D Delete — entity removed from the world."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        was_mobile = serial in p.world.mobiles
        p.world.remove(serial)
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

        p.emit(GameEventType.ITEM_APPEARED, {"serial": serial, "x": x, "y": y})

    handler.register(0x1A, handle_world_item)

    def handle_update_item_sa(packet_id: int, data: bytes) -> None:
        """0xF3 UpdateItemSA — modern item update."""
        r = PacketReader(data[1:])
        r.skip(2)  # unknown
        r.read_u8()  # data_type: 0x00 = item, 0x02 = multi
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
                # Female, race omitted — skip: sex(1) + race(1) if available
                r.skip(1)  # sex
                p.self_state.strength = r.read_u16()
                p.self_state.dexterity = r.read_u16()
                p.self_state.intelligence = r.read_u16()
                p.self_state.stam = r.read_u16()
                p.self_state.stam_max = r.read_u16()
                p.self_state.mana = r.read_u16()
                p.self_state.mana_max = r.read_u16()
                p.self_state.gold = r.read_u32()
                p.self_state.armor = r.read_u16()
                p.self_state.weight = r.read_u16()

            if flag >= 5 and r.remaining >= 2:
                p.self_state.weight_max = r.read_u16()
                if r.remaining >= 1:
                    r.skip(1)  # race
            else:
                # Calculate weight_max from STR (UOR formula)
                p.self_state.weight_max = 7 * (p.self_state.strength // 2) + 40

            # stat_cap + followers form one contiguous block that ServUO
            # writes for every self-status (type >= 1), regardless of
            # expansion. They must be read together or the resist block
            # below misaligns. (Ref: ServUO MobileStatus, ClassicUO 0x11.)
            if r.remaining >= 4:
                p.self_state.stat_cap = r.read_u16()
                p.self_state.followers = r.read_u8()
                p.self_state.followers_max = r.read_u8()

            # AOS+ block (type >= 4): four resists, luck, and the damage
            # range are a single contiguous run on the wire — luck/damage
            # are NOT a separate type-6 field. The old code gated them on
            # flag >= 6, so on a stock AOS/ML shard (type 4 or 5) luck and
            # damage_min/max were never decoded. A trailing tithing u32
            # follows and must be consumed so any type-6 tail stays aligned.
            if flag >= 4 and r.remaining >= 8:
                p.self_state.resist_fire = r.read_u16()
                p.self_state.resist_cold = r.read_u16()
                p.self_state.resist_poison = r.read_u16()
                p.self_state.resist_energy = r.read_u16()
                if r.remaining >= 6:
                    p.self_state.luck = r.read_u16()
                    p.self_state.damage_min = r.read_u16()
                    p.self_state.damage_max = r.read_u16()
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
            mob = p.world.get_or_create_mobile(serial)
            mob.name = name
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
            mob = p.world.get_or_create_mobile(serial)
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
        has_cap = list_type in (0x02, 0x03, 0xDF, 0xFF)
        # For full lists (0x00, 0x02), ClassicUO decrements skill_id by 1
        adjust_id = list_type in (0x00, 0x01, 0x02, 0x03)

        while r.remaining >= 2:
            skill_id = r.read_u16()

            # Full lists are terminated by skill_id=0
            if not is_single and skill_id == 0:
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
            skill.value = value / 10.0
            skill.base = base / 10.0
            skill.cap = cap / 10.0
            if 0 <= lock <= 2:
                skill.lock = Lock(lock)

            p.emit(GameEventType.SKILL_CHANGED, {"skill_id": skill_id, "value": skill.value})

            # Log skill gains/losses to journal and activity feed
            diff = skill.value - old_value
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
                msg = f"{arrow} {sname} {old_value:.1f} \u2192 {skill.value:.1f}"
                p.social.add_speech(0xFFFFFFFF, "System", msg, 0)
                logger.info(
                    "skill_gain", skill=sname,
                    old=old_value, new=skill.value, diff=f"{diff:+.1f}",
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
        graphic = r.read_u16()
        r.skip(1)  # unknown
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
            r.skip(1)  # graphic_inc
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

    handler.register(0x3C, handle_container_content)

    def handle_add_item_to_container(packet_id: int, data: bytes) -> None:
        """0x25 AddItemToContainer — single item added to container."""
        r = PacketReader(data[1:])
        serial = r.read_u32()
        graphic = r.read_u16()
        r.skip(1)  # graphic_inc
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
        if msg_type == 6 and serial:
            mob = p.world.mobiles.get(serial)
            if mob is not None and text:
                mob.name = text  # e.g. "Hastin the baker"
            item = p.world.items.get(serial)
            if item is not None and text:
                item.name = text

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
        args_raw = data[3 + r.position:]
        try:
            args = args_raw.decode("utf-16-le").rstrip("\x00")
        except (UnicodeDecodeError, ValueError):
            args = ""

        # Resolve cliloc text
        base_text = cliloc_text(cliloc_num)
        if base_text and args:
            parts = args.split("\t")
            text = base_text
            import re as _re
            for i, part in enumerate(parts):
                text = _re.sub(rf"~{i + 1}(?:_[^~]*)?~", part, text, count=1)
                text = text.replace(f"#{i + 1}", part)
        elif base_text:
            text = base_text
        else:
            text = f"[cliloc {cliloc_num}]"

        import re as _re
        text = _re.sub(r"~\d+[^~]*~", "", text).strip()

        if not name:
            name = "System"

        p.social.add_speech(serial, name, text, msg_type, hue)
        p.emit(GameEventType.SPEECH_HEARD, {
            "serial": serial, "name": name, "text": text,
        })
        logger.info("speech_cliloc", name=name, text=text, cliloc=cliloc_num)

    handler.register(0xC1, handle_cliloc_message)

    # ------------------------------------------------------------------
    # Movement packets
    # ------------------------------------------------------------------

    _dir_names = {0: "N", 1: "NE", 2: "E", 3: "SE", 4: "S", 5: "SW", 6: "W", 7: "NW"}

    def handle_confirm_walk(packet_id: int, data: bytes) -> None:
        """0x22 ConfirmWalk."""
        r = PacketReader(data[1:])
        seq = r.read_u8()
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
        # Store revision so we know OPL exists for this entity
        p.world.opl_revisions[serial] = revision

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
                args = raw.decode("utf-16-le", errors="replace")
                r.skip(text_len)
            elif text_len > 0:
                break  # not enough data

            base_text = cliloc_text(cliloc_num)
            if base_text and args:
                parts = args.split("\t")
                text = base_text
                for i, part in enumerate(parts):
                    text = re.sub(rf"~{i + 1}_[^~]*~", part, text, count=1)
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
            if name and not mob.name:
                mob.name = name
        item = p.world.items.get(serial)
        if item is not None:
            item.properties = properties
            if name and not item.name:
                item.name = name
        # Cache so a later 0x1D/0x78 round-trip for the same NPC doesn't
        # blank out the name we just learned.
        if name:
            p.world.opl_names[serial] = name
        if properties:
            p.world.opl_properties[serial] = properties

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
        cursor_flag = r.read_u8()  # 0=neutral, 1=harmful, 2=helpful

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

        p.self_state.gumps[gump_id] = gump
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

        p.self_state.gumps[gump_id] = gump
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
            # DisplayContextMenu: subcmd(2) + unk(2) + serial(4) + count(1)
            # Per entry: cliloc(4) + index(2) + flags(2) = 8 bytes each
            from anima.perception.self_state import ContextMenuEntry as CMEntry

            raw = data[5:]
            # unk(2) + serial(4) + count(1)
            serial = struct.unpack(">I", raw[2:6])[0]
            count = raw[6]
            entry_offset = 7
            entries: list[CMEntry] = []
            for i in range(count):
                off = entry_offset + i * 8
                if off + 8 > len(raw):
                    break
                cliloc = struct.unpack(">I", raw[off : off + 4])[0]
                index = struct.unpack(">H", raw[off + 4 : off + 6])[0]
                flags = struct.unpack(">H", raw[off + 6 : off + 8])[0]
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
        """0x17 HealthBarStatusUpdate — color flags on a mobile's health bar.

        Variable packet. Format (ServUO HealthbarPoison/HealthbarYellow,
        Packets.cs:3792-3838):
            [0x17][len:u16][serial:u32][count:u16]
            then `count` entries of [status_type:u16][flag:u8]
        status_type 1 = Poison (flag = poison level + 1, 0 = cured),
        status_type 2 = Yellow/Blessed bar.

        Without this handler the poison flag never reaches WorldState, so the
        brain cannot tell a poisoned target (or itself) apart from a healthy
        one. We translate status_type 1 into a boolean is_poisoned on the
        mobile / self_state.
        """
        if len(data) < 9:
            return
        r = PacketReader(data[3:])  # variable: skip id + length
        serial = r.read_u32()
        if r.remaining < 2:
            return
        count = r.read_u16()

        poisoned: bool | None = None
        for _ in range(count):
            if r.remaining < 3:
                break
            status_type = r.read_u16()
            flag = r.read_u8()
            if status_type == 1:  # poison bar
                poisoned = flag != 0

        if poisoned is None:
            return  # only yellow/blessed bars in this packet — nothing to do

        if serial == p.self_state.serial:
            p.self_state.is_poisoned = poisoned
        else:
            p.world.get_or_create_mobile(serial).is_poisoned = poisoned

    handler.register(0x17, handle_health_bar_status)

    def handle_close_vendor(packet_id: int, data: bytes) -> None:
        """0x3B CloseVendorInterface — vendor buy window closed."""
        r = PacketReader(data[3:])  # skip packet id + length
        serial = r.read_u32()
        logger.debug("vendor_closed", serial=f"0x{serial:08X}")

    handler.register(0x3B, handle_close_vendor)
