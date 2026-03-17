"""Packet handlers that update Perception state.

All handlers are synchronous — they mutate state in-place with no I/O.
"""

from __future__ import annotations

import struct

import structlog

from anima.client.codec import PacketReader
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.enums import Direction, Lock, MobileFlags, NotorietyFlag
from anima.perception.event_stream import GameEventType
from anima.perception.self_state import SkillInfo
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

        p.emit(GameEventType.MOBILE_APPEARED, {"serial": serial, "x": x, "y": y})
        logger.debug(
            "mobile_incoming", serial=f"0x{serial:08X}",
            body=f"0x{body:04X}", pos=f"({x},{y},{z})",
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
            return  # self position managed by walker

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
        graphic = r.read_u16()

        amount = 0
        if serial & 0x80000000:
            serial &= 0x7FFFFFFF
            amount = r.read_u16()

        if graphic & 0x8000:
            graphic &= 0x7FFF
            graphic += r.read_u8()  # graphic_inc

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
                r.skip(1)  # race

            if flag >= 2 and r.remaining >= 2:
                p.self_state.stat_cap = r.read_u16()

            if flag >= 3 and r.remaining >= 2:
                p.self_state.followers = r.read_u8()
                p.self_state.followers_max = r.read_u8()

            if flag >= 4 and r.remaining >= 8:
                p.self_state.resist_fire = r.read_u16()
                p.self_state.resist_cold = r.read_u16()
                p.self_state.resist_poison = r.read_u16()
                p.self_state.resist_energy = r.read_u16()

            if flag >= 6 and r.remaining >= 4:
                p.self_state.luck = r.read_u16()
                p.self_state.damage_min = r.read_u16()
                p.self_state.damage_max = r.read_u16()

            p.emit(GameEventType.STATS_CHANGED, {"serial": serial})
            logger.debug(
                "self_stats",
                hp=f"{hits}/{hits_max}",
                str=p.self_state.strength,
                dex=p.self_state.dexterity,
                int=p.self_state.intelligence,
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
        """0x3A SkillUpdate — skill list or single skill change."""
        r = PacketReader(data[3:])  # variable: skip id + length
        list_type = r.read_u8()  # 0x00 = full, 0x02 = single, 0xFF = full + caps
        while r.remaining >= 2:
            if list_type in (0x00, 0xFF):
                skill_id = r.read_u16()
                if skill_id == 0 and r.remaining < 5:
                    break
            elif list_type == 0x02:
                skill_id = r.read_u16()
            elif list_type == 0xDF:
                skill_id = r.read_u16()
            else:
                break

            if r.remaining < 5:
                break

            value = r.read_u16()
            base = r.read_u16()
            lock = r.read_u8()
            cap = 0
            if list_type in (0xFF, 0xDF, 0x02) and r.remaining >= 2:
                cap = r.read_u16()

            skill = p.self_state.skills.get(skill_id)
            if skill is None:
                skill = SkillInfo(id=skill_id)
                p.self_state.skills[skill_id] = skill
            skill.value = value / 10.0
            skill.base = base / 10.0
            skill.cap = cap / 10.0
            if 0 <= lock <= 2:
                skill.lock = Lock(lock)

            p.emit(GameEventType.SKILL_CHANGED, {"skill_id": skill_id, "value": skill.value})

            if list_type == 0x02:
                break  # single skill update

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
        """0x3C ContainerContent — items inside a container."""
        r = PacketReader(data[3:])  # variable: skip id + length
        count = r.read_u16()
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

        p.social.add_speech(serial, name, text, msg_type, hue)
        p.emit(
            GameEventType.SPEECH_HEARD,
            {"serial": serial, "name": name, "text": text, "type": msg_type},
        )
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

    # ------------------------------------------------------------------
    # Movement packets
    # ------------------------------------------------------------------

    def handle_confirm_walk(packet_id: int, data: bytes) -> None:
        """0x22 ConfirmWalk."""
        r = PacketReader(data[1:])
        seq = r.read_u8()
        walker.confirm_walk(seq)
        logger.debug("walk_confirmed", seq=seq)

    handler.register(0x22, handle_confirm_walk)

    def handle_deny_walk(packet_id: int, data: bytes) -> None:
        """0x21 DenyWalk."""
        r = PacketReader(data[1:])
        seq = r.read_u8()
        x = r.read_u16()
        y = r.read_u16()
        direction = r.read_u8() & 0x07
        z = r.read_i8()
        walker.deny_walk(seq, x, y, z, direction)
        logger.info("walk_denied", seq=seq, pos=f"({x},{y},{z})")

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
