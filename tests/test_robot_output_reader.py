"""The `robot-output-xml` parser reads an output.xml without Robot
Framework — a server process loads nothing of Robot to parse what its
workers produced — and reads it the way Robot does. Pinned here against
Robot's own result model, field by field, over outputs of every shape a
deployment produces: Robot 7 and legacy (Robot 6) format, suite teardowns
that fail or skip (nested), setup failures, skips, HTML messages, tasks,
merged results with re-executions, cleaned outputs, and a test without
times of its own. The suite-name rule the dispatching process uses is
pinned against Robot's own function the same way.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser, read_output

from .conftest import REPO, needs_robot
from .test_robot_pool import write_suite

pytestmark = needs_robot

CHECKOUT_INIT = "*** Settings ***\nSuite Teardown    Fail    payment gateway closed\n"
CARDS = """\
*** Settings ***
Suite Teardown    Fail    card vault closed

*** Test Cases ***
Pay By Card
    [Tags]    Checkout    Payments-Cards
    Log    paid

Card Is Declined
    [Tags]    Checkout    Payments-Cards
    Fail    *HTML* <b>declined</b> — see <a href="declined.png">the screenshot</a>

Zahlung Über Grenze
    [Tags]    Checkout
    Skip    not in this region
"""
CART = """\
*** Settings ***
Suite Teardown    Skip    cart service is in maintenance

*** Test Cases ***
Cart Holds One Item
    [Tags]    Cart
    Log    ok

Cart Rejects A Negative Quantity
    [Tags]    Cart
    Fail    quantity must be positive
"""
CATALOG = """\
*** Test Cases ***
Catalog Lists Every Product
    [Tags]    Catalog
    FOR    ${index}    IN RANGE    5
        Log    product ${index}
    END

Catalog Setup Fails
    [Tags]    Catalog
    [Setup]    Fail    catalog index missing
    Log    never reached
    [Teardown]    Log    cleaned up

Catalog Search Is Templated
    [Tags]    Catalog
    [Template]    Should Be Equal
    shoes    shoes
    hats     caps
"""
TASKS = """\
*** Tasks ***
Restock The Warehouse
    [Tags]    Catalog
    Log    restocked

Reprice The Catalog
    [Tags]    Catalog
    Fail    price feed unreachable
