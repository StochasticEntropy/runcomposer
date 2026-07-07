"""`runcomposer demo` — boots the neutral web-shop corpus end-to-end (DESIGN.md §12).

The P0 in-process loop: manifest source → taxonomy view → tag-filter
selection → materialized runspec (validated against the shipped schema) →
demo runner → verdict summary — then a history-derived "rerun what failed"
selection with recorded provenance.
"""

from __future__ import annotations

from importlib import resources
from typing import Any, Callable

import yaml

from runcomposer.core.filter import format_filter, parse_filter
from runcomposer.core.model import Item, Verdict
from runcomposer.core.selection import Selection
from runcomposer.core.spec import build_spec, validate_document
from runcomposer.plugins.demo_runner import DemoRunner
from runcomposer.plugins.manifest_source import ManifestSource

_RULE = "─" * 64

Out = Callable[[str], Any]


def _demo_dir():
    return resources.files("runcomposer.demo")


def _print_taxonomy_node(node: dict, items: list[Item], out: Out, indent: int) -> None:
    pad = "  " * indent
    if "filter" in node:
        matcher = parse_filter(node["filter"])
        count = sum(1 for item in items if matcher.matches(item.tags))
        out(f"{pad}{node['label']:<12} {count:>3} items   ({node['filter']})")
    else:
        out(f"{pad}{node['label']}")
    for child in node.get("children", ()):
        _print_taxonomy_node(child, items, out, indent + 1)


def _summarize(verdicts: list[Verdict], out: Out) -> None:
    counts = {status: 0 for status in ("PASS", "FAIL", "SKIP", "ERROR")}
    for verdict in verdicts:
        counts[verdict.status] += 1
    total_s = sum(v.duration_ms for v in verdicts) / 1000
    parts = ", ".join(f"{n} {status}" for status, n in counts.items() if n)
    out(f"Results: {parts}  ({len(verdicts)} items, {total_s:.1f}s simulated)")
    failed = [v for v in verdicts if v.status == "FAIL"]
    for verdict in failed[:5]:
        out(f"  FAIL  {verdict.item_id} — {verdict.message}")
    if len(failed) > 5:
        out(f"  … and {len(failed) - 5} more failures")


def _compose_and_run(
    *,
    title: str,
    seed: str,
    materialized: list[Item],
    source: ManifestSource,
    tag_filter: Any = None,
    item_ids: list[str] | None = None,
    derived_from: list[dict] | None = None,
) -> tuple[dict, DemoRunner]:
    spec = build_spec(
        title=title,
        tag_filter=tag_filter,
        item_ids=item_ids,
        materialized_ids=[item.id for item in materialized],
        derived_from=derived_from,
        source_provider=source.provider_id,
        source_root="corpus.json",
        snapshot=source.snapshot(),
        results={"expect": [{"format": "demo-verdicts"}], "shards": 1, "deliver": "none"},
        runner={"demo": {"seed": seed}},
        labels={"origin": "demo"},
    )
    report = validate_document(spec, for_dispatch=True)
    if not report.ok:
        raise AssertionError(f"demo composed an invalid spec: {report.errors}")
    runner = DemoRunner(seed=seed)
    runner.dispatch(spec)
    return spec, runner


def run_demo(out: Out = print) -> int:
    source = ManifestSource(_demo_dir() / "corpus.json")
    items = source.items()

    out(_RULE)
    out("runcomposer demo — fictional web-shop corpus")
    out(_RULE)
    out(f"Corpus: {len(items)} items via the '{source.provider_id}' source")
    out(f"Catalog snapshot: {source.snapshot()[:29]}…")
    out("")

    out("Taxonomy (curated tree over tag patterns — data, not code):")
    taxonomy = yaml.safe_load((_demo_dir() / "taxonomy.yaml").read_text(encoding="utf-8"))
    for node in taxonomy["taxonomy"]:
        _print_taxonomy_node(node, items, out, indent=1)
    out("")

    # 1) Compose: tag-filter selection → materialized, validated runspec.
    filter_data = {
        "op": "AND",
        "items": [
            {"op": "OR", "items": ["Payments", "prefix:Checkout-", "regex:^Cart(V2)?$"]},
            {"not": "prefix:Quarantine-"},
        ],
    }
    selection = Selection.from_data({"tag_filter": filter_data})
    matched = selection.compile(items)
    out(f"Selection: {format_filter(selection.tag_filter)}")
    out(f"  matched {len(matched)} of {len(items)} items")

    spec, runner = _compose_and_run(
        title="Web-shop regression without quarantined tests",
        seed="demo-1",
        materialized=matched,
        source=source,
        tag_filter=filter_data,
    )
    out(f"Run spec {spec['run']['id']}: validates against the runspec-1.0 schema (dispatch profile)")
    out("")
    out("Spec document (excerpt):")
    excerpt = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).splitlines()
    for line in excerpt[:14]:
        out(f"  {line}")
    out(f"  … ({len(excerpt) - 14} more lines)")
    out("")

    delivery = runner.deliveries[-1]
    out(
        f"Dispatched to the '{runner.runner_id}' runner: "
        f"dispatch {delivery['dispatch_id']}, {len(runner.deliveries)} shard delivered"
    )
    _summarize(delivery["verdicts"], out)
    out("")

    # 2) "Rerun what failed" — pre-seeded history (DESIGN.md §7, §12).
    out(_RULE)
    out('"Rerun what failed" — history-derived selection')
    out(_RULE)
    regression_items = Selection.from_data({"tag_filter": "Regression"}).compile(items)
    history: list[tuple[dict, DemoRunner]] = []
    for n in (1, 2, 3):
        history.append(
            _compose_and_run(
                title=f"Nightly regression #{n}",
                seed=f"nightly-{n}",
                materialized=regression_items,
                source=source,
                tag_filter="Regression",
            )
        )
    out(f"Seeded history: {len(history)} completed runs over 'Regression' ({len(regression_items)} items each)")

    latest_spec, latest_runner = history[-1]
    failed_ids = [v.item_id for v in latest_runner.deliveries[-1]["verdicts"] if v.status == "FAIL"]
    out(f"Latest completed run {latest_spec['run']['id']}: {len(failed_ids)} FAIL")
    if not failed_ids:
        out("Nothing failed in the latest run — nothing to rerun.")
        return 0

    rerun_items = Selection.from_data({"item_ids": failed_ids}).compile(items)
    rerun_spec, rerun_runner = _compose_and_run(
        title="Rerun failures of the latest nightly regression",
        seed="rerun-1",
        materialized=rerun_items,
        source=source,
        item_ids=failed_ids,
        derived_from=[
            {
                "provider": "history",
                "query": {"run": "LATEST", "verdicts": ["FAIL"]},
                "resolved_run_id": latest_spec["run"]["id"],
            }
        ],
    )
    out(f"Derived selection: {len(rerun_items)} items, provenance recorded in selection.derived_from")
    _summarize(rerun_runner.deliveries[-1]["verdicts"], out)
    out("")
    out("Demo complete. Next steps:")
    out("  runcomposer catalog                      # list the demo corpus + snapshot")
    out("  runcomposer validate <spec.yaml|json>    # validate any runspec document")
    return 0
