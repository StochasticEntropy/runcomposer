"""What a duration-balanced plan weighs, and that it says so (DESIGN.md §6.2).

An item without a known duration weighs the mean of the known ones. A plan
whose partitions hold very different numbers of items is then usually the
balance working — one slow item against many quick ones — and the plan
must make that readable: how many items had a duration, what the others
were given, how long each partition is estimated to take, and which item
dominates.
"""

from __future__ import annotations

from runcomposer.core.planning import plan_chunks

from .conftest import needs_robot
from .test_robot_pool import pool_service, shop_suite

ITEMS = [f"Shop.Checkout.Standard.T{n:03d}" for n in range(1, 59)]
SLOW, SLOWER = ITEMS[0], ITEMS[1]


class TestTheWeights:
    def test_every_item_known_one_dominant_item_ends_up_alone(self):
        """The shape that reads as broken: 57 items on one side, 1 on the
        other — the one takes 14 s, the 57 together about 8 s."""
        durations = dict.fromkeys(ITEMS, 1.0)
        durations.update({SLOW: 14003.0, SLOWER: 8004.0})
        plan = plan_chunks(ITEMS, 2, durations)
        assert sorted(len(chunk) for chunk in plan.chunks) == [1, 57]
        assert (SLOW,) in plan.chunks
        assert sorted(plan.loads_ms) == [8060.0, 14003.0]
        assert plan.fallback_ms == 0.0 and plan.unknown == (0, 0)
        assert plan.dominant == (SLOW, 14003.0)
        text = plan.describe()
        assert "history for 58/58 item(s)" in text and "weigh the mean" not in text
        assert f"{SLOW} alone weighs 14003ms, more than an even share of 11032ms per chunk" in text

    def test_unknown_items_weigh_the_mean_of_the_known_and_the_plan_says_so(self):
        durations = {SLOW: 14003.0, SLOWER: 8004.0, ITEMS[2]: 1.0}
        plan = plan_chunks(ITEMS, 2, durations)
        fallback = (14003.0 + 8004.0 + 1.0) / 3
        assert plan.fallback_ms == fallback
        assert sum(plan.unknown) == 55
        # Balanced on measured + assumed weights; the estimates count the measured only.
        assert abs(sum(plan.loads_ms) - (14003.0 + 8004.0 + 1.0 + 55 * fallback)) < 1e-6
        assert sum(plan.estimates_ms) == 14003.0 + 8004.0 + 1.0
        assert max(plan.loads_ms) - min(plan.loads_ms) <= fallback
        text = plan.describe()
        assert f"the other 55 weigh the mean of the known durations, {fallback:.0f}ms each" in text
        assert "without history → balanced on" in text

    def test_one_known_item_makes_every_item_weigh_the_same(self):
        """Only one duration known, an outlier: every unknown weighs that
        much too, so the split is by count — nothing is put on one side."""
        plan = plan_chunks(ITEMS, 2, {SLOW: 14003.0})
        assert sorted(len(chunk) for chunk in plan.chunks) == [29, 29]
        assert plan.fallback_ms == 14003.0 and plan.dominant is None

    def test_no_known_item_is_round_robin_without_weights(self):
        plan = plan_chunks(ITEMS, 2)
        assert [len(chunk) for chunk in plan.chunks] == [29, 29]
        assert plan.loads_ms == () and plan.unknown == () and plan.fallback_ms == 0.0
        assert plan.dominant is None and "cold start" in plan.describe()


@needs_robot
class TestAFastTestIsKnownToBeFast:
    def test_a_test_that_ran_in_under_a_millisecond_is_1ms_not_unknown(self, tmp_path):
        """Robot reports whole milliseconds, so a quick test reads as 0 — and
        0 means "no duration known" to the history. Weighed as unknown, it
        counted as long as the mean of the known ones: in the example corpus
        40 of 58 tests, each weighed at 1.3 s because two tests sleep 8 s and
        14 s."""
        import re

        from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser
        from runcomposer.sdk.robot import run_shard

        from .test_robot_pool import FIVE_TESTS
        from .test_robot_toolkit import CART, payload

        root = shop_suite(tmp_path, FIVE_TESTS, name="cart.robot")
        run_shard(payload(root, tmp_path / "out", CART))
        output = tmp_path / "out" / "output.xml"
        text = output.read_text(encoding="utf-8")
        assert 'elapsed="' in text  # Robot 7's own format
        output.write_text(re.sub(r'elapsed="[0-9.]+"', 'elapsed="0.000300"', text), encoding="utf-8")
        parsed = {p.native_name: p for p in RobotOutputXmlParser().parse(output)}
        assert {name: p.duration_ms for name, p in parsed.items()} == dict.fromkeys(CART, 1)
        assert parsed["Shop.Cart.Cart Rejects A Negative Quantity"].status == "FAIL"


@needs_robot
class TestThePreviewExplainsTheSplit:
    SUITE = "*** Test Cases ***\n" + "".join(
        f"Checkout Step {n}\n    [Tags]    Checkout\n    Log    {n}\n\n" for n in range(1, 7)
    )

    def lanes(self, make_service, tmp_path, durations: dict[str, float]):
        root = shop_suite(tmp_path, self.SUITE)
        service = pool_service(
            make_service, root, partitions=["eu", "us"], partition_mode="split",
            max_tests_per_chunk=1, chunk_shards=True,
        )
        service.import_durations(durations)
        return service.plan("robot-pool", selection={"tag_filter": "Checkout"}).preview

    def test_one_slow_item_alone_in_a_partition_is_explained(self, make_service, tmp_path):
        ids = [f"Shop.Checkout.Checkout Step {n}" for n in range(1, 7)]
        preview = self.lanes(make_service, tmp_path, {ids[0]: 14000.0, **dict.fromkeys(ids[1:], 100.0)})
        groups = list(preview.groups.values())
        assert sorted((groups.count("eu"), groups.count("us"))) == [1, 5]
        assert preview.data["durations"] == {"known": 6, "total": 6, "fallback_ms": 0.0}
        assert sorted(preview.data["partition_estimates_ms"].values()) == [500.0, 14000.0]
        assert any(
            note.startswith(f"{ids[0]} alone takes 14.0s, more than an even share")
            and "not a fault" in note
            for note in preview.notes
        )
        assert "est ~14.0s" in preview.text and "est ~0.5s" in preview.text

    def test_items_without_history_and_their_weight_are_named(self, make_service, tmp_path):
        ids = [f"Shop.Checkout.Checkout Step {n}" for n in range(1, 7)]
        preview = self.lanes(make_service, tmp_path, {ids[0]: 3000.0, ids[1]: 1000.0})
        assert preview.data["durations"] == {"known": 2, "total": 6, "fallback_ms": 2000.0}
        assert any(
            note.startswith("duration history for 2 of 6 item(s); the other 4 are weighed with "
                            "the mean of the known durations, 2.0s each")
            for note in preview.notes
        )
        # 3000 + 1000 + 4 × 2000 = 12000, balanced into two lanes of 6000.
        assert sorted(preview.data["partition_estimates_ms"].values()) == [6000.0, 6000.0]
