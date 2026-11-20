"""Callables of the example web shop that a deployment names in its config —
``"tests.shop_callables:<name>"`` — the way a connector's own would be
(``"shop_connector.hooks:<name>"``). Each is written against
``runcomposer.sdk`` alone.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from runcomposer.sdk import DispatchContext, PlanContext

# -- robot-pool: pre_dispatch, worker_setup ------------------------------------------


def reserve_partitions(ctx: DispatchContext, info: dict[str, Any]) -> dict[str, str]:
    """A ``pre_dispatch`` step: the shop's staging environments are booked
    before anything runs on them. It knows which partitions were chosen and
    what each will execute; one that is frozen for a release refuses the
    dispatch. What it books is kept where nobody can download it."""
    frozen = [p for p in info["partitions"] if p in str(ctx.parameters.get("frozen", "")).split(",")]
    if frozen:
        ctx.refuse(f"partition(s) {frozen} are frozen for the release — choose others")
    booking = {
        "partitions": info["partitions"],
        "shards": {shard: len(items) for shard, items in info["shards"].items()},
        "groups": info["groups"],
        "parameters": info["parameters"],
        "booking_key": "k-booking-4711",
    }
    (ctx.private_dir / "booking.json").write_text(json.dumps(booking), encoding="utf-8")
    (ctx.workdir / "prepared.txt").write_text("partitions reserved", encoding="utf-8")
    ctx.log(f"reserved {len(info['partitions'])} partition(s) for {len(info['item_ids'])} test(s)")
    return {"BOOKING": "booked-" + "-".join(info["partitions"])}


def record_window(info: dict[str, Any]) -> None:
    """A ``worker_setup`` step that notes when its shard's worker started."""
    Path(info["out_dir"], "started.json").write_text(
        json.dumps({"shard": info["shard"], "partition": info["partition"], "at": time.time(),
                    "pid": os.getpid()}),
        encoding="utf-8",
    )


# -- jenkins: field_providers, postprocess ------------------------------------------------


def include_tags(ctx: PlanContext, fields: dict[str, Any]) -> str:
    """A ``field_providers`` entry: the shop's nightly job selects by tag,
    not by test id — so the tags of the selection become the job's
    ``INCLUDE`` parameter. A selection that cannot be said that way (a
    test without one of the job's feature tags) is refused before anything
    is triggered."""
    tags: list[str] = []
    planned = set(ctx.item_ids)
    for item in ctx.source.items():
        if item.id not in planned:
            continue
        own = sorted(tag for tag in item.tags if tag.startswith(("Checkout-", "Payments-", "Auth-")))
        if not own:
            ctx.refuse(f"the nightly job selects by tag, and {item.id} carries none it knows")
        tags.extend(tag for tag in own if tag not in tags)
    return "OR".join(sorted(tags))


def shard_count(ctx: PlanContext, fields: dict[str, Any]) -> str:
    """A ``field_providers`` entry computed from another field."""
    return str(len([name for name in str(fields["shards"]).split(",") if name]))


_BLOCK = re.compile(r"^=== results (?P<shard>\S+) ===\n(?P<body>.*?)\n=== end ===$", re.S | re.M)


def results_from_console(ctx: DispatchContext, shard: str, directory: Path) -> Path:
    """A ``postprocess`` step: this job does not archive its results — it
    prints them, per shard, into its console log. What was downloaded is the
    console text; the shard's block becomes the results file to parse."""
    text = (directory / "consoleText").read_text(encoding="utf-8")
    blocks = {match["shard"]: match["body"] for match in _BLOCK.finditer(text)}
    if shard not in blocks:
        raise RuntimeError(f"the console log has no results block for shard {shard!r}")
    extracted = directory / "extracted"
    extracted.mkdir(exist_ok=True)
    (extracted / "results.json").write_text(blocks[shard], encoding="utf-8")
    ctx.log(f"shard {shard}: results taken from the console log")
    return extracted
