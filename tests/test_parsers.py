"""Result parsers (DESIGN.md §5) and the defused XML reader they share.

One error for bad input — ``ParseError`` from the SDK — whichever parser
meets it, and a DTD refused by the parser itself, wherever in
the document it sits and in whatever encoding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import runcomposer.plugins.junit_parser as junit_module
import runcomposer.plugins.robot_output_parser as robot_module
import runcomposer.plugins.verdicts_parser as verdicts_module
from runcomposer.plugins.junit_parser import JunitXmlParser
from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser
from runcomposer.plugins.verdicts_parser import VerdictsParser
from runcomposer.sdk import (
    REPORTABLE_STATUSES,
    ParsedVerdict,
    ParseError,
    RunComposerError,
    forbid_dtd,
    parse_xml,
)

from .conftest import HAS_ROBOT, ROBOT_SHOP, needs_robot

JUNIT_SAMPLE = """<?xml version="1.0"?>
<testsuite name="s" tests="4">
  <testcase classname="test_cart" name="test_add_item" time="0.12"/>
  <testcase classname="test_cart" name="test_broken" time="0.5">
    <failure message="assert failed">trace</failure>
  </testcase>
  <testcase classname="test_cart" name="test_error" time="0.1">
    <error message="boom"/>
  </testcase>
  <testcase name="orphan_no_classname" time="0">
    <skipped message="not today"/>
  </testcase>
