"""GM account provisioning — a deliberate human act (FOUNDRY.md §2).

Inserts (or elevates) the ``foundrygm`` account in the local ServUO
``Saves/Accounts/accounts.xml`` with GameMaster access. ServUO loads this file
at boot and rewrites it on every world save, so the edit is only safe while
the server is STOPPED — this tool refuses to run against a live shard.

Password hash format (verified against Scripts/Accounting/Account.cs):
    newCryptPassword = SHA1(username + password) as dash-separated upper hex.

Usage:
    python3 -m foundry.kernel.provision            # dry-run: report state
    python3 -m foundry.kernel.provision --apply    # edit accounts.xml
"""

from __future__ import annotations

import hashlib
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from foundry.kernel.gm import GM_PASS, GM_USER

SERVUO_ROOT = Path.home() / "dev" / "uo" / "servuo"
GM_LEVELS = {"GameMaster", "Seer", "Administrator", "Developer", "Owner"}


def hash_password(username: str, password: str) -> str:
    digest = hashlib.sha1((username + password).encode("ascii")).digest()
    return "-".join(f"{b:02X}" for b in digest)


def server_is_up(host: str = "127.0.0.1", port: int = 2594) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def accounts_path(servuo_root: Path = SERVUO_ROOT) -> Path:
    return servuo_root / "Saves" / "Accounts" / "accounts.xml"


def check(servuo_root: Path = SERVUO_ROOT) -> str:
    """'ok' | 'needs-elevation' | 'missing' | 'no-file'"""
    p = accounts_path(servuo_root)
    if not p.exists():
        return "no-file"
    root = ET.parse(p).getroot()
    for acct in root.findall("account"):
        if (acct.findtext("username") or "").lower() == GM_USER:
            level = acct.findtext("accessLevel") or "Player"
            return "ok" if level in GM_LEVELS else "needs-elevation"
    return "missing"


def apply(servuo_root: Path = SERVUO_ROOT) -> str:
    p = accounts_path(servuo_root)
    tree = ET.parse(p)
    root = tree.getroot()

    target = None
    for acct in root.findall("account"):
        if (acct.findtext("username") or "").lower() == GM_USER:
            target = acct
            break

    if target is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        target = ET.SubElement(root, "account")
        for tag, text in (
            ("username", GM_USER),
            ("newCryptPassword", hash_password(GM_USER, GM_PASS)),
            ("accessLevel", "GameMaster"),
            ("created", now),
            ("lastLogin", now),
            ("totalGameTime", "PT0S"),
        ):
            ET.SubElement(target, tag).text = text
        ET.SubElement(target, "chars")
        ET.SubElement(target, "totalCurrency").text = "0"
        ET.SubElement(target, "sovereigns").text = "0"
        action = "created"
    else:
        level = target.find("accessLevel")
        if level is None:
            level = ET.SubElement(target, "accessLevel")
        level.text = "GameMaster"
        pw = target.find("newCryptPassword")
        if pw is not None:
            pw.text = hash_password(GM_USER, GM_PASS)
        action = "elevated"

    root.set("count", str(len(root.findall("account"))))
    ET.indent(tree, space="\t")
    tree.write(p, encoding="utf-8", xml_declaration=True)
    return action


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Provision the foundrygm GameMaster account")
    ap.add_argument("--servuo", type=Path, default=SERVUO_ROOT)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    state = check(args.servuo)
    print(f"account '{GM_USER}': {state}  ({accounts_path(args.servuo)})")
    if state == "ok" or not args.apply:
        return 0 if state == "ok" else 1
    if state == "no-file":
        print("accounts.xml not found — boot the server once first")
        return 2
    if server_is_up():
        print("REFUSING: ServUO is running on :2594 — stop it first "
              "(it rewrites accounts.xml on every world save)")
        return 3
    print(f"{apply(args.servuo)} '{GM_USER}' with GameMaster access — restart the server")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
