"""What the example shop's fan-out job needs from the runcomposer side — two
small callables named in ``config.yaml`` (make this directory importable:
``PYTHONPATH=examples/jenkins``). Written against ``runcomposer.sdk`` alone,
as a connector's own would be.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runcomposer.sdk import DispatchContext, PlanContext

_BLOCK = re.compile(r"^=== results (?P<shard>\S+) ===\n(?P<body>.*?)\n=== end ===$", re.S | re.M)


#: The features the fan-out job is set up for, as tag prefixes.
FEATURES = ("Checkout-", "Payments-", "Auth-")


def include_tags(ctx: PlanContext, fields: dict[str, Any]) -> str:
    """``field_providers.include``: the feature tags of the selected tests,
    as the job's ``INCLUDE`` parameter. A selection holding a test of a
    feature the job is not set up for is refused before anything is
    triggered — the job could not run it."""
    tags: list[str] = []
    planned = set(ctx.item_ids)
    for item in ctx.source.items():
        if item.id not in planned:
            continue
        own = sorted(tag for tag in item.tags if tag.startswith(FEATURES))
        if not own:
            ctx.refuse(
                f"the fan-out job runs the features {[f.rstrip('-') for f in FEATURES]}, and "
                f"{item.id} belongs to none of them"
            )
        tags.extend(tag for tag in own if tag not in tags)
    return "OR".join(sorted(tags))


def results_from_console(ctx: DispatchContext, shard: str, directory: Path) -> Path:
    """``postprocess``: the job archives nothing — it prints each region's
    results into its console log. What was downloaded is ``consoleText``;
    the shard's block becomes the results file that is parsed."""
    text = (directory / "consoleText").read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = {match["shard"]: match["body"] for match in _BLOCK.finditer(text)}
    if shard not in blocks:
        raise RuntimeError(f"the console log has no results block for shard {shard!r}")
    extracted = directory / "extracted"
    extracted.mkdir(exist_ok=True)
    (extracted / "results.json").write_text(blocks[shard], encoding="utf-8")
    ctx.log(f"shard {shard}: results taken from the console log")
    return extracted