</testsuite>
"""


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


# -- the defused XML reader ---------------------------------------------------------


class TestDefusedXml:
    def test_a_plain_document_is_read(self, tmp_path):
        file = write(tmp_path / "ok.xml", '<?xml version="1.0"?><shop><cart items="2"/></shop>')
        forbid_dtd(file)  # does not raise
        root = parse_xml(file)
        assert root.tag == "shop" and root.find("cart").get("items") == "2"

    def test_accepts_str_and_path(self, tmp_path):
        file = write(tmp_path / "ok.xml", "<shop/>")
        assert parse_xml(str(file)).tag == "shop"

    def test_a_doctype_is_refused(self, tmp_path):
        file = write(tmp_path / "x.xml", '<!DOCTYPE shop SYSTEM "shop.dtd"><shop/>')
        with pytest.raises(ParseError, match=r"DOCTYPE declaration — refused \(defused parsing"):
            forbid_dtd(file)
        with pytest.raises(ParseError, match="DOCTYPE"):
            parse_xml(file)

    def test_an_external_entity_never_reads_the_file_it_points_at(self, tmp_path):
        secret = write(tmp_path / "secret.txt", "card-number-4111")
        file = write(
            tmp_path / "xxe.xml",
            f'<!DOCTYPE shop [<!ENTITY card SYSTEM "file://{secret}">]><shop>&card;</shop>',
        )
        with pytest.raises(ParseError) as caught:
            parse_xml(file)
        assert "card-number-4111" not in str(caught.value)

    def test_an_entity_bomb_is_refused_before_anything_expands(self, tmp_path):
        levels = "".join(
            f'<!ENTITY {name} "{f"&{previous};" * 10}">'
            for previous, name in zip("abcdefg", "bcdefgh", strict=True)
        )
        bomb = f'<!DOCTYPE shop [<!ENTITY a "aaaaaaaaaa">{levels}]><shop>&h;</shop>'
        with pytest.raises(ParseError, match="DOCTYPE"):
            forbid_dtd(write(tmp_path / "bomb.xml", bomb))

    def test_a_doctype_behind_100_kib_of_padding_is_refused(self, tmp_path):
        """Regression: a scan of the first 64 KiB walks straight past it."""
        padding = "<!-- " + "padding " * 13_000 + "-->\n"
        assert len(padding.encode()) > 100 * 1024
        file = write(
            tmp_path / "padded.xml",
            '<?xml version="1.0"?>\n' + padding
            + '<!DOCTYPE testsuite [<!ENTITY e "expanded">]>\n'
            + '<testsuite><testcase name="&e;"/></testsuite>',
        )
        assert file.read_bytes().index(b"<!DOCTYPE") > 100 * 1024
        with pytest.raises(ParseError, match="DOCTYPE"):
            forbid_dtd(file)

    def test_a_utf_16_document_with_an_entity_is_refused(self, tmp_path):
        """Regression: in a two-byte encoding the ASCII bytes ``<!DOCTYPE``
        do not occur at all."""
        text = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE testsuite [<!ENTITY e "expanded">]>'
            '<testsuite><testcase name="&e;"/></testsuite>'
        )
        file = tmp_path / "utf16.xml"
        file.write_bytes(text.encode("utf-16"))
        assert b"<!DOCTYPE" not in file.read_bytes() and b"<!ENTITY" not in file.read_bytes()
        with pytest.raises(ParseError, match="DOCTYPE"):
            forbid_dtd(file)
        with pytest.raises(ParseError):
            JunitXmlParser().parse(file)

    def test_a_utf_16_document_without_a_dtd_is_read(self, tmp_path):
        file = tmp_path / "utf16.xml"
        file.write_bytes('<?xml version="1.0" encoding="UTF-16"?><shop name="Zahlungen prüfen"/>'.encode("utf-16"))
        assert parse_xml(file).get("name") == "Zahlungen prüfen"

    def test_the_words_alone_are_not_a_dtd(self, tmp_path):
        """The parser decides, not a byte scan: text that merely *mentions* a
        DOCTYPE — in a comment, in CDATA, in a failure message — is content."""
        file = write(
            tmp_path / "mention.xml",
            "<testsuite><!-- <!DOCTYPE not one --><testcase name='t'>"
            "<failure message='saw &lt;!DOCTYPE in the page'><![CDATA[<!ENTITY x 'y'>]]></failure>"
            "</testcase></testsuite>",
        )
        forbid_dtd(file)
        (case,) = JunitXmlParser().parse(file)
        assert case.status == "FAIL" and case.message == "saw <!DOCTYPE in the page"

    @pytest.mark.parametrize("text", ["<shop><unclosed>", "not xml at all", "", "<a></b>"])
    def test_malformed_xml_is_a_parse_error(self, tmp_path, text):
        file = write(tmp_path / "bad.xml", text)
        with pytest.raises(ParseError, match="not well-formed XML"):
            forbid_dtd(file)
        with pytest.raises(ParseError, match="not well-formed XML"):
            parse_xml(file)

    def test_an_unreadable_file_is_a_parse_error(self, tmp_path):
        with pytest.raises(ParseError, match="cannot read"):
            forbid_dtd(tmp_path / "missing.xml")

    def test_the_error_names_the_file(self, tmp_path):
        file = write(tmp_path / "results" / "junit.xml", "<!DOCTYPE x><x/>")
        with pytest.raises(ParseError, match=r"junit\.xml"):
            forbid_dtd(file)


# -- one ParseError -------------------------------------------------------------------


class TestOneParseError:
    """Regression: three parsers used to raise three separate ParseError
    classes, and ingestion caught none of them."""

    def test_no_parser_module_defines_its_own(self):
        for module in (verdicts_module, junit_module, robot_module):
            assert module.ParseError is ParseError, module.__name__
            assert "ParseError" not in getattr(module, "__all__", ())

    def test_it_is_part_of_the_error_taxonomy(self):
        assert issubclass(ParseError, RunComposerError) and issubclass(ParseError, ValueError)
        assert ParseError.code == "parse-error"

    def test_every_parser_raises_it_for_bad_input(self, tmp_path):
        bad_verdicts = write(
            tmp_path / "results.json", json.dumps({"format": "runcomposer-verdicts", "verdicts": 5})
        )
        bad_junit = write(tmp_path / "junit.xml", "<robot><test/></robot>")
        raised = []
        for parser, file in ((VerdictsParser(), bad_verdicts), (JunitXmlParser(), bad_junit)):
            with pytest.raises(ParseError) as caught:
                parser.parse(file)
            raised.append(type(caught.value))
        if HAS_ROBOT:
            with pytest.raises(ParseError) as caught:
                RobotOutputXmlParser().parse(write(tmp_path / "output.xml", "<robot><unclosed>"))
            raised.append(type(caught.value))
        assert set(raised) == {ParseError}

    def test_parsers_declare_the_format_id_they_are_registered_under(self):
        assert VerdictsParser.format_id == "runcomposer-verdicts"
        assert JunitXmlParser.format_id == "junit-xml"
        assert RobotOutputXmlParser.format_id == "robot-output-xml"


# -- runcomposer-verdicts ----------------------------------------------------------------


def verdicts_doc(*verdicts: dict) -> str:
    return json.dumps({"format": "runcomposer-verdicts", "verdicts": list(verdicts)})


class TestVerdictsParser:
    def test_a_results_file(self, tmp_path):
        file = write(
            tmp_path / "results.json",
            verdicts_doc(
                {"name": "Shop.Payments.Cards.T001", "status": "PASS", "duration_ms": 120},
                {"name": "Shop.Payments.Cards.T002", "status": "FAIL", "duration_ms": 80,
                 "message": "card declined", "attempt": 2},
                {"name": "Shop.Payments.Cards.T003", "status": "SKIP"},
            ),
        )
        assert VerdictsParser().parse(file) == [
            ParsedVerdict("Shop.Payments.Cards.T001", "PASS", 120),
            ParsedVerdict("Shop.Payments.Cards.T002", "FAIL", 80, "card declined", 2),
            ParsedVerdict("Shop.Payments.Cards.T003", "SKIP", 0, "", 1),
        ]

    def test_the_native_name_is_kept_exactly_as_spelled(self, tmp_path):
        """Correlation is the TestSource's job — the parser normalizes nothing."""
        name = "  tests/test_cart.py::test_checkout[visa]  "
        file = write(tmp_path / "r.json", verdicts_doc({"name": name, "status": "PASS"}))
        assert VerdictsParser().parse(file)[0].native_name == name

    def test_a_bundle_directory_reads_every_document_of_the_format(self, tmp_path):
        bundle = tmp_path / "bundle"
        write(bundle / "b.json", verdicts_doc({"name": "Shop.Cart.Core.T002", "status": "PASS"}))
        write(bundle / "a.json", verdicts_doc({"name": "Shop.Cart.Core.T001", "status": "PASS"}))
        write(bundle / "nested" / "c.json", verdicts_doc({"name": "Shop.Cart.Core.T003", "status": "ERROR"}))
        write(bundle / "runcomposer_run.json", json.dumps({"run_id": "01X"}))  # the marker: not results
        write(bundle / "notes.json", "{ this is not json")
        write(bundle / "other.json", json.dumps(["a", "list"]))
        write(bundle / "log.txt", "not looked at")
        parsed = VerdictsParser().parse(bundle)
        assert [p.native_name for p in parsed] == [
            "Shop.Cart.Core.T001", "Shop.Cart.Core.T002", "Shop.Cart.Core.T003",
        ]

    def test_a_bundle_with_nothing_of_the_format_parses_to_nothing(self, tmp_path):
        bundle = tmp_path / "bundle"
        write(bundle / "junit.xml", JUNIT_SAMPLE)
        assert VerdictsParser().parse(bundle) == []

    def test_every_reportable_status_is_accepted(self, tmp_path):
        file = write(
            tmp_path / "r.json",
            verdicts_doc(*[{"name": f"t{i}", "status": s} for i, s in enumerate(REPORTABLE_STATUSES)]),
        )
        assert [p.status for p in VerdictsParser().parse(file)] == list(REPORTABLE_STATUSES)

    def test_missing_is_derived_never_reported(self, tmp_path):
        """An executor cannot claim MISSING: it is what runcomposer says about
        an item that did *not* come back."""
        file = write(tmp_path / "r.json", verdicts_doc({"name": "Shop.Cart.Core.T001", "status": "MISSING"}))
        with pytest.raises(ParseError, match=r"'status' must be one of .*got 'MISSING'"):
            VerdictsParser().parse(file)

    @pytest.mark.parametrize(
        ("document", "fragment"),
        [
            ({"format": "runcomposer-verdicts"}, "'verdicts' must be a list"),
            ({"format": "runcomposer-verdicts", "verdicts": {"a": 1}}, "'verdicts' must be a list"),
            ({"format": "runcomposer-verdicts", "verdicts": ["Shop.Cart.Core.T001"]},
             r"verdicts\[0\]: must be an object"),
            ({"format": "runcomposer-verdicts", "verdicts": [{"status": "PASS"}]},
             r"verdicts\[0\]: 'name' is required"),
            ({"format": "runcomposer-verdicts", "verdicts": [{"name": "", "status": "PASS"}]},
             "'name' is required"),
            ({"format": "runcomposer-verdicts", "verdicts": [{"name": 7, "status": "PASS"}]},
             "'name' is required"),
            ({"format": "runcomposer-verdicts", "verdicts": [{"name": "t"}]}, "'status' must be one of"),
            ({"format": "runcomposer-verdicts", "verdicts": [{"name": "t", "status": "passed"}]},
             "got 'passed'"),
            ({"format": "runcomposer-verdicts",
              "verdicts": [{"name": "t", "status": "PASS"}, {"name": "u", "status": "PASS",
                                                             "duration_ms": "fast"}]},
             r"verdicts\[1\]: 'duration_ms' and 'attempt' must be integers"),
            ({"format": "runcomposer-verdicts",
              "verdicts": [{"name": "t", "status": "PASS", "attempt": None}]},
             "must be integers"),
        ],
    )
    def test_a_malformed_document_is_a_parse_error_naming_the_place(self, tmp_path, document, fragment):
        file = write(tmp_path / "results.json", json.dumps(document))
        with pytest.raises(ParseError, match=fragment) as caught:
            VerdictsParser().parse(file)
        assert "results.json" in str(caught.value)

    def test_one_malformed_document_fails_the_whole_bundle(self, tmp_path):
        bundle = tmp_path / "bundle"
        write(bundle / "a.json", verdicts_doc({"name": "Shop.Cart.Core.T001", "status": "PASS"}))
        write(bundle / "b.json", json.dumps({"format": "runcomposer-verdicts", "verdicts": "x"}))
        with pytest.raises(ParseError, match=r"b\.json"):
            VerdictsParser().parse(bundle)

    def test_non_ascii_names_and_messages_survive(self, tmp_path):
        file = write(
            tmp_path / "r.json",
            json.dumps({"format": "runcomposer-verdicts",
                        "verdicts": [{"name": "Kasse › Zahlung prüfen", "status": "FAIL",
                                      "message": "Betrag ≠ 9,99 €"}]}, ensure_ascii=False),
        )
        (parsed,) = VerdictsParser().parse(file)
        assert (parsed.native_name, parsed.message) == ("Kasse › Zahlung prüfen", "Betrag ≠ 9,99 €")


