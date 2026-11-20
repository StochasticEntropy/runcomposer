"""How ``store-0.1.7.sql`` was produced — kept so the fixture is reproducible
and its contents are explained, not so that it runs here.

It must be executed by **runcomposer 0.1.7** (it uses that release's
``Service``/``SqliteRunStore`` API, which 0.2 replaced)::

    pip install runcomposer==0.1.7
    python make_store_0_1_7.py workdir
    sqlite3 workdir/store.db .dump > store-0.1.7.sql

The database it writes is a small history on the bundled web-shop corpus,
covering every shape the 0.2 migration has to handle:

==========================  ====================================================
Run (title)                 What 0.1.7 stored
==========================  ====================================================
Nightly regression          dispatched to the demo runner, COMPLETE
Checkout export             exported, AWAITING_RESULTS, ingest token in the spec
Auth smoke (composed only)  COMPOSED, never dispatched, token in the spec
Payments partial            exported; ONE of 17 planned items came back, yet
                            0.1.7 recorded COMPLETE / PASS
Catalog direct ingest       a delivery attached to the run with no dispatch
Cart interrupted            RUNNING with streamed live rows when the process died
Payments double delivery    two deliveries for one shard (the 0.1 write race)
==========================  ====================================================
"""

import json
import sqlite3
import sys
from pathlib import Path

from runcomposer.config import load_config
from runcomposer.service import Service

from runcomposer.core.model import Verdict


def main(workdir: str) -> None:
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("store:\n  sqlite: { path: store.db }\n", encoding="utf-8")
    service = Service(load_config(str(root / "config.yaml")))
    store = service.store

    def bundle(name: str, run_id: str, names: list[str], status: str = "PASS") -> Path:
        target = root / name
        target.mkdir(exist_ok=True)
        (target / "results.json").write_text(
            json.dumps(
                {
                    "format": "runcomposer-verdicts",
                    "verdicts": [{"name": n, "status": status, "duration_ms": 120} for n in names],
                }
            ),
            encoding="utf-8",
        )
        (target / "runcomposer_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        return target

    nightly = service.compose_run(
        {"tag_filter": "Regression"}, title="Nightly regression",
        labels={"suite": "nightly"}, origin="cli",
    )
    dispatch = service.dispatch_runner(nightly.run.id, "demo")
    store.add_artifact_ref(
        nightly.run.id, dispatch.dispatch_id, name="results.json",
        media_type="application/json", url_or_path="artifacts/nightly/results.json",
    )

    checkout = service.compose_run({"tag_filter": "Checkout"}, title="Checkout export", origin="cli")
    service.export_dispatch(checkout.run.id, spec_bytes=json.dumps(checkout.spec).encode())

    service.compose_run({"tag_filter": "Auth"}, title="Auth smoke (composed only)", origin="api")

    partial = service.compose_run({"tag_filter": "Payments"}, title="Payments partial", origin="cli")
    service.export_dispatch(partial.run.id, spec_bytes=json.dumps(partial.spec).encode())
    first = partial.spec["selection"]["materialized"]["item_ids"][0]
    service.ingest(bundle("partial", partial.run.id, [first]))

    direct = service.compose_run({"tag_filter": "Catalog"}, title="Catalog direct ingest", origin="cli")
    ids = direct.spec["selection"]["materialized"]["item_ids"]
    service.ingest(bundle("direct", direct.run.id, ids))

    cart = service.compose_run({"tag_filter": "Cart"}, title="Cart interrupted", origin="api")
    cart_ids = cart.spec["selection"]["materialized"]["item_ids"]
    running = store.add_dispatch(cart.run.id, dispatch_id="01LIVEDISPATCH0000000000000", mode="demo", declared_shards=2)
    store.set_run_state(cart.run.id, "RUNNING")
    store.record_live_verdict(cart.run.id, running.dispatch_id, "1", Verdict(cart_ids[0], "PASS", 90))

    double = service.compose_run({"tag_filter": "Payments"}, title="Payments double delivery", origin="cli")
    double_dispatch = service.export_dispatch(double.run.id, spec_bytes=json.dumps(double.spec).encode())
    double_ids = double.spec["selection"]["materialized"]["item_ids"]
    service.ingest(bundle("double", double.run.id, double_ids, status="FAIL"))
    # The second, later delivery for the same shard that 0.1's unlocked
    # check-then-write could leave behind: written the way the race did, past
    # the store's own replacement step.
    conn = sqlite3.connect(root / "store.db")
    conn.execute(
        "INSERT INTO deliveries VALUES (?, ?, ?, '1', 'sha256:second', 'runcomposer-verdicts', '2099-01-01T00:00:00Z')",
        ("01SECONDDELIVERY00000000000", double.run.id, double_dispatch.dispatch_id),
    )
    conn.executemany(
        "INSERT INTO verdicts VALUES (?, ?, '1', '01SECONDDELIVERY00000000000', ?, 'PASS', 100, '', 1)",
        [(double.run.id, double_dispatch.dispatch_id, item_id) for item_id in double_ids],
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1])
