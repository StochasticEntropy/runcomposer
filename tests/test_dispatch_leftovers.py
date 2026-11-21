"""A dispatch that leaves no record leaves no files (DESIGN.md §4, §6.2).

A runner refused before the hand-off — by ``ctx.refuse``, an
``InvalidParameter``, an exception, or by returning without declaring —
leaves no dispatch behind; the run itself may be kept. Whatever directory
that attempt created for ``ctx.workdir`` and ``ctx.private_dir`` goes with
it, and nothing an earlier dispatch of the run made is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runcomposer.sdk import DispatchContext, InvalidParameter, Parameter, RunnerBase, RunnerInfo, Verdict

from .conftest import compose

HERE = "tests.test_dispatch_leftovers"


class PreparingRunner(RunnerBase):
    """Writes into both directories, then ends as ``how`` says."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="preparing", parameters=(
            Parameter("how", kind="choice", default="deliver",
                      choices=("deliver", "refuse", "invalid", "raise", "return")),))

    def dispatch(self, ctx: DispatchContext) -> None:
        (ctx.workdir / "plan.txt").write_text("two suites", encoding="utf-8")
        (ctx.private_dir / "session.env").write_text("SHOP_SESSION=abc123", encoding="utf-8")
        how = ctx.parameters["how"]
        if how == "refuse":
            ctx.refuse("the staging shop is down for maintenance")
        if how == "invalid":
            raise InvalidParameter("how", "not on a weekend")
        if how == "raise":
            raise RuntimeError("the staging shop answered 502")
        if how == "return":
            return
        ctx.declare(["1"])
        ctx.deliver("1", verdicts=[Verdict(item, "PASS") for item in ctx.item_ids])


def files_under(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")) if root.exists() else []


@pytest.fixture()
def preparing(make_service):
    return make_service(runners={"preparing": {"module": f"{HERE}:PreparingRunner"}})


@pytest.mark.parametrize("how", ["refuse", "invalid", "raise", "return"])
def test_a_refused_attempt_leaves_nothing_under_the_work_and_private_directories(preparing, how):
    service = preparing
    run_id = compose(service, "Checkout")
    try:
        outcome = service.dispatch(run_id, "preparing", {"how": how}, foreground=True)
    except InvalidParameter:
        assert how == "invalid"
    else:
        assert outcome.refusal is not None
    assert service.require_run(run_id).dispatches == ()
    assert files_under(service.config.artifact_dir) == []
    assert files_under(service.config.private_dir) == []


def test_an_earlier_dispatch_keeps_its_files_and_a_successful_one_is_unaffected(preparing):
    service = preparing
    run_id = compose(service, "Checkout")
    service.dispatch(run_id, "preparing", {"how": "deliver"}, foreground=True)
    (first,) = service.require_run(run_id).dispatches
    kept = files_under(service.config.artifact_dir), files_under(service.config.private_dir)
    assert kept == (
        [run_id, f"{run_id}/{first.dispatch_id}", f"{run_id}/{first.dispatch_id}/plan.txt"],
        [run_id, f"{run_id}/{first.dispatch_id}", f"{run_id}/{first.dispatch_id}/session.env"],
    )
    outcome = service.dispatch(run_id, "preparing", {"how": "refuse"}, foreground=True)
    assert outcome.refusal == "the staging shop is down for maintenance"
    assert (files_under(service.config.artifact_dir), files_under(service.config.private_dir)) == kept