# -- junit-xml ------------------------------------------------------------------------------


class TestJunitParser:
    def test_statuses_names_durations(self, tmp_path):
        file = write(tmp_path / "junit.xml", JUNIT_SAMPLE)
        parsed = {p.native_name: p for p in JunitXmlParser().parse(file)}
        assert parsed["test_cart.test_add_item"].status == "PASS"
        assert parsed["test_cart.test_add_item"].duration_ms == 120
        assert parsed["test_cart.test_broken"].status == "FAIL"
        assert parsed["test_cart.test_broken"].message == "assert failed"
        assert parsed["test_cart.test_error"].status == "ERROR"
        assert parsed["test_cart.test_error"].message == "boom"
        assert parsed["orphan_no_classname"].status == "SKIP"
        assert parsed["orphan_no_classname"].message == "not today"
        assert len(parsed) == 4

    def test_the_native_name_is_classname_dot_name_as_the_artifact_spells_it(self, tmp_path):
        file = write(
            tmp_path / "junit.xml",
            '<testsuite><testcase classname="tests.test_cart" name="test_checkout[visa]"/></testsuite>',
        )
        assert JunitXmlParser().parse(file)[0].native_name == "tests.test_cart.test_checkout[visa]"

    def test_a_testsuites_wrapper_is_read_through(self, tmp_path):
        file = write(
            tmp_path / "junit.xml",
            '<testsuites><testsuite name="a"><testcase classname="c" name="one"/></testsuite>'
            '<testsuite name="b"><testcase classname="c" name="two"/></testsuite></testsuites>',
        )
        assert [p.native_name for p in JunitXmlParser().parse(file)] == ["c.one", "c.two"]

    def test_the_failure_text_stands_in_for_a_missing_message(self, tmp_path):
        file = write(
            tmp_path / "junit.xml",
            "<testsuite><testcase name='t'><failure>\n  AssertionError: total was 0\n</failure>"
            "</testcase></testsuite>",
        )
        assert JunitXmlParser().parse(file)[0].message == "AssertionError: total was 0"

    def test_a_message_is_bounded(self, tmp_path):
        file = write(
            tmp_path / "junit.xml",
            f"<testsuite><testcase name='t'><failure message='{'x' * 2000}'/></testcase></testsuite>",
        )
        assert len(JunitXmlParser().parse(file)[0].message) == 500

    def test_an_error_outranks_a_failure_on_one_case(self, tmp_path):
        file = write(
            tmp_path / "junit.xml",
            "<testsuite><testcase name='t'><failure message='f'/><error message='e'/></testcase>"
            "</testsuite>",
        )
        (case,) = JunitXmlParser().parse(file)
        assert (case.status, case.message) == ("ERROR", "e")

    def test_a_bundle_directory_reads_the_files_matching_the_pattern(self, tmp_path):
        bundle = tmp_path / "bundle"
        write(bundle / "junit.xml", JUNIT_SAMPLE)
        write(bundle / "shard-2" / "junit-2.xml",
              '<testsuite><testcase classname="test_auth" name="test_login"/></testsuite>')
        write(bundle / "coverage.xml", "<coverage/>")  # not matched: never read as results
        names = [p.native_name for p in JunitXmlParser().parse(bundle)]
        assert len(names) == 5 and "test_auth.test_login" in names

    def test_the_pattern_is_configurable(self, tmp_path):
        """What a `parsers: {junit-xml: {pattern: …}}` entry sets."""
        bundle = tmp_path / "bundle"
        write(bundle / "TEST-checkout.xml",
              '<testsuite><testcase classname="CheckoutTest" name="paysByCard"/></testsuite>')
        assert JunitXmlParser().parse(bundle) == []  # the default looks for junit*.xml
        parsed = JunitXmlParser(pattern="TEST-*.xml").parse(bundle)
        assert [p.native_name for p in parsed] == ["CheckoutTest.paysByCard"]

    def test_a_single_file_is_read_whatever_it_is_called(self, tmp_path):
        file = write(tmp_path / "results.xml", JUNIT_SAMPLE)
        assert len(JunitXmlParser().parse(file)) == 4

    @pytest.mark.parametrize("payload", ["<!DOCTYPE", "<!ENTITY"])
    def test_doctype_entity_refused(self, tmp_path, payload):
        body = JUNIT_SAMPLE.split("\n", 1)[1]  # without the XML declaration
        file = write(
            tmp_path / "junit.xml",
            f'<!DOCTYPE x [ <!ENTITY e SYSTEM "file:///etc/hostname"> ]>\n{body}'
            if payload == "<!DOCTYPE"
            else f'<!ENTITY e "y">\n{body}',
        )
        # A DOCTYPE is refused as such; a bare entity declaration is not even XML.
        expected = r"DOCTYPE declaration — refused \(defused" if payload == "<!DOCTYPE" else "not well-formed"
        with pytest.raises(ParseError, match=expected):
            JunitXmlParser().parse(file)

    def test_malformed_and_wrong_root_refused(self, tmp_path):
        with pytest.raises(ParseError, match="not well-formed"):
            JunitXmlParser().parse(write(tmp_path / "junit.xml", "<testsuite><unclosed>"))
        with pytest.raises(ParseError, match="not a JUnit"):
            JunitXmlParser().parse(write(tmp_path / "junit2.xml", "<robot><test/></robot>"))

    def test_a_case_without_a_name_or_with_a_bad_time_is_a_parse_error(self, tmp_path):
        with pytest.raises(ParseError, match="without a name"):
            JunitXmlParser().parse(write(tmp_path / "a.xml", "<testsuite><testcase classname='c'/></testsuite>"))
        with pytest.raises(ParseError, match="non-numeric time"):
            JunitXmlParser().parse(
                write(tmp_path / "b.xml", "<testsuite><testcase name='t' time='fast'/></testsuite>")
            )

    def test_the_shipped_pytest_example_aliases_these_names_onto_its_ids(self):
        """The id-space rule end to end (DESIGN.md §2): the parser reports
        ``classname.name``; the manifest's aliases map that onto nodeids."""
        from runcomposer.plugins.manifest_source import ManifestSource

        manifest = ManifestSource(ROBOT_SHOP.parent.parent / "pytest-shop" / "manifest.json")
        items = manifest.items()
        assert items and all("::" in item.id for item in items)
        raw = json.loads((ROBOT_SHOP.parent.parent / "pytest-shop" / "manifest.json").read_text())
        for entry in raw["items"]:
            for alias in entry.get("aliases", ()):
                assert manifest.resolve(alias) == entry["id"]


