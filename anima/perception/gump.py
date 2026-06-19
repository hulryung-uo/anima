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

from anima.data import cliloc_text

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
        Strips HTML tags from resolved text before matching.
        """
        substring_lower = substring.lower()
        # Build (text_id → resolved string) for fast lookup
        label_positions: list[tuple[int, int, str]] = []
        for t in self.texts:
            resolved = self.get_text(t.text_id)
            if resolved:
                # Strip HTML tags (e.g. <CENTER>text</CENTER>)
                clean = re.sub(r"<[^>]+>", "", resolved)
                label_positions.append((t.x, t.y, clean))

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


# Matches a cliloc placeholder ``~N~`` or ``~N_label~`` (label is ignored;
# the embedded index N drives the substitution).
_CLILOC_ARG_RE = re.compile(r"~(\d+)(?:_[^~]*)?~")


def _resolve_cliloc_tok(cliloc_num: int, args: str) -> str:
    """Resolve an ``xmfhtmltok`` cliloc + tab-separated args into text.

    Mirrors ClassicUO ClilocLoader.Translate (the same logic the 0xD6 / 0xC1 /
    0xCC handlers use): each ``~N~`` / ``~N_label~`` placeholder is filled by the
    (N-1)th tab-separated arg, keyed off the index *embedded in the placeholder*
    — never by positional order — so out-of-order and repeated placeholders both
    resolve, and every occurrence is replaced (not just the first).

    The previous loop only matched the literal label ``~N_val~`` and did a bogus
    ``#N`` replacement against the base text. Real crafting/tooltip clilocs use
    labels like ``~1_AMOUNT~`` / bare ``~1~``, so item names and quantities in
    crafting menus were left as raw ``~1_AMOUNT~`` placeholders and never matched
    by find_button_near_text.
    """
    base_text = cliloc_text(cliloc_num)
    if base_text and args:
        parts = args.split("\t")

        def _sub(m: "re.Match[str]") -> str:
            idx = int(m.group(1)) - 1
            return parts[idx] if 0 <= idx < len(parts) else m.group(0)

        base_text = _CLILOC_ARG_RE.sub(_sub, base_text)
    # Strip any unresolved placeholders (ClassicUO renders them empty).
    return re.sub(r"~\d+[^~]*~", "", base_text).strip()


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

        elif cmd == "xmfhtmlgump" and len(tokens) >= 8:
            # xmfhtmlgump x y width height cliloc_num background scrollbar
            cliloc_num = _safe_int(tokens[5])
            resolved = cliloc_text(cliloc_num)
            if resolved:
                idx = len(text_lines)
                text_lines.append(resolved)
                gump.texts.append(
                    GumpText(
                        x=_safe_int(tokens[1]),
                        y=_safe_int(tokens[2]),
                        hue=0,
                        text_id=idx,
                    )
                )

        elif cmd == "xmfhtmlgumpcolor" and len(tokens) >= 9:
            # xmfhtmlgumpcolor x y width height cliloc_num background scrollbar hue
            cliloc_num = _safe_int(tokens[5])
            resolved = cliloc_text(cliloc_num)
            if resolved:
                idx = len(text_lines)
                text_lines.append(resolved)
                gump.texts.append(
                    GumpText(
                        x=_safe_int(tokens[1]),
                        y=_safe_int(tokens[2]),
                        hue=_safe_int(tokens[8]) if len(tokens) > 8 else 0,
                        text_id=idx,
                    )
                )

        elif cmd == "xmfhtmltok" and len(tokens) >= 9:
            # xmfhtmltok x y width height background scrollbar hue cliloc_num @args@
            # ClassicUO strips a leading '#' off the cliloc id (PacketHandlers.cs).
            cliloc_num = _safe_int(tokens[8].replace("#", ""))
            # ClassicUO joins the arg tokens with '\t', then trims the surrounding
            # '@' and turns each in-token '@' separator into '\t' before handing
            # the blob to Translate. Replicate that so multi-arg tooltips split.
            if len(tokens) > 9:
                args = "\t".join(tokens[9:]).strip("@").replace("@", "\t")
                resolved = _resolve_cliloc_tok(cliloc_num, args)
            else:
                resolved = _resolve_cliloc_tok(cliloc_num, "")
            if resolved:
                idx = len(text_lines)
                text_lines.append(resolved)
                gump.texts.append(
                    GumpText(
                        x=_safe_int(tokens[1]),
                        y=_safe_int(tokens[2]),
                        hue=_safe_int(tokens[7]) if len(tokens) > 7 else 0,
                        text_id=idx,
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
