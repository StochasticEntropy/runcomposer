"""core.lifecycle (DESIGN.md §4): the state machine, shard accounting and
computed completion — pure functions, so every rule is pinned without a store.
"""

from __future__ import annotations

import pytest

from runcomposer.core.errors import InvalidTransition
from runcomposer.core.lifecycle import (
    ACTIVE_STATES,
    COMPLETIONS,
    RUN_STATES,
    TERMINAL_STATES,
    advance,
    completion_of,
    effective_verdicts,
    headline,
    missing_verdicts,
    outstanding_shards,
    summarize,
)
from runcomposer.core.model import VERDICT_STATUSES, DispatchRecord, Verdict

T1, T2, T3 = "Shop.Payments.Cards.T001", "Shop.Payments.Cards.T002", "Shop.Payments.Cards.T003"


def dispatch(state: str, **fields) -> DispatchRecord:
    return DispatchRecord(
        dispatch_id=fields.pop("dispatch_id", "D1"),
        run_id="R1",
        mode="demo",
        state=state,
        created_at="2026-07-07T10:00:00Z",
        **fields,
    )


class TestVocabulary:
    def test_states_partition_into_active_and_terminal(self):
        assert set(ACTIVE_STATES) | set(TERMINAL_STATES) | {"COMPOSED"} == set(RUN_STATES)
        assert not set(ACTIVE_STATES) & set(TERMINAL_STATES)

    def test_the_vocabulary_for_planned_but_never_reported(self):
        assert "MISSING" in VERDICT_STATUSES
        assert "INCOMPLETE" in COMPLETIONS


class TestTransitions:
    """One transition function; everything not listed is refused."""

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("DISPATCHED", "RUNNING"),
            ("DISPATCHED", "CANCELLED"),
            ("DISPATCHED", "STALE"),
            ("RUNNING", "AWAITING_RESULTS"),
            ("RUNNING", "COMPLETE"),
            ("RUNNING", "FAILED"),
            ("RUNNING", "CANCELLED"),
            ("RUNNING", "STALE"),
            ("AWAITING_RESULTS", "COMPLETE"),
            ("AWAITING_RESULTS", "CANCELLED"),
            ("COMPLETE", "COMPLETE"),  # a corrected shard recomputes completion
        ],
    )
    def test_legal_moves(self, current, target):
        assert advance(current, target) == target

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("RUNNING", "DISPATCHED"),
            ("AWAITING_RESULTS", "RUNNING"),
            ("COMPLETE", "RUNNING"),
            ("COMPLETE", "FAILED"),
            ("COMPLETE", "CANCELLED"),
            ("FAILED", "RUNNING"),
            ("STALE", "RUNNING"),
            ("STALE", "FAILED"),
        ],
    )
    def test_illegal_moves_are_refused(self, current, target):
        with pytest.raises(InvalidTransition, match=f"{current} to {target}"):
            advance(current, target)

    @pytest.mark.parametrize("interrupted", ["FAILED", "STALE"])
    def test_late_results_may_still_complete_an_interrupted_dispatch(self, interrupted):
        assert advance(interrupted, "COMPLETE") == "COMPLETE"

    @pytest.mark.parametrize("target", [s for s in RUN_STATES if s != "COMPOSED"])
    def test_cancelled_is_final(self, target):
        with pytest.raises(InvalidTransition):
            advance("CANCELLED", target)

    def test_unknown_states_are_refused_not_guessed(self):
        with pytest.raises(InvalidTransition, match="unknown lifecycle state"):
            advance("RUNNING", "DONE")
        with pytest.raises(InvalidTransition, match="unknown lifecycle state"):
            advance("COMPOSED", "RUNNING")  # a run is composed; a dispatch never is


class TestShardAccounting:
    def test_outstanding_in_declaration_order(self):
        assert outstanding_shards(["a", "b", "c"], {"b"}) == ["a", "c"]
        assert outstanding_shards(["a", "b"], {"a", "b"}) == []

    def test_completion_is_against_the_declared_set_not_a_count(self):
        """Regression: two deliveries under two labels used to complete a
        two-shard dispatch even though one declared shard never arrived."""
        assert outstanding_shards(["a", "b"], {"a", "a-retry"}) == ["b"]

    def test_nothing_declared_means_nothing_outstanding(self):
        assert outstanding_shards([], {"1"}) == []


class TestEffectiveVerdicts:
    def test_highest_attempt_wins_per_shard_and_item(self):
        verdicts = [
            Verdict(T1, "FAIL", attempt=1, shard="env1"),
            Verdict(T1, "PASS", attempt=2, shard="env1"),
            Verdict(T1, "FAIL", attempt=1, shard="env2"),
            Verdict(T2, "PASS", attempt=1, shard="env1"),
        ]
        effective = {(v.shard, v.item_id): v for v in effective_verdicts(verdicts)}
        assert effective[("env1", T1)].status == "PASS"  # the retry decides
        assert effective[("env2", T1)].status == "FAIL"  # another shard, its own verdict
        assert len(effective) == 3

    def test_order_of_arrival_does_not_decide(self):
        late_first = [Verdict(T1, "PASS", attempt=2), Verdict(T1, "FAIL", attempt=1)]
        assert [v.status for v in effective_verdicts(late_first)] == ["PASS"]

    def test_the_later_one_wins_a_tie(self):
        tie = [Verdict(T1, "FAIL", attempt=1), Verdict(T1, "PASS", attempt=1)]
        assert [v.status for v in effective_verdicts(tie)] == ["PASS"]


