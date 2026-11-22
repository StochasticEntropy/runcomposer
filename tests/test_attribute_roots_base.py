"""``attribute_roots`` / ``root_mismatches`` with ``base=``: suite roots known
relative to another machine's checkout, checked against a catalog of this
machine's (DESIGN.md §6.2, the Robot toolkit). Needs no Robot Framework:
the check is plain path and name arithmetic.
"""

from __future__ import annotations

import os

import pytest

from runcomposer.sdk import Item
from runcomposer.sdk.robot import attribute_roots, root_mismatches


@pytest.fixture()
def checkout(tmp_path):
    """This machine's checkout of the shop's suites, with two roots whose
    top suites carry the same name (``Suites``), and a working directory
    elsewhere."""
    root = tmp_path / "checkout"
    for area in ("storefront", "backoffice"):
        (root / area / "suites").mkdir(parents=True)
        (root / area / "suites" / "cart.robot").write_text("*** Test Cases ***\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = os.getcwd()
    os.chdir(elsewhere)
    try:
        yield root
    finally:
        os.chdir(before)


def items_of(checkout):
    return [
        Item("Suites.Cart.Add Item", meta={"source": str(checkout / "storefront" / "suites" / "cart.robot")}),
        Item("Suites.Cart.Refund Order", meta={"source": str(checkout / "backoffice" / "suites" / "cart.robot")}),
    ]


ROOTS = ["storefront/suites", "backoffice/suites"]
IDS = ["Suites.Cart.Add Item", "Suites.Cart.Refund Order"]


class TestRootsOfACheckoutElsewhere:
    def test_without_a_base_relative_roots_are_read_from_the_working_directory(self, checkout):
        # Both roots are named "Suites", and neither lies where the working
        # directory says: every item falls back to the first root by name.
        assert attribute_roots(ROOTS, IDS, items_of(checkout)) == {
            "Suites.Cart.Add Item": "storefront/suites",
            "Suites.Cart.Refund Order": "storefront/suites",
        }

    def test_with_the_base_each_item_goes_to_the_root_its_source_lies_under(self, checkout):
        assert attribute_roots(ROOTS, IDS, items_of(checkout), base=checkout) == {
            "Suites.Cart.Add Item": "storefront/suites",
            "Suites.Cart.Refund Order": "backoffice/suites",
        }

    def test_a_single_relative_root_is_checked_against_the_base(self, checkout):
        items = items_of(checkout)
        assert root_mismatches(["storefront/suites"], IDS[:1], items, base=checkout) == []
        (problem,) = root_mismatches(["storefront/suites"], IDS[1:], items, base=checkout)
        assert "'Suites.Cart.Refund Order' comes from" in problem
        assert "not under the suite root 'storefront/suites'" in problem
        # Without the base the same single root is not found at all.
        assert root_mismatches(["storefront/suites"], IDS[:1], items) != []

    def test_an_absolute_root_is_not_moved_by_the_base(self, checkout, tmp_path):
        absolute = str(checkout / "backoffice" / "suites")
        assert attribute_roots([absolute], IDS[1:], items_of(checkout), base=tmp_path / "other") == {
            "Suites.Cart.Refund Order": absolute,
        }


# -- a Robot variable name a payload may set ------------------------------------------------


class TestVariableNames:
    """``variable_name_problem`` / ``VARIABLE_NAME_PATTERN``: what a payload
    may set as a Robot variable. ``BASE_URL:https`` would set ``${BASE_URL}``
    — Robot reads ``NAME:value`` up to the first colon."""

    @pytest.mark.parametrize("name", ["BASE_URL", "base_url", "Base Url", "SHOP-REGION", "v1.2", "_x"])
    def test_plain_names_are_accepted(self, name):
        from runcomposer.sdk.robot import variable_name_problem

        assert variable_name_problem(name) is None

    @pytest.mark.parametrize("name", ["BASE_URL:https", "A=B", "${BASE_URL}", "&{MAP}", " LEADING", "TRAILING ", "", "a\nb", 7])
    def test_anything_else_is_refused_with_why(self, name):
        from runcomposer.sdk.robot import variable_name_problem

        problem = variable_name_problem(name)
        assert problem is not None and repr(name) in problem
        if isinstance(name, str) and ":" in name:
            assert "first ':'" in problem

    def test_a_payload_refuses_such_a_name_where_it_is_made(self):
        from runcomposer.sdk.robot import Execution, ShardPayload

        with pytest.raises(ValueError, match=r"ShardPayload 'eu-01': variables: 'BASE_URL:https' is not a plain"):
            ShardPayload(shard="eu-01", out_dir="out", executions=(Execution("suites", ()),),
                         variables={"BASE_URL:https": "//attacker.shop.example"})
