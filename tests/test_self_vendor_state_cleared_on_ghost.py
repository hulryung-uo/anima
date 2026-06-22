"""Cached vendor trading state must reset when our body flips to a ghost (death).

``vendor_serial`` / ``vendor_buy_list`` / ``vendor_sell_list`` are populated by
the 0x74 / 0x9E handlers and otherwise only cleared by a matching 0x1D Delete for
the vendor or an explicit 0xBF vendor-close. A vendor mobile stays live
server-side across our death (no 0x1D), and death is not a vendor *close*, so a
vendor list open at the moment of death would survive the ghost period and the
resurrect. ``_wait_for_buy_list`` / ``_wait_for_sell_list`` treat a non-empty
list as "ready", so a freshly-resurrected agent would fire build_buy_items /
build_sell_items at the stale serial. The ghost body flip must clear all three,
mirroring the poison / open_container / context_menu clears.
"""

from anima.perception.self_state import SelfState, VendorBuyItem, VendorSellItem

SELF = 0x00000001
LIVING_BODY = 0x0190
GHOST_BODY = 0x0192
VENDOR = 0x40000099


def _open_vendor(ss: SelfState) -> None:
    ss.vendor_serial = VENDOR
    ss.vendor_buy_list = [
        VendorBuyItem(serial=0x4001, graphic=0x0F7A, amount=5, price=3, name="garlic")
    ]
    ss.vendor_sell_list = [
        VendorSellItem(serial=0x4002, graphic=0x13B9, amount=1, price=20, name="sword")
    ]


def test_ghost_body_clears_vendor_state():
    ss = SelfState(serial=SELF)
    ss.set_body(LIVING_BODY)
    _open_vendor(ss)
    assert ss.vendor_serial == VENDOR
    assert ss.vendor_buy_list
    assert ss.vendor_sell_list

    # We die: body flips to a ghost (no paired 0x1D Delete / vendor-close).
    ss.set_body(GHOST_BODY)
    assert ss.is_ghost is True
    assert ss.vendor_serial == 0
    assert ss.vendor_buy_list == []
    assert ss.vendor_sell_list == []


def test_vendor_state_stays_clear_through_resurrect():
    ss = SelfState(serial=SELF)
    ss.set_body(LIVING_BODY)
    _open_vendor(ss)

    ss.set_body(GHOST_BODY)
    assert ss.vendor_serial == 0

    # Resurrect: body flips back to a living body (no fresh 0x74/0x9E).
    ss.set_body(LIVING_BODY)
    assert ss.is_ghost is False
    assert ss.vendor_serial == 0
    assert ss.vendor_buy_list == []
    assert ss.vendor_sell_list == []


def test_living_to_living_body_change_keeps_vendor_state():
    """A non-death body change (polymorph/mount) must not wipe an open vendor."""
    ss = SelfState(serial=SELF)
    ss.set_body(LIVING_BODY)
    _open_vendor(ss)

    # Some other living body update arrives — the open vendor must persist.
    ss.set_body(0x0191)
    assert ss.is_ghost is False
    assert ss.vendor_serial == VENDOR
    assert ss.vendor_buy_list
    assert ss.vendor_sell_list


def test_first_body_set_to_living_does_not_treat_as_death():
    """The initial 0->living body set is not a death transition."""
    ss = SelfState(serial=SELF)
    _open_vendor(ss)
    ss.set_body(LIVING_BODY)
    assert ss.vendor_serial == VENDOR
    assert ss.vendor_buy_list
    assert ss.vendor_sell_list
