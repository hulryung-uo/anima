# UOR Skills & Stats Reference

> Ultima Online Renaissance (UOR) 시대 기준 스킬/스탯 메커니즘 레퍼런스

## Caps

| 항목 | 값 |
|------|-----|
| Total Skill Cap | 700.0 |
| Individual Skill Cap | 100.0 |
| Total Stat Cap | 225 |
| Individual Stat Cap | 100 |

## Skill Lock States

| 값 | 상태 | 설명 |
|----|------|------|
| 0 | Up | 스킬 사용 시 상승 가능 |
| 1 | Down | 다른 스킬 상승 시 하락 가능 |
| 2 | Locked | 변동 없음 |

스킬 총합이 700에 도달하면, Up 상태 스킬이 올라가려면 Down 상태 스킬이 내려가야 함.

## Stat Lock States

스탯도 동일한 Lock 시스템 사용 (0=Up, 1=Down, 2=Locked).
스킬 사용 시 연관된 스탯이 상승할 수 있으며, 총합 225 도달 시 Down 스탯이 내려감.

## Skill List (UOR Era)

| ID | Name | Primary Stat | Category |
|----|------|-------------|----------|
| 0 | Alchemy | INT | Crafting |
| 1 | Anatomy | INT | Combat |
| 2 | Animal Lore | INT | Wilderness |
| 3 | Item ID | INT | Misc |
| 4 | Arms Lore | INT | Combat |
| 5 | Parrying | DEX | Combat |
| 6 | Begging | DEX | Misc |
| 7 | Blacksmith | STR | Crafting |
| 8 | Bowcraft | DEX | Crafting |
| 9 | Peacemaking | INT | Bard |
| 10 | Camping | INT | Wilderness |
| 11 | Carpentry | STR | Crafting |
| 12 | Cartography | INT | Crafting |
| 13 | Cooking | INT | Crafting |
| 14 | Detect Hidden | INT | Misc |
| 15 | Enticement | INT | Bard |
| 16 | Eval Intelligence | INT | Magic |
| 17 | Healing | INT | Combat |
| 18 | Fishing | DEX | Gathering |
| 19 | Forensic Eval | INT | Misc |
| 20 | Herding | INT | Wilderness |
| 21 | Hiding | DEX | Stealth |
| 22 | Provocation | INT | Bard |
| 23 | Inscription | INT | Crafting |
| 24 | Lockpicking | DEX | Thief |
| 25 | Magery | INT | Magic |
| 26 | Resisting Spells | INT | Magic |
| 27 | Tactics | STR | Combat |
| 28 | Snooping | DEX | Thief |
| 29 | Musicianship | DEX | Bard |
| 30 | Poisoning | INT | Combat |
| 31 | Archery | DEX | Combat |
| 32 | Spirit Speak | INT | Magic |
| 33 | Stealing | DEX | Thief |
| 34 | Tailoring | DEX | Crafting |
| 35 | Animal Taming | INT | Wilderness |
| 36 | Taste ID | INT | Misc |
| 37 | Tinkering | DEX | Crafting |
| 38 | Tracking | INT | Wilderness |
| 39 | Veterinary | INT | Wilderness |
| 40 | Swordsmanship | STR | Combat |
| 41 | Mace Fighting | STR | Combat |
| 42 | Fencing | DEX | Combat |
| 43 | Wrestling | STR | Combat |
| 44 | Lumberjacking | STR | Gathering |
| 45 | Mining | STR | Gathering |
| 46 | Meditation | INT | Magic |
| 47 | Stealth | DEX | Stealth |
| 48 | Remove Trap | DEX | Thief |

## Packets

### Skill Lock (0x3A, Variable)

Client → Server: 스킬 잠금 상태 변경

```
[0x3A] [length: u16 BE] [skill_id: u16 BE] [lock_state: u8]
```

Server → Client: 스킬 목록 업데이트

```
[0x3A] [length: u16 BE] [list_type: u8]
  list_type:
    0x00 = Full list (no caps)
    0x02 = Single update (with cap)
    0xFF = Full list (with caps)
    0xDF = Single update (with cap)
  Per skill:
    [skill_id: u16 BE] [value: u16 BE] [base: u16 BE] [lock: u8] [cap: u16 BE]
  Values in tenths (45.5 = 455)
```

### Stat Lock (0xBF subcommand 0x1A, Variable)

Client → Server: 스탯 잠금 상태 변경

```
[0xBF] [length: u16 BE] [0x001A: u16 BE] [stat_index: u8] [lock_state: u8]
  stat_index: 0=STR, 1=DEX, 2=INT
```

## Persona-Skill Mapping

각 페르소나별로 올려야 할 스킬(Up)과 잠글 스킬(Locked)을 정의.

### Adventurer
- **Up**: Swordsmanship(40), Healing(17), Tactics(27), Anatomy(1), Parrying(5)
- **Stats**: STR Up, DEX Up, INT Locked

### Blacksmith
- **Up**: Mining(45), Blacksmith(7), Arms Lore(4), Tinkering(37)
- **Stats**: STR Up, DEX Locked, INT Locked

### Merchant
- **Up**: Tinkering(37), Tailoring(34), Item ID(3), Arms Lore(4)
- **Stats**: STR Locked, DEX Up, INT Up

### Mage
- **Up**: Magery(25), Meditation(46), Eval Intelligence(16), Resisting Spells(26), Inscription(23)
- **Stats**: STR Locked, DEX Locked, INT Up

### Bard
- **Up**: Musicianship(29), Peacemaking(9), Provocation(22), Magery(25)
- **Stats**: STR Locked, DEX Up, INT Up

### Ranger
- **Up**: Archery(31), Tactics(27), Healing(17), Tracking(38), Lumberjacking(44)
- **Stats**: STR Up, DEX Up, INT Locked
