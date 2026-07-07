"""Cart checks of the fictional web shop — the pytest half of the
framework-agnosticism proof (DESIGN.md §6.1)."""

import pytest


def test_add_item():
    cart = ["chair"]
    assert len(cart) == 1


def test_remove_item():
    cart = ["chair", "desk"]
    cart.remove("chair")
    assert cart == ["desk"]


@pytest.mark.parametrize("method", ["visa", "wallet"])
def test_checkout(method):
    assert method in ("visa", "wallet", "invoice")


def test_total_never_negative():
    # Deliberately red: the example corpus ships one failing test.
    total = 10 - 25
    assert total >= 0, "simulated defect: negative cart total"
