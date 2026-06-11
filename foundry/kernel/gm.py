"""GM fixed-start driver (FOUNDRY.md §5 eval protocol) — kernel-owned.

Standardizes the eval start state by driving a GameMaster account over the
standard UO wire protocol: teleport the eval character to its workplace, pin
the profession skill to a fixed baseline, and hand it its tool. Combined with
``trajectory.parse_file(window_start=...)`` the setup never contaminates the
scored window (skills shift baseline, GM-given items are excluded).

Design constraints honored here:
  - The kernel never imports anima/ (anti-gaming) — this is a from-scratch
    minimal client speaking only the packets it needs.
  - No Huffman decoder needed: the GM connects THROUGH its own uo_proxy
    instance, sends raw bytes on the socket (C->S is never compressed), and
    "reads" the server by tailing the proxy's decompressed JSONL log.
  - Login phase (account login + redirect) is uncompressed and read directly
    from the socket. Blind 0x5D works because ServUO validates only the slot.
  - All [commands needing a target cursor are answered by serial via 0x6C,
    so parallel evals are never touched ([Online would hit every slot).

The GM account must exist with GameMaster access — provisioned once by
``python -m foundry.kernel.provision`` (a deliberate human act, FOUNDRY.md §2).
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ANIMA_ROOT = Path(__file__).resolve().parents[2]

GM_USER = "foundrygm"
GM_PASS = "foundry-gm-pass"
GM_CHAR = "FoundryGM"
CLIENT_VERSION = "7.0.102.3"

# Fixed-start profiles, keyed by persona (FOUNDRY.md §5 "fixed start state").
# "go" is self-calibrated for Z: the GM [Go-es there first and the server
# settles it onto valid ground; the eval char is then [Set to the GM's spot.
FIXED_START_PROFILES: dict[str, dict] = {
    "miner": {
        # Minoc east mountain face at ground level — calibrated offline from
        # map data: a walkable tile with ~19 mineable tiles within reach 2.
        "go": (2567, 493),
        "skills": {"Mining": 35.0},        # uniform gain region across evals
        "items": ["Pickaxe", "Pickaxe"],   # spare tool (they wear out)
        # Shard-granted starting wealth is front-loading (FOUNDRY.md §5 fixed
        # start: "no consumables to front-load") and derails the planner into
        # a bank trip before any work. Shrink these pack stacks to amount 1.
        "neutralize": [0x0EED],            # gold coins
    },
    # Profession profiles share the calibrated Minoc spot — any open
    # ground works for these trades, and reusing it keeps Z-calibration
    # and the eval's region accounting uniform across professions.
    #
    # Skill names must match the ServUO SkillName enum property path
    # ([Set Skills.<Name>.Base) — note "Swords", not "Swordsmanship".
    "mage": {
        "go": (2567, 493),
        # Magery 35 → Greater Heal (circle 4) sits in the gain window;
        # Meditation 60 keeps mana cycling fast enough for a 10-min eval.
        "skills": {"Magery": 35.0, "Meditation": 60.0},
        # Loose reagent stacks (creation reagents arrive nested in a bag
        # the agent's flat backpack search can't see into). 50 each ≈
        # 20 stones — 200 each overloaded the low-STR mage (159/135
        # weight) and wedged movement; 50 covers ~50 Greater Heals.
        "items": ["Garlic 50", "Ginseng 50", "SpidersSilk 50",
                  "MandrakeRoot 50"],
        "neutralize": [0x0EED],
    },
    "warrior": {
        "go": (2567, 493),
        "skills": {"Swords": 35.0, "Tactics": 35.0, "Healing": 35.0},
        "items": ["Katana", "Bandage 100"],
        # Standardized arena: weak melee fodder spawned around the
        # workplace (nearest wild spawns are ettins ~190 tiles south —
        # lethal at skill 35). HeadlessOne: HP 16-30, Wrestling 25-40.
        "spawn_mobs": ["HeadlessOne"] * 8,
        "neutralize": [0x0EED],
    },
    "bard": {
        "go": (2567, 493),
        "skills": {"Musicianship": 35.0, "Peacemaking": 35.0},
        "items": ["Lute"],
        "neutralize": [0x0EED],
    },
    "thief": {
        "go": (2567, 493),
        "skills": {"Hiding": 35.0, "Stealth": 35.0},
        "items": [],                       # hiding needs nothing
        "neutralize": [0x0EED],
    },
}


# --- minimal packet builders (layouts cross-checked vs ServUO + live capture) --

def build_seed(seed: int) -> bytes:
    return struct.pack(">BIIIII", 0xEF, seed & 0xFFFFFFFF, 7, 0, 102, 3)


def _ascii(s: str, n: int) -> bytes:
    raw = s.encode("ascii", errors="replace")[:n]
    return raw + b"\x00" * (n - len(raw))


def build_account_login(user: str, password: str) -> bytes:
    return b"\x80" + _ascii(user, 30) + _ascii(password, 30) + b"\xff"


def build_server_select(index: int = 0) -> bytes:
    return struct.pack(">BH", 0xA0, index)


def build_game_login(auth_key: int, user: str, password: str) -> bytes:
    return struct.pack(">BI", 0x91, auth_key & 0xFFFFFFFF) + _ascii(user, 30) + _ascii(password, 30)


def build_client_version(version: str = CLIENT_VERSION) -> bytes:
    body = version.encode("ascii") + b"\x00"
    return struct.pack(">BH", 0xBD, 3 + len(body)) + body


def build_play_character(name: str, slot: int = 0) -> bytes:
    return (
        b"\x5d" + struct.pack(">I", 0xEDEDEDED) + _ascii(name, 30)
        + b"\x00" * 2 + struct.pack(">I", 0) + b"\x00" * 24
        + struct.pack(">II", slot, 0x7F000001)
    )


def build_create_character(name: str, city_index: int = 0, slot: int = 0) -> bytes:
    """CreateCharacter70 (0xF8, 106 bytes) — minimal GM body (str/dex/int 60/10/10)."""
    w = bytearray()
    w += b"\xf8"
    w += struct.pack(">IIB", 0xEDEDEDED, 0xFFFFFFFF, 0x00)
    w += _ascii(name, 30)
    w += b"\x00" * 2
    w += struct.pack(">III", 0, 1, 0)      # flags / unknown=1 / login count
    w += b"\x00"                           # profession: custom
    w += b"\x00" * 15
    w += b"\x00"                           # male human
    w += bytes([60, 10, 10])               # str/dex/int (total 80)
    w += bytes([45, 50, 7, 50, 0, 0, 0, 0])  # Mining 50 / Blacksmith 50 (valid combo)
    w += struct.pack(">HHHHH", 0x03EA, 0x203C, 0x044E, 0, 0)  # skin/hair/hues
    w += struct.pack(">HHHI", city_index, 0, slot, 0x7F000001)
    w += struct.pack(">HH", 0x0084, 0x0044)  # shirt/pants hue
    data = bytes(w)
    return data + b"\x00" * (106 - len(data)) if len(data) < 106 else data[:106]


def build_unicode_speech(text: str, hue: int = 0x0034, font: int = 3) -> bytes:
    """Plain-mode 0xAD (no keyword encoding — [commands need none)."""
    body = (
        b"\x00" + struct.pack(">HH", hue, font) + b"ENU\x00"
        + text.encode("utf-16-be") + b"\x00\x00"
    )
    pkt = b"\xad" + struct.pack(">H", 3 + len(body)) + body
    return pkt


def build_target_response(cursor_id: int, serial: int) -> bytes:
    """0x6C object-target response (19 bytes)."""
    return struct.pack(">BBIBIHHHH", 0x6C, 0x00, cursor_id, 0x00, serial, 0, 0, 0, 0)


def build_ground_target_response(cursor_id: int, x: int, y: int, z: int) -> bytes:
    """0x6C ground-target response (19 bytes, target_type=1)."""
    return struct.pack(
        ">BBIBIHHHH", 0x6C, 0x01, cursor_id, 0x00, 0, x, y, z & 0xFFFF, 0,
    )


# --- decompressed-trajectory tail reader --------------------------------------

class JsonlWatch:
    """Incremental reader over a uo_proxy JSONL file (complete lines only)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._pos = 0

    def poll(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with open(self.path, "rb") as fh:
            fh.seek(self._pos)
            chunk = fh.read()
        if not chunk:
            return out
        end = chunk.rfind(b"\n")
        if end < 0:
            return out
        for raw in chunk[: end + 1].splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        self._pos += end + 1
        return out

    def wait_for(self, pred, timeout_s: float = 20.0) -> dict | None:
        """Poll until an event satisfies pred(ev, data: bytes) -> bool."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for ev in self.poll():
                try:
                    data = bytes.fromhex(ev.get("hex", ""))
                except ValueError:
                    continue
                if pred(ev, data):
                    ev["_data"] = data
                    return ev
            time.sleep(0.15)
        return None


def wait_pack_item(traj_path: str | Path, graphic: int,
                   min_amount: int = 2, timeout_s: float = 25.0) -> int | None:
    """Find a pack item's serial by graphic from a trajectory's 0x3C records.

    Re-reads the file from the start (its own cursor) and keeps polling until
    the container contents (sent when the agent opens its backpack) appear.
    """
    w = JsonlWatch(traj_path)

    def scan(ev: dict, d: bytes) -> bool:
        return (ev.get("direction") == "S->C" and ev.get("pid") == "0x3C"
                and len(d) >= 25)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ev = w.wait_for(scan, timeout_s=max(0.2, deadline - time.monotonic()))
        if ev is None:
            return None
        d = ev["_data"]
        count = struct.unpack_from(">H", d, 3)[0]
        off = 5
        for _ in range(count):
            if off + 20 > len(d):
                break
            serial, g = struct.unpack_from(">IH", d, off)
            amount = struct.unpack_from(">H", d, off + 7)[0]
            if g == graphic and amount >= min_amount:
                return serial
            off += 20
    return None


def wait_login_confirm(traj_path: str | Path, timeout_s: float = 60.0,
                       watch: JsonlWatch | None = None) -> dict | None:
    """Wait for 0x1B in a trajectory; returns {serial,x,y} (used by eval too)."""
    w = watch or JsonlWatch(traj_path)
    ev = w.wait_for(
        lambda e, d: e.get("direction") == "S->C" and e.get("pid") == "0x1B" and len(d) >= 15,
        timeout_s,
    )
    if ev is None:
        return None
    d = ev["_data"]
    return {
        "serial": struct.unpack_from(">I", d, 1)[0],
        "x": struct.unpack_from(">H", d, 11)[0],
        "y": struct.unpack_from(">H", d, 13)[0],
        "z": struct.unpack_from(">h", d, 15)[0],
        "ts": float(ev.get("ts", 0.0)),
        "watch": w,
    }


# --- the GM session ------------------------------------------------------------

@dataclass
class GmConfig:
    username: str = GM_USER
    password: str = GM_PASS
    char_name: str = GM_CHAR
    host: str = "127.0.0.1"
    server_port: int = 2594
    proxy_port: int = 2612
    log_dir: Path = field(default_factory=lambda: ANIMA_ROOT / "data" / "eval_logs")

    @property
    def trajectory_path(self) -> Path:
        return ANIMA_ROOT / "data" / "trajectories" / f"gm-{self.proxy_port}.jsonl"


class GmError(RuntimeError):
    pass


class GmClient:
    """A send-only UO client with proxy-log eyes, holding one GM session."""

    def __init__(self, cfg: GmConfig | None = None) -> None:
        self.cfg = cfg or GmConfig()
        self.proxy: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.watch: JsonlWatch | None = None
        self.serial: int | None = None
        self.pos: tuple[int, int, int] | None = None  # last known self (x, y, z)
        self._drain: threading.Thread | None = None
        self._closing = False

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "GmClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        cfg = self.cfg
        if cfg.trajectory_path.exists():
            cfg.trajectory_path.unlink()
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        logfh = open(cfg.log_dir / f"gm-proxy-{cfg.proxy_port}.log", "ab", buffering=0)
        self.proxy = subprocess.Popen(
            [
                "uv", "run", "python", "-m", "uo_proxy",
                "--upstream", f"{cfg.host}:{cfg.server_port}",
                "--listen", f"{cfg.host}:{cfg.proxy_port}",
                "--advertise", f"{cfg.host}:{cfg.proxy_port}",
                "--out", str(cfg.trajectory_path),
                "--intent-prefix", "", "--intent-watch", "",
            ],
            cwd=str(ANIMA_ROOT), stdout=logfh, stderr=logfh,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        if not _wait_port(cfg.host, cfg.proxy_port, 20.0):
            raise GmError("gm proxy did not open its listen port")
        try:
            auth = self._login_phase()
            self._game_phase(auth)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._closing = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proxy is not None and self.proxy.poll() is None:
            self.proxy.terminate()
            try:
                self.proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proxy.kill()
        self.proxy = None

    # -- login ------------------------------------------------------------
    def _login_phase(self) -> int:
        cfg = self.cfg
        s = socket.create_connection((cfg.host, cfg.proxy_port), timeout=10.0)
        s.sendall(build_seed(seed=0x0BADF00D))
        s.sendall(build_account_login(cfg.username, cfg.password))
        buf = _read_until(s, {0xA8, 0x82, 0x53})
        if buf[0] in (0x82, 0x53):
            s.close()
            reason = buf[1] if len(buf) > 1 else "?"
            raise GmError(f"account login denied (0x{buf[0]:02X} reason={reason})"
                          " — run python -m foundry.kernel.provision")
        s.sendall(build_server_select(0))
        buf = _read_until(s, {0x8C})
        auth = struct.unpack_from(">I", buf, 7)[0]
        s.close()
        return auth

    def _game_phase(self, auth: int) -> None:
        cfg = self.cfg
        self.watch = JsonlWatch(cfg.trajectory_path)
        self.watch.poll()  # skip login-phase records already logged
        s = socket.create_connection((cfg.host, cfg.proxy_port), timeout=10.0)
        self.sock = s
        s.sendall(build_seed(seed=auth))
        s.sendall(build_game_login(auth, cfg.username, cfg.password))
        # NB: 0xBD client-version must wait until we have a mobile — this
        # ServUO build disconnects on "Packet (0xBD) Requires State Mobile".
        self._drain = threading.Thread(target=self._drain_loop, daemon=True)
        self._drain.start()

        ev = self.watch.wait_for(
            lambda e, d: e.get("direction") == "S->C" and e.get("pid") == "0xA9", 20.0
        )
        if ev is None:
            raise GmError("no character list (0xA9) — bad GM credentials?")
        slot0_name = ev["_data"][4:34].split(b"\x00", 1)[0].decode("ascii", "replace") \
            if len(ev["_data"]) >= 34 else ""
        if slot0_name:
            s.sendall(build_play_character(slot0_name, 0))
        else:
            s.sendall(build_create_character(cfg.char_name))

        got = wait_login_confirm(cfg.trajectory_path, 30.0, watch=self.watch)
        if got is None:
            raise GmError("GM never entered the world (no 0x1B)")
        self.serial = got["serial"]
        self.pos = (got["x"], got["y"], got["z"])
        s.sendall(build_client_version())  # answer the version gate, now legal
        time.sleep(1.5)  # let the server finish the login burst
        self.say("[Self Set Hidden true")  # invisible groundskeeper
        time.sleep(0.5)

    def _drain_loop(self) -> None:
        """Discard compressed S->C bytes (we read the proxy log instead)."""
        s = self.sock
        if s is None:
            return
        try:
            s.settimeout(1.0)
            while not self._closing:
                try:
                    if not s.recv(65536):
                        break
                except socket.timeout:
                    continue
        except OSError:
            pass

    # -- actions ----------------------------------------------------------
    def say(self, text: str) -> float:
        """Speak (commands start with '['). Returns the send timestamp."""
        if self.sock is None:
            raise GmError("GM session not started")
        self.sock.sendall(build_unicode_speech(text))
        return time.time()

    def _await_cursor(self, since_ts: float, timeout_s: float = 10.0) -> int:
        ev = self.watch.wait_for(
            lambda e, d: (
                e.get("direction") == "S->C" and e.get("pid") == "0x6C"
                and float(e.get("ts", 0)) >= since_ts and len(d) >= 6
            ),
            timeout_s,
        )
        if ev is None:
            raise GmError("no target cursor (0x6C) after command")
        return struct.unpack_from(">I", ev["_data"], 2)[0]

    def command_on(self, command: str, target_serial: int) -> None:
        """Run a single-target [command, answering the cursor with a serial."""
        ts = self.say(command)
        cursor = self._await_cursor(ts)
        self.sock.sendall(build_target_response(cursor, target_serial))
        time.sleep(0.4)

    def command_at(self, command: str, x: int, y: int, z: int) -> None:
        """Run a [command, answering the cursor with a ground target.

        Used to place world objects/mobiles ([Add HeadlessOne) at a tile.
        """
        ts = self.say(command)
        cursor = self._await_cursor(ts)
        self.sock.sendall(build_ground_target_response(cursor, x, y, z))
        time.sleep(0.4)

    def command_area(self, command: str, x1: int, y1: int, x2: int, y2: int,
                     z: int) -> None:
        """Run a bounding-box [command ([WipeNPCs etc.) over a rectangle.

        ServUO's BoundingBoxPicker asks for two ground targets (corners).
        """
        ts = self.say(command)
        cursor = self._await_cursor(ts)
        self.sock.sendall(build_ground_target_response(cursor, x1, y1, z))
        cursor = self._await_cursor(time.time())
        self.sock.sendall(build_ground_target_response(cursor, x2, y2, z))
        time.sleep(0.4)

    def goto(self, x: int, y: int, z: int | None = None) -> tuple[int, int, int]:
        """[Go self to (x, y); returns the server-settled (x, y, z).

        Skips the trip when already standing there ([Go to the current spot
        sends no 0x20, which would read as a timeout).
        """
        if self.pos is not None and (self.pos[0], self.pos[1]) == (x, y):
            return self.pos
        ts = self.say(f"[Go {x} {y}" + (f" {z}" if z is not None else ""))
        ev = self.watch.wait_for(
            lambda e, d: (
                e.get("direction") == "S->C" and e.get("pid") == "0x20"
                and float(e.get("ts", 0)) >= ts and len(d) >= 19
                and struct.unpack_from(">I", d, 1)[0] == self.serial
            ),
            10.0,
        )
        if ev is None:
            raise GmError(f"[Go {x} {y} produced no position update")
        d = ev["_data"]
        self.pos = (
            struct.unpack_from(">H", d, 11)[0],
            struct.unpack_from(">H", d, 13)[0],
            struct.unpack_from(">b", d, 18)[0],
        )
        return self.pos

    # -- the fixed-start procedure ----------------------------------------
    def fixed_start(self, eval_serial: int, profile: str = "miner",
                    eval_traj: str | Path | None = None) -> dict:
        """Standardize one eval character's start state. Returns what was done.

        All commands target the eval char REMOTELY by serial (verified to work
        on ServUO for GM cursors) — the GM never walks up to the character, so
        no heavy view-update burst ever hits this fragile log path.
        """
        p = FIXED_START_PROFILES[profile]
        # 1) calibrate the workplace Z by standing there ourselves (hidden)
        gx, gy, gz = self.goto(*p["go"])
        # 1b) clear leftover NPCs from previous evals (e.g. the warrior
        #     profile's arena mobs linger and maul later non-combat evals
        #     at the shared workplace). Players are exempt from WipeNPCs.
        try:
            self.command_area("[WipeNPCs", gx - 12, gy - 12, gx + 12, gy + 12, gz)
        except GmError:
            pass  # best-effort cleanup — an empty area sends no cursor flow change
        # 2) pin skills + hand tools, targeting by serial from afar
        for skill, val in p.get("skills", {}).items():
            self.command_on(f"[Set Skills.{skill}.Base {val}", eval_serial)
        for item in p.get("items", []):
            self.command_on(f"[AddToPack {item}", eval_serial)
        # 3) shrink shard-granted starting stacks (gold) to amount 1
        neutralized: list[str] = []
        if eval_traj and p.get("neutralize"):
            for graphic in p["neutralize"]:
                serial = wait_pack_item(eval_traj, graphic, timeout_s=25.0)
                if serial is None:
                    neutralized.append(f"0x{graphic:04X}:not-found")
                    continue
                self.command_on("[Set Amount 1", serial)
                neutralized.append(f"0x{graphic:04X}:0x{serial:08X}")
        # 4) spawn standardized arena mobs around the workplace (warrior
        #    profile). Ring offsets keep them adjacent-but-not-stacked.
        spawned: list[str] = []
        offsets = [(2, 0), (-2, 0), (0, 2), (0, -2),
                   (3, 2), (-3, -2), (2, -3), (-2, 3)]
        for i, mob in enumerate(p.get("spawn_mobs", [])):
            dx, dy = offsets[i % len(offsets)]
            try:
                self.command_at(f"[Add {mob}", gx + dx, gy + dy, gz)
                spawned.append(mob)
            except GmError as e:
                spawned.append(f"{mob}:failed({e})")
        # 5) finally teleport the char to the calibrated workplace
        self.command_on(f"[Set X {gx} Y {gy} Z {gz}", eval_serial)
        return {"workplace": (gx, gy, gz), "profile": profile,
                "neutralized": neutralized, "spawned": spawned}


# --- helpers -------------------------------------------------------------------

def _wait_port(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _read_until(s: socket.socket, want_pids: set[int], timeout_s: float = 15.0) -> bytes:
    """Read the uncompressed login stream until a wanted packet arrives.

    Returns the wanted packet's bytes (framed by known login-phase lengths).
    """
    fixed = {0x82: 2, 0x8C: 11, 0x53: 2}  # deny, redirect, reject-char
    buf = b""
    s.settimeout(timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if buf:
            pid = buf[0]
            if pid in fixed:
                need = fixed[pid]
            elif pid == 0xA8:
                need = struct.unpack_from(">H", buf, 1)[0] if len(buf) >= 3 else None
            else:  # unknown login packet — drop a byte to resync (defensive)
                buf = buf[1:]
                continue
            if need is not None and len(buf) >= need:
                pkt, buf = buf[:need], buf[need:]
                if pid in want_pids:
                    return pkt
                continue
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise GmError("login connection closed by server")
        buf += chunk
    raise GmError(f"timed out waiting for login packet in {sorted(hex(p) for p in want_pids)}")


def _main(argv: list[str]) -> int:
    """Smoke test: log the GM in, optionally run a fixed start on a serial."""
    import argparse

    ap = argparse.ArgumentParser(description="Foundry GM driver smoke test")
    ap.add_argument("--serial", type=lambda v: int(v, 0), default=0,
                    help="eval char serial to fixed-start (0 = login test only)")
    ap.add_argument("--profile", default="miner")
    ap.add_argument("--proxy-port", type=int, default=2612)
    args = ap.parse_args(argv)

    cfg = GmConfig(proxy_port=args.proxy_port)
    with GmClient(cfg) as gm:
        print(f"GM in world: serial=0x{gm.serial:08X}")
        if args.serial:
            done = gm.fixed_start(args.serial, args.profile)
            print(f"fixed-start applied: {done}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