# -- robot-output-xml -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_output(tmp_path_factory) -> Path:
    """One real Robot execution shared by the parser tests."""
    import robot

    out_dir = tmp_path_factory.mktemp("robot-out")
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        robot.run(
            str(ROBOT_SHOP),
            test=["Tests.Payments.Visa Payment Succeeds",
                  "Tests.Payments.Expired Card Is Rejected Loudly"],
            outputdir=str(out_dir),
            output="output.xml",
            log="NONE",
            report="NONE",
            stdout=devnull,
            stderr=devnull,
        )
    return out_dir / "output.xml"


@needs_robot
class TestRobotOutputParser:
    def test_parses_longnames_statuses_durations(self, sample_output):
        parsed = {p.native_name: p for p in RobotOutputXmlParser().parse(sample_output)}
        assert set(parsed) == {
            "Tests.Payments.Visa Payment Succeeds",
            "Tests.Payments.Expired Card Is Rejected Loudly",
        }
        assert parsed["Tests.Payments.Visa Payment Succeeds"].status == "PASS"
        failing = parsed["Tests.Payments.Expired Card Is Rejected Loudly"]
        assert failing.status == "FAIL"
        assert "expired card" in failing.message
        assert all(p.duration_ms >= 0 and p.attempt == 1 for p in parsed.values())

    def test_a_bundle_directory_reads_every_output_file(self, sample_output, tmp_path):
        bundle = tmp_path / "bundle"
        write(bundle / "env1" / "output.xml", sample_output.read_text(encoding="utf-8"))
        write(bundle / "env2" / "output-2.xml", sample_output.read_text(encoding="utf-8"))
        write(bundle / "log.html", "<html/>")
        assert len(RobotOutputXmlParser().parse(bundle)) == 4

    def test_the_pattern_is_configurable(self, sample_output, tmp_path):
        bundle = tmp_path / "bundle"
        write(bundle / "results.xml", sample_output.read_text(encoding="utf-8"))
        assert RobotOutputXmlParser().parse(bundle) == []
        assert len(RobotOutputXmlParser(pattern="results*.xml").parse(bundle)) == 2

    @pytest.mark.parametrize("payload", ["<!DOCTYPE", "<!ENTITY"])
    def test_doctype_and_entity_declarations_are_refused(self, tmp_path, sample_output, payload):
        original = sample_output.read_text(encoding="utf-8")
        evil = (
            original.replace(
                "<robot", '<!DOCTYPE robot [ <!ENTITY xxe SYSTEM "file:///etc/hostname"> ]>\n<robot', 1
            )
            if payload == "<!DOCTYPE"
            else original.replace("<robot", '<!ENTITY x "y">\n<robot', 1)
        )
        malicious = write(tmp_path / "output.xml", evil)
        expected = r"DOCTYPE declaration — refused \(defused" if payload == "<!DOCTYPE" else "not well-formed"
        with pytest.raises(ParseError, match=expected):
            RobotOutputXmlParser().parse(malicious)

    def test_a_dtd_deep_in_a_real_output_is_refused(self, tmp_path, sample_output):
        """Regression, on the real thing: the declaration sits after 100 KiB."""
        original = sample_output.read_text(encoding="utf-8")
        padding = "<!-- " + "padding " * 13_000 + "-->\n"
        evil = original.replace(
            "<robot", padding + '<!DOCTYPE robot [<!ENTITY e "x">]>\n<robot', 1
        )
        with pytest.raises(ParseError, match="DOCTYPE"):
            RobotOutputXmlParser().parse(write(tmp_path / "output.xml", evil))

    def test_garbage_is_a_parse_error(self, tmp_path):
        with pytest.raises(ParseError, match="not well-formed"):
            RobotOutputXmlParser().parse(write(tmp_path / "output.xml", "<robot><unclosed>"))

    def test_well_formed_xml_that_is_not_robot_output_is_a_parse_error(self, tmp_path):
        with pytest.raises(ParseError, match=r"not a parsable output\.xml"):
            RobotOutputXmlParser().parse(write(tmp_path / "output.xml", JUNIT_SAMPLE))


@pytest.mark.skipif(HAS_ROBOT, reason="only meaningful where Robot Framework is absent")
def test_without_robot_the_parser_says_what_to_install(tmp_path):
    """The extra is a plugin dependency: without it the parser is a clean
    ParseError naming the extra, not an ImportError out of ingestion."""
    file = write(tmp_path / "output.xml", "<robot/>")
    with pytest.raises(ParseError, match=r"runcomposer\[robot\]"):
        RobotOutputXmlParser().parse(file)
