"""core.planning (DESIGN.md §6.2): duration-balanced chunk planning, exposed
so any runner can reuse it."""

from __future__ import annotations

from runcomposer.core.planning import ChunkPlan, plan_chunks, split_max

ITEMS = [f"Shop.Checkout.Standard.T{n:03d}" for n in range(1, 9)]


class TestColdStart:
    def test_no_history_is_round_robin(self):
        plan = plan_chunks(ITEMS[:5], 2)
        assert plan.chunks == (
            (ITEMS[0], ITEMS[2], ITEMS[4]),
            (ITEMS[1], ITEMS[3]),
        )
        assert plan.balanced is False and plan.known == 0 and plan.total == 5
        assert plan.estimates_ms == (0.0, 0.0)

    def test_the_plan_says_it_is_a_cold_start(self):
        text = plan_chunks(ITEMS[:4], 2).describe()
        assert "cold start" in text and "round-robin" in text
        assert "chunk 1: 2 item(s), est 0ms" in text
        assert "duration-balanced" not in text

    def test_history_for_other_items_is_still_a_cold_start(self):
        plan = plan_chunks(ITEMS[:4], 2, {"Shop.Auth.Login.T001": 900.0})
        assert plan.balanced is False

    def test_a_zero_duration_is_not_history(self):
        assert plan_chunks(ITEMS[:2], 2, {ITEMS[0]: 0.0}).balanced is False


class TestBalancing:
    def test_the_slow_item_ends_up_alone(self):
        """Longest first onto the lightest chunk: one 4 s item against three
        100 ms ones is 1 + 3, not 2 + 2."""
        durations = {ITEMS[0]: 4000.0, ITEMS[1]: 100.0, ITEMS[2]: 100.0, ITEMS[3]: 100.0}
        plan = plan_chunks(ITEMS[:4], 2, durations)
        assert sorted(len(chunk) for chunk in plan.chunks) == [1, 3]
        assert (ITEMS[0],) in plan.chunks
        assert plan.balanced is True and plan.known == 4
        assert sorted(plan.estimates_ms) == [300.0, 4000.0]

    def test_the_plan_says_what_it_balanced_on(self):
        durations = {ITEMS[0]: 4000.0, ITEMS[1]: 100.0}
        text = plan_chunks(ITEMS[:4], 2, durations).describe()
        assert "duration-balanced chunking: history for 2/4 item(s)" in text
        assert "cold start" not in text

    def test_items_without_history_weigh_the_mean_of_the_known(self):
        # Known: 1000 and 3000 → an unknown item weighs 2000.
        durations = {ITEMS[0]: 1000.0, ITEMS[1]: 3000.0}
        plan = plan_chunks(ITEMS[:4], 2, durations)
        # 3000 | 2000 → 3000 | 2000+2000 → 3000+1000 | 4000: an even split.
        assert plan.chunks == ((ITEMS[1], ITEMS[0]), (ITEMS[2], ITEMS[3]))
        # The estimate only counts what is actually known.
        assert plan.estimates_ms == (4000.0, 0.0)

    def test_every_item_is_planned_exactly_once(self):
        durations = {item: float(100 * (index + 1)) for index, item in enumerate(ITEMS)}
        plan = plan_chunks(ITEMS, 3, durations)
        planned = [item for chunk in plan.chunks for item in chunk]
        assert sorted(planned) == sorted(ITEMS)
        assert len(plan.chunks) == 3


class TestEdges:
    def test_fewer_items_than_chunks_drops_the_empty_chunks(self):
        plan = plan_chunks(ITEMS[:2], 8)
        assert plan.chunks == ((ITEMS[0],), (ITEMS[1],))
        assert len(plan.estimates_ms) == 2

    def test_an_empty_selection_plans_no_chunk(self):
        plan = plan_chunks([], 4)
        assert plan.chunks == () and plan.total == 0
        assert isinstance(plan, ChunkPlan)
        assert "cold start" in plan.describe()

    def test_a_chunk_count_below_one_is_one(self):
        assert plan_chunks(ITEMS[:3], 0).chunks == (tuple(ITEMS[:3]),)


class TestSplitMax:
    def test_cuts_into_consecutive_pieces(self):
        assert split_max(ITEMS[:5], 2) == [ITEMS[0:2], ITEMS[2:4], ITEMS[4:5]]

    def test_within_the_bound_is_one_piece(self):
        assert split_max(ITEMS[:3], 3) == [ITEMS[:3]]
        assert split_max(ITEMS[:3], 10) == [ITEMS[:3]]

    def test_unbounded(self):
        assert split_max(ITEMS, None) == [ITEMS]
        assert split_max(ITEMS, 0) == [ITEMS]

    def test_nothing_to_split(self):
        assert split_max([], 3) == []
        assert split_max([], None) == []
