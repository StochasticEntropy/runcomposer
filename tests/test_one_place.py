"""What was made to live in one place stays there — guards that fail if a
second copy, or the old way, comes back:

- correlation of native names, content hashes, the time stamp of records
  and the refusal of an XML DOCTYPE are each defined once;
- nothing outside the service reaches a private member of it;
- the store's run list filters and pages in SQL: listing one page of a
  large store builds the records of that page only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import runcomposer

PACKAGE = Path(runcomposer.__file__).resolve().parent


def definitions(name: str) -> list[str]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                found.append(str(path.relative_to(PACKAGE)))
    return found


@pytest.mark.parametrize(
    ("name", "home"),
    [
        ("correlate", "core/handoff.py"),
        ("sha256_hex", "core/hashing.py"),
        ("bundle_hash", "core/hashing.py"),
        ("verdicts_hash", "core/hashing.py"),
        ("utc_now", "core/timeutil.py"),
        ("forbid_dtd", "core/xmlsafe.py"),
    ],
)
def test_each_is_defined_once(name, home):
    assert definitions(name) == [home]


def test_the_current_time_of_a_record_is_taken_in_one_place():
    """``datetime.now(timezone.utc)`` — the time stamp records carry — is
    written in ``core/timeutil.py`` only; everything else calls ``utc_now``."""
    users = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "now"
                and any(isinstance(arg, ast.Attribute) and arg.attr == "utc" for arg in node.args)
            ):
                users.append(str(path.relative_to(PACKAGE)))
    assert users == ["core/timeutil.py"]


def test_a_doctype_is_refused_in_one_place():
    """The DOCTYPE refusal message is raised by ``forbid_dtd`` alone."""
    raising = [
        str(path.relative_to(PACKAGE))
        for path in sorted(PACKAGE.rglob("*.py"))
        if "carries a DOCTYPE" in path.read_text(encoding="utf-8")
    ]
    assert raising == ["core/xmlsafe.py"]


def test_an_exception_reaches_a_log_line_in_one_place():
    """A log record that carries an exception is printed by whatever
    formatter there is, with whatever the exception quotes. So nothing logs
    one that way: ``log_failure`` writes it as text with the configured
    secrets blanked out — no ``exc_info=`` and no ``.exception(…)`` call
    anywhere in the package."""
    carrying = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
                carrying.append(f"{path.relative_to(PACKAGE)}:{node.lineno}: .exception(…)")
            if any(keyword.arg == "exc_info" for keyword in node.keywords):
                carrying.append(f"{path.relative_to(PACKAGE)}:{node.lineno}: exc_info=")
    assert carrying == []


def test_a_failure_is_logged_as_blanked_text_whatever_the_exception_does(caplog):
    import logging

    from runcomposer.app.redaction import TRACEBACK_FOLLOWS, error_summary, log_failure, redact

    class OpaqueError(Exception):
        def __str__(self) -> str:
            raise ValueError("no text for you")

    def blank(value):
        return redact(value, {"basket-token-example"})

    def refusing(_value):
        raise RuntimeError("cannot blank")

    assert error_summary(OpaqueError()) == "OpaqueError"
    assert error_summary(RuntimeError("first\nsecond")) == "RuntimeError: first"
    assert error_summary(KeyError()) == "KeyError"
    log = logging.getLogger("runcomposer.test-one-place")
    with caplog.at_level("WARNING", logger="runcomposer"):
        try:
            try:
                raise KeyError("inner basket-token-example")
            except KeyError as inner:
                raise RuntimeError("outer basket-token-example\nmore basket-token-example") from inner
        except RuntimeError as error:
            log_failure(log, "the pool failed", error, blank)
        log_failure(log, "the pool failed again", OpaqueError(), blank, level=logging.WARNING)
        log_failure(log, "blanking failed", RuntimeError("basket-token-example"), refusing)
    first, second, third = caplog.records
    assert first.getMessage().startswith("the pool failed: RuntimeError: outer ")
    assert "Traceback" in first.getMessage() and "KeyError" in first.getMessage()
    assert second.getMessage().startswith("the pool failed again: OpaqueError") and second.levelname == "WARNING"
    assert third.getMessage().startswith("blanking failed: RuntimeError\n")
    for record in caplog.records:
        assert "basket-token-example" not in record.getMessage()
        assert record.exc_info is None and record.__dict__[TRACEBACK_FOLLOWS] is True


def test_nothing_outside_the_service_reaches_a_private_member_of_it():
    reached = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.relative_to(PACKAGE).as_posix() == "app/service.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__"):
                owner = node.value
                name = owner.id if isinstance(owner, ast.Name) else owner.attr if isinstance(owner, ast.Attribute) else ""
                if name in ("service", "_service"):
                    reached.append(f"{path.relative_to(PACKAGE)}:{node.lineno}: {name}.{node.attr}")
    assert reached == []


def test_one_page_of_a_large_store_builds_that_page_only(monkeypatch, tmp_path):
    from runcomposer.core.spec import build_spec
    from runcomposer.plugins import sqlite_store
    from runcomposer.plugins.sqlite_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    for index in range(400):
        spec = build_spec(
            title=f"Run {index}", materialized_ids=["Shop.Cart.Core.T001"], source_provider="list",
            snapshot="sha256:00", labels={"suite": "nightly" if index % 2 else "adhoc"},
            created_at=f"2026-07-{1 + index % 28:02d}T10:{index % 60:02d}:00Z",
        )
        store.create_run(spec, origin="test")
    built = []
    original = sqlite_store._run_record
    monkeypatch.setattr(sqlite_store, "_run_record", lambda *a, **k: built.append(1) or original(*a, **k))
    page = store.list_runs(limit=5, offset=10, labels={"suite": "nightly"})
    assert page.total == 200 and len(page.runs) == 5
    assert len(built) == 5, "the page's records only — filtering and paging happen in SQL"
    store.close()
