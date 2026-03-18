"""Gump (generic UI panel) data model and layout parser.

UO servers send gumps via packet 0xB0 (uncompressed) or 0xDD (zlib-compressed).
The layout is an ASCII string of commands wrapped in ``{ }``.  Text lines are
sent as a separate array of UTF-16 BE strings referenced by index.

This module parses those into structured Python objects so that skills
and the brain can programmatically interact with gumps (e.g. crafting menus).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Parsed layout elements
# ---------------------------------------------------------------------------


@dataclass
class GumpButton:
    x: int
    y: int
    normal_id: int
    pressed_id: int
    button_type: int  # 0 = page switch, 1 = reply (sent to server)
    param: int  # page number (type 0) or unused (type 1)
    button_id: int  # ID reported in GumpResponse


@dataclass
class GumpText:
    """A static text label displayed on the gump."""

    x: int
    y: int
    hue: int
    text_id: int  # index into text_lines


@dataclass
class GumpTextEntry:
    """An editable text input field."""

    x: int
    y: int
    width: int
    height: int
    hue: int
    entry_id: int
    initial_text: str = ""


@dataclass
class GumpSwitch:
    """A checkbox or radio button."""

    x: int
    y: int
    unchecked_id: int
    checked_id: int
    initial_state: bool
    switch_id: int
    is_radio: bool = False


# ---------------------------------------------------------------------------
# Gump container
# ---------------------------------------------------------------------------


@dataclass
class GumpData:
    """A fully parsed gump received from the server."""

    serial: int  # sender serial (NPC / object)
    gump_id: int  # unique gump type ID
    x: int
    y: int
    layout: str  # raw layout command string
    text_lines: list[str] = field(default_factory=list)

    # Parsed elements
    buttons: list[GumpButton] = field(default_factory=list)
    texts: list[GumpText] = field(default_factory=list)
    text_entries: list[GumpTextEntry] = field(default_factory=list)
    switches: list[GumpSwitch] = field(default_factory=list)

    # Flags extracted from layout
    no_close: bool = False
    no_dispose: bool = False
    no_move: bool = False
    no_resize: bool = False

    def get_text(self, text_id: int) -> str:
        """Resolve a text_id to its string from text_lines."""
        if 0 <= text_id < len(self.text_lines):
            return self.text_lines[text_id]
        return ""

    def reply_buttons(self) -> list[GumpButton]:
        """Return only buttons that trigger a server response (type 1)."""
        return [b for b in self.buttons if b.button_type == 1]

    def find_button_near_text(self, substring: str) -> GumpButton | None:
        """Find a reply button whose nearest text label contains *substring*.

        Useful for crafting menus: find the button next to "Boards" etc.
        """
        substring_lower = substring.lower()
        # Build (text_id → resolved string) for fast lookup
        label_positions: list[tuple[int, int, str]] = []
        for t in self.texts:
            resolved = self.get_text(t.text_id)
            if resolved:
                label_positions.append((t.x, t.y, resolved))

        best_button: GumpButton | None = None
        best_dist = float("inf")

        for btn in self.reply_buttons():
            for tx, ty, label in label_positions:
                if substring_lower in label.lower():
                    dist = abs(btn.x - tx) + abs(btn.y - ty)
                    if dist < best_dist:
                        best_dist = dist
                        best_button = btn

        return best_button

    def find_button_by_id(self, button_id: int) -> GumpButton | None:
        """Find a button by its button_id."""
        for b in self.buttons:
            if b.button_id == button_id:
                return b
        return None


# ---------------------------------------------------------------------------
# Layout parser
# ---------------------------------------------------------------------------

# Matches individual commands: { command args... }
_CMD_RE = re.compile(r"\{\s*([^}]*?)\s*\}")


def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (ValueError, IndexError):
        return default


def parse_layout(layout: str, text_lines: list[str]) -> GumpData:
    """Parse a raw layout string into a :class:`GumpData` (without serial/gump_id/x/y).

    The caller must set ``serial``, ``gump_id``, ``x``, ``y`` after calling.
    """
    gump = GumpData(serial=0, gump_id=0, x=0, y=0, layout=layout, text_lines=text_lines)

    for m in _CMD_RE.finditer(layout):
        tokens = m.group(1).split()
        if not tokens:
            continue
        cmd = tokens[0].lower()

        if cmd == "button" and len(tokens) >= 8:
            gump.buttons.append(
                GumpButton(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    normal_id=_safe_int(tokens[3]),
                    pressed_id=_safe_int(tokens[4]),
                    button_type=_safe_int(tokens[5]),
                    param=_safe_int(tokens[6]),
                    button_id=_safe_int(tokens[7]),
                )
            )

        elif cmd == "buttontileart" and len(tokens) >= 12:
            # buttontileart x y normalId pressedId type param buttonId tileId hue w h
            gump.buttons.append(
                GumpButton(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    normal_id=_safe_int(tokens[3]),
                    pressed_id=_safe_int(tokens[4]),
                    button_type=_safe_int(tokens[5]),
                    param=_safe_int(tokens[6]),
                    button_id=_safe_int(tokens[7]),
                )
            )

        elif cmd in ("text", "croppedtext") and len(tokens) >= 5:
            gump.texts.append(
                GumpText(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    hue=_safe_int(tokens[3]),
                    text_id=_safe_int(tokens[4]),
                )
            )

        elif cmd == "htmlgump" and len(tokens) >= 8:
            # htmlgump x y width height text_id background scrollbar
            gump.texts.append(
                GumpText(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    hue=0,
                    text_id=_safe_int(tokens[5]),
                )
            )

        elif cmd == "textentry" and len(tokens) >= 7:
            tid = _safe_int(tokens[6])
            gump.text_entries.append(
                GumpTextEntry(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    width=_safe_int(tokens[3]),
                    height=_safe_int(tokens[4]),
                    hue=_safe_int(tokens[5]),
                    entry_id=tid,
                    initial_text=text_lines[tid] if 0 <= tid < len(text_lines) else "",
                )
            )

        elif cmd == "textentrylimited" and len(tokens) >= 8:
            tid = _safe_int(tokens[6])
            gump.text_entries.append(
                GumpTextEntry(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    width=_safe_int(tokens[3]),
                    height=_safe_int(tokens[4]),
                    hue=_safe_int(tokens[5]),
                    entry_id=tid,
                    initial_text=text_lines[tid] if 0 <= tid < len(text_lines) else "",
                )
            )

        elif cmd == "checkbox" and len(tokens) >= 7:
            gump.switches.append(
                GumpSwitch(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    unchecked_id=_safe_int(tokens[3]),
                    checked_id=_safe_int(tokens[4]),
                    initial_state=bool(_safe_int(tokens[5])),
                    switch_id=_safe_int(tokens[6]),
                )
            )

        elif cmd == "radio" and len(tokens) >= 7:
            gump.switches.append(
                GumpSwitch(
                    x=_safe_int(tokens[1]),
                    y=_safe_int(tokens[2]),
                    unchecked_id=_safe_int(tokens[3]),
                    checked_id=_safe_int(tokens[4]),
                    initial_state=bool(_safe_int(tokens[5])),
                    switch_id=_safe_int(tokens[6]),
                    is_radio=True,
                )
            )

        elif cmd == "noclose":
            gump.no_close = True
        elif cmd == "nodispose":
            gump.no_dispose = True
        elif cmd == "nomove":
            gump.no_move = True
        elif cmd == "noresize":
            gump.no_resize = True

    return gump
