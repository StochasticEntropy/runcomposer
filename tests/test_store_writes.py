"""DESIGN.md §8 lists, for every writing member of the ``RunStore``
protocol, the text it holds and where that text is blanked or why it is
stored as given. A member added to the protocol must be listed here as a
read or a write — and a write must have its row in that table."""

from __future__ import annotations

import re

from runcomposer.sdk import RunStore, contract_members

from .conftest import REPO

WRITES = {
    "add_artifact_ref", "add_dispatch", "append_event", "create_run", "delete_dispatch", "delete_run",
    "prune_runs", "record_delivery", "record_live_verdict", "seed_durations", "set_dispatch_spec",
    "set_ingest_token", "set_item_names", "set_run_headline", "set_spec_document", "update_dispatch",
}
READS = {
    "artifact_refs", "atomic", "close", "delivered_shards", "duration_aggregates", "events", "get_dispatch",
    "get_dispatch_spec", "get_run", "get_spec_document", "ingest_tokens", "item_names", "latest_completed_run",
    "latest_events", "list_runs", "live_verdicts", "owned_dispatches", "store_id", "verdicts_for",
}


def test_every_member_of_the_protocol_is_listed_as_a_read_or_a_write():
    assert WRITES.isdisjoint(READS)
    assert set(contract_members(RunStore)) == WRITES | READS


def test_every_write_has_its_row_in_design_section_8():
    design = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    table = design.split("What the store holds, write by write", 1)[1].split("- **Callables", 1)[0]
    rows = [line for line in table.splitlines() if line.strip().startswith("| `")]
    named = {name for row in rows for name in re.findall(r"`([a-z_]+)`", row.split("|")[1])}
    assert WRITES - named == set(), f"writes without a row in DESIGN.md §8: {sorted(WRITES - named)}"
    assert named - WRITES == set(), f"rows that name no write: {sorted(named - WRITES)}"