class TestMissing:
    def test_one_missing_verdict_per_unreported_planned_item(self):
        missing = missing_verdicts([T1, T2, T3], [Verdict(T1, "PASS", shard="1")])
        assert [(v.item_id, v.status, v.message) for v in missing] == [
            (T2, "MISSING", "not-reported"),
            (T3, "MISSING", "not-reported"),
        ]

    def test_reported_anywhere_counts_without_a_per_shard_plan(self):
        reported = [Verdict(T1, "PASS", shard="env1"), Verdict(T2, "FAIL", shard="env2")]
        assert missing_verdicts([T1, T2], reported) == []

    def test_a_per_shard_plan_asks_per_shard(self):
        """Fanned out over two partitions and reported by one: missing in
        the other."""
        plan = {"env1": [T1, T2], "env2": [T1, T2]}
        reported = [
            Verdict(T1, "PASS", shard="env1"),
            Verdict(T2, "PASS", shard="env1"),
            Verdict(T1, "PASS", shard="env2"),
        ]
        missing = missing_verdicts([T1, T2], reported, plan)
        assert [(v.shard, v.item_id, v.status) for v in missing] == [("env2", T2, "MISSING")]

    def test_everything_planned_is_missing_when_nothing_came_back(self):
        assert len(missing_verdicts([T1, T2], [])) == 2


class TestCompletion:
    def test_nothing_reported_is_incomplete_never_pass(self):
        """Regression: an empty result used to count as PASS."""
        assert completion_of([]) == "INCOMPLETE"

    def test_all_pass_is_pass(self):
        assert completion_of([Verdict(T1, "PASS"), Verdict(T2, "PASS")]) == "PASS"

    def test_skip_alone_is_pass(self):
        assert completion_of([Verdict(T1, "SKIP"), Verdict(T2, "SKIP")]) == "PASS"

    def test_a_missing_item_makes_it_incomplete(self):
        """Regression: 1 of 46 delivered was COMPLETE/PASS."""
        delivered = [Verdict(T1, "PASS")]
        scored = delivered + missing_verdicts([T1, T2, T3], delivered)
        assert completion_of(scored) == "INCOMPLETE"

    def test_precedence_error_fail_incomplete_pass(self):
        passed, failed = Verdict(T1, "PASS"), Verdict(T2, "FAIL")
        missing, error = Verdict(T3, "MISSING"), Verdict("Shop.Cart.Core.T001", "ERROR")
        assert completion_of([passed, missing]) == "INCOMPLETE"
        assert completion_of([passed, missing, failed]) == "FAIL"
        assert completion_of([failed, missing, passed]) == "FAIL"
        assert completion_of([passed, failed, missing, error]) == "ERROR"

    def test_a_retried_item_that_finally_passed_is_a_pass(self):
        verdicts = [Verdict(T1, "FAIL", attempt=1, shard="1"), Verdict(T1, "PASS", attempt=2, shard="1")]
        assert completion_of(verdicts) == "PASS"

    def test_every_result_is_one_of_the_completions(self):
        for verdicts in ([], [Verdict(T1, "PASS")], [Verdict(T1, "FAIL")], [Verdict(T1, "ERROR")]):
            assert completion_of(verdicts) in COMPLETIONS


class TestSummarize:
    def test_counts_effective_verdicts_by_status(self):
        verdicts = [
            Verdict(T1, "FAIL", attempt=1, shard="1"),
            Verdict(T1, "PASS", attempt=2, shard="1"),
            Verdict(T2, "PASS", shard="1"),
            Verdict(T3, "MISSING"),
        ]
        assert summarize(verdicts) == {"PASS": 2, "MISSING": 1}

    def test_empty(self):
        assert summarize([]) == {}


class TestHeadline:
    def test_no_dispatch_is_composed(self):
        assert headline([]) == ("COMPOSED", None, None)

    def test_the_latest_dispatch_decides(self):
        first = dispatch(
            "COMPLETE", completion="FAIL", completed_at="2026-07-07T10:05:00Z", dispatch_id="D1"
        )
        second = dispatch("RUNNING", dispatch_id="D2")
        assert headline([first, second]) == ("RUNNING", None, None)
        assert headline([first]) == ("COMPLETE", "FAIL", "2026-07-07T10:05:00Z")

    def test_removing_a_refused_dispatch_restores_what_was_established(self):
        """Regression: a refused re-dispatch used to erase a finished run.
        The headline is derived, so without the refused dispatch it is simply
        the previous one's again."""
        finished = dispatch("COMPLETE", completion="FAIL", completed_at="2026-07-07T10:05:00Z")
        refused = dispatch("DISPATCHED", dispatch_id="D2")
        assert headline([finished, refused])[0] == "DISPATCHED"
        assert headline([finished]) == ("COMPLETE", "FAIL", "2026-07-07T10:05:00Z")