"""


def shop(tmp_path: Path) -> Path:
    root = tmp_path / "Shop"
    write_suite(root / "checkout", "__init__.robot", CHECKOUT_INIT)
    write_suite(root / "checkout", "cards.robot", CARDS)
    write_suite(root, "cart.robot", CART)
    write_suite(root, "catalog.robot", CATALOG)
    return root


def run_robot(source: Path, out: Path, **options) -> Path:
    import robot

    with open(out.parent / f"{out.name}.console", "w", encoding="utf-8") as console:
        robot.run(str(source), outputdir=str(out), log="NONE", report="NONE", stdout=console,
                  stderr=console, **options)
    return out / "output.xml"


def robots_reading(output: Path) -> list[tuple[str, str, str, int]]:
    from robot.api import ExecutionResult

    return [
        (test.longname, test.status, test.message, test.elapsedtime)
        for test in ExecutionResult(str(output)).suite.all_tests
    ]


def assert_same_reading(output: Path) -> list[tuple[str, str, str, int]]:
    mine = read_output(output)
    assert mine == robots_reading(output)
    return mine


class TestTheSameReadingAsRobot:
    def test_a_robot_7_output(self, tmp_path):
        tests = assert_same_reading(run_robot(shop(tmp_path), tmp_path / "out"))
        by_name = {name: (status, message) for name, status, message, _ms in tests}
        # The shapes this corpus is made for are really in it:
        assert by_name["Shop.Checkout.Cards.Pay By Card"] == (
            "FAIL", "Parent suite teardown failed:\ncard vault closed\n\n"
            "Also parent suite teardown failed:\npayment gateway closed",
        )
        assert by_name["Shop.Checkout.Cards.Zahlung Über Grenze"][0] == "SKIP"
        assert by_name["Shop.Cart.Cart Holds One Item"] == (
            "SKIP", "Skipped in parent suite teardown:\ncart service is in maintenance",
        )
        assert by_name["Shop.Catalog.Catalog Setup Fails"] == (
            "FAIL", "Setup failed:\ncatalog index missing",
        )
        assert by_name["Shop.Checkout.Cards.Card Is Declined"][1].startswith("*HTML* <b>declined</b>")

    def test_a_legacy_robot_6_output(self, tmp_path):
        output = run_robot(shop(tmp_path), tmp_path / "out", legacyoutput=True)
        assert 'starttime="' in output.read_text(encoding="utf-8")
        assert_same_reading(output)

    def test_tasks(self, tmp_path):
        root = tmp_path / "Ops"
        write_suite(root, "warehouse.robot", TASKS)
        tests = assert_same_reading(run_robot(root, tmp_path / "out"))
        assert [status for _name, status, _m, _ms in tests] == ["PASS", "FAIL"]

    def test_a_merged_result_with_a_re_execution(self, tmp_path):
        """Written by rebot: the teardown messages are already in it, and
        are not applied a second time."""
        import robot

        root = shop(tmp_path)
        first = run_robot(root, tmp_path / "first")
        again = run_robot(root, tmp_path / "again", test=["Cart Rejects A Negative Quantity"])
        with open(tmp_path / "rebot.console", "w", encoding="utf-8") as console:
            robot.rebot(str(first), str(again), merge=True, outputdir=str(tmp_path / "merged"),
                        output="output.xml", log="NONE", report="NONE", stdout=console, stderr=console)
        merged = tmp_path / "merged" / "output.xml"
        assert 'generator="Rebot' in merged.read_text(encoding="utf-8")
        assert_same_reading(merged)

    def test_a_cleaned_output(self, tmp_path):
        from runcomposer.sdk.robot import clean_output

        output = run_robot(shop(tmp_path), tmp_path / "out")
        before = read_output(output)
        clean_output(output, settings={"remove_keywords": ["passed"], "flatten_keywords": ["for"]})
        assert assert_same_reading(output) == before

    def test_a_legacy_test_without_times_of_its_own_takes_what_it_contains(self, tmp_path):
        output = tmp_path / "output.xml"
        output.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<robot generator="Robot 6.1.1 (Python 3.12 on linux)" generated="20260101 12:00:00.000" rpa="false">\n'
            '<suite id="s1" name="Shop">\n'
            '<test id="s1-t1" name="Cart Holds One Item">\n'
            '<kw name="Log" library="BuiltIn"><status status="PASS" starttime="20260101 12:00:00.000" endtime="20260101 12:00:00.250"/></kw>\n'
            '<kw name="Log" library="BuiltIn"><status status="PASS" starttime="20260101 12:00:00.250" endtime="20260101 12:00:01.000"/></kw>\n'
            '<tag>Cart</tag>\n'
            '<status status="PASS" starttime="N/A" endtime="N/A"/>\n'
            "</test>\n"
            '<status status="PASS" starttime="20260101 12:00:00.000" endtime="20260101 12:00:01.000"/>\n'
            "</suite>\n"
            "</robot>\n",
            encoding="utf-8",
        )
        assert assert_same_reading(output) == [("Shop.Cart Holds One Item", "PASS", "", 1000)]


class TestTheParser:
    def test_it_parses_with_robot_framework_unimportable(self, tmp_path):
        """In a fresh interpreter where importing ``robot`` fails: the
        parser does not need it, and reads what it reads with it."""
        output = run_robot(shop(tmp_path), tmp_path / "out")
        expected = [
            [v.native_name, v.status, v.duration_ms, v.message] for v in RobotOutputXmlParser().parse(output)
        ]
        script = (
            "import sys\n"
            "sys.modules['robot'] = None  # any import of robot now fails\n"
            "from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser\n"
            f"parsed = RobotOutputXmlParser().parse({str(output)!r})\n"
            "import json\n"
            "print(json.dumps([[v.native_name, v.status, v.duration_ms, v.message] for v in parsed]))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=str(REPO)
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == expected
        assert len(expected) == 8

    def test_a_large_output_is_read_without_its_keyword_tree(self, tmp_path):
        """What has been read is let go: the reader keeps per test what a
        verdict needs, not the elements."""
        import tracemalloc

        root = tmp_path / "Shop"
        write_suite(
            root, "catalog.robot",
            "*** Test Cases ***\n" + "".join(
                f"Catalog Lists Page {page}\n    [Tags]    Catalog\n"
                "    FOR    ${index}    IN RANGE    200\n        Log    product ${index} of the page\n    END\n\n"
                for page in range(20)
            ),
        )
        output = run_robot(root, tmp_path / "out")
        size = output.stat().st_size
        tracemalloc.start()
        tests = read_output(output)
        _now, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(tests) == 20 and size > 1_000_000
        assert peak < size / 4, (peak, size)


@pytest.mark.parametrize(
    "name",
    ["web", "my_tests", "01__checkout_flow", "UPPER_case", "mixedCase", "a__", "cart.robot",
     "Cart Suite.robot", "02__payments_by_card.robot", "x.y.robot", "storefront__"],
)
def test_the_suite_name_rule_is_robots_own(tmp_path, name):
    from robot.running import TestSuite

    from runcomposer.sdk.robot import _suite_name

    path = tmp_path / name
    if name.endswith(".robot"):
        path.write_text("*** Test Cases ***\n", encoding="utf-8")
    else:
        path.mkdir()
    assert _suite_name(path) == TestSuite.name_from_source(path)
    assert _suite_name(str(path)) == TestSuite.name_from_source(str(path))
