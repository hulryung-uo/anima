"""Tests for banker balance parsing (anima.procedures.check_bank_balance).

ServUO's AccountGold banker renders cliloc 1155855 as
``String.Format("{0}\\t{1}", TotalPlat, TotalGold)`` — platinum first, the
spendable gold remainder second (see Scripts/Mobiles/NPCs/Banker.cs and
Scripts/Accounting/Account.cs).  The two numbers are independent magnitudes,
so the old ``max()`` heuristic returned the platinum count whenever it exceeded
the gold remainder.  The parser must report the amount labelled "gold".
"""

from anima.procedures.check_bank_balance import _parse_balance


def test_single_gold_form():
    # cliloc 1042759 — "Thy current bank balance is ~1~ gold."
    assert _parse_balance("Thy current bank balance is 1,234 gold.") == 1234


def test_platinum_form_takes_gold_not_max():
    # cliloc 1155855 — platinum count (50) exceeds the gold remainder (30).
    # Old max() returned 50; the spendable gold is 30.
    text = "Your bank balance is 50 platinum and 30 gold."
    assert _parse_balance(text) == 30


def test_platinum_form_large_gold():
    text = "Your bank balance is 3 platinum and 999,999 gold."
    assert _parse_balance(text) == 999999


def test_platinum_form_zero_gold_remainder():
    # Exactly N platinum, no leftover gold — gold field is 0.
    text = "Your bank balance is 7 platinum and 0 gold."
    assert _parse_balance(text) == 0


def test_secure_account_bare_amount():
    # cliloc 1155848 secure-account line — a lone integer, no "gold" label
    # in some locales; a single number is unambiguous.
    assert _parse_balance("...2,500") == 2500


def test_secure_account_with_gold_label():
    assert _parse_balance("Your secure account balance is 2,500 gold.") == 2500


def test_no_number_returns_none():
    assert _parse_balance("I will not do business with a criminal!") is None
