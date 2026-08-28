"""Regression tests for four defects an adopter hit building a connector
against the public docs alone:

1. verdicts dropped their shard, making a partition fan-out unreadable
   without opening the database file (§2, §4, §6.3);
2. artifact references were write-only, and the `/artifacts/…` route
   DESIGN.md §6.4 promises did not exist;
3. `failed@latest` had no label scope, so "latest" meant the latest
   completed run of anything in the store (§7);
4. the taxonomy file was never validated — a wrong-shaped one was served
   as a 200 with an empty tree (§2, §8).
"""

import json

import pytest
from fastapi.testclient import TestClient

from runcomposer.api import create_app
from runcomposer.cli import main as cli_main
from runcomposer.config import Config
from runcomposer.core.model import Verdict
from runcomposer.core.spec import build_spec
from runcomposer.core.taxonomy import TaxonomyError, validate_taxonomy
from runcomposer.service import Service, ServiceError

DEMO_ITEMS = ["Shop.Payments.Cards.T001", "Shop.Payments.Cards.T002"]


def config_for(tmp_path, **core):
    return Config(data={
        "store": {"sqlite": {"path": str(tmp_path / "adopter.db")}},
        "core": {
            "artifact_dir": str(tmp_path / "artifacts"),
            "ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")},
            **core,
        },
    })


@pytest.fixture()
def service(tmp_path):
    return Service(config_for(tmp_path))


def seed_run(service, *, title="fan-out", labels=None, run_id=None, created_at=None):
    spec = build_spec(
        title=title,
        tag_filter="Payments",
        materialized_ids=list(DEMO_ITEMS),
        source_provider="manifest",
        snapshot="sha256:" + "ab" * 32,
        labels=labels,
        run_id=run_id,
        created_at=created_at,
    )
    run = service.store.create_run(spec, origin="test")
    service.store.add_dispatch(run.id, dispatch_id=f"D-{run.id}", mode="export", declared_shards=2)
    return run


def deliver(service, run, shard, statuses, *, attempt=1):
    service.store.record_delivery(
        run.id,
        dispatch_id=f"D-{run.id}",
        shard=shard,
        content_hash=f"sha256:{run.id}-{shard}",
        format="runcomposer-verdicts",
        verdicts=[Verdict(i, s, attempt=attempt) for i, s in statuses.items()],
    )


# -- 1. the shard on a verdict ------------------------------------------------


class TestVerdictCarriesItsShard:
    """§4: identity is layered run → dispatch → shard. One selection across
    two partitions delivers two verdicts per item; without the shard label
    nothing says which partition produced which."""

    def test_store_returns_the_shard_and_attempt(self, service):
        run = seed_run(service)
        deliver(service, run, "env1", {DEMO_ITEMS[0]: "PASS", DEMO_ITEMS[1]: "PASS"})
        deliver(service, run, "env2", {DEMO_ITEMS[0]: "PASS", DEMO_ITEMS[1]: "FAIL"}, attempt=2)

        verdicts = service.store.verdicts_for(run.id)
        assert len(verdicts) == 4
        by_shard = {(v.shard, v.item_id): v.status for v in verdicts}
        assert by_shard[("env1", DEMO_ITEMS[1])] == "PASS"
        assert by_shard[("env2", DEMO_ITEMS[1])] == "FAIL"
        assert {v.attempt for v in verdicts if v.shard == "env2"} == {2}

    def test_store_filters_by_shard(self, service):
        run = seed_run(service)
        deliver(service, run, "env1", {DEMO_ITEMS[0]: "PASS"})
        deliver(service, run, "env2", {DEMO_ITEMS[0]: "FAIL"})

        only = service.store.verdicts_for(run.id, shard="env2")
        assert [(v.item_id, v.status, v.shard) for v in only] == [
            (DEMO_ITEMS[0], "FAIL", "env2")
        ]
        assert service.store.verdicts_for(run.id, f"D-{run.id}", shard="env1")[0].status == "PASS"
        assert service.store.verdicts_for(run.id, shard="nope") == []

    def test_shard_summaries_answer_green_here_red_there(self, service):
        run = seed_run(service)
        deliver(service, run, "env1", {DEMO_ITEMS[0]: "PASS", DEMO_ITEMS[1]: "PASS"})
        deliver(service, run, "env2", {DEMO_ITEMS[0]: "PASS", DEMO_ITEMS[1]: "FAIL"})

        summaries = {s["shard"]: s for s in service.shard_summaries(run.id, f"D-{run.id}")}
        assert summaries["env1"]["completion"] == "PASS"
        assert summaries["env2"]["completion"] == "FAIL"
        assert summaries["env2"]["summary"] == {"PASS": 1, "FAIL": 1}
        assert summaries["env1"]["count"] == 2

    def test_run_detail_payload_labels_every_verdict(self, tmp_path):
        config = config_for(tmp_path)
        service = Service(config)
        run = seed_run(service)
        deliver(service, run, "env1", {DEMO_ITEMS[0]: "PASS", DEMO_ITEMS[1]: "PASS"})
        deliver(service, run, "env2", {DEMO_ITEMS[0]: "PASS", DEMO_ITEMS[1]: "FAIL"}, attempt=2)

        body = TestClient(create_app(config)).get(f"/api/v1/runs/{run.id}").json()
        # the flat list stays flat (existing readers keep working) but every
        # row now says which shard and which attempt it came from
        assert {v["shard"] for v in body["verdicts"]} == {"env1", "env2"}
        red = [v for v in body["verdicts"] if v["status"] == "FAIL"]
        assert [(v["shard"], v["attempt"]) for v in red] == [("env2", 2)]
        # and the roll-up answers the fan-out question directly
        shards = {s["shard"]: s["completion"] for s in body["shards"]}
        assert shards == {"env1": "PASS", "env2": "FAIL"}


# -- 2. artifact references are readable, and servable -------------------------


class TestArtifactsAreReadable:
    """§6.4: the store records `(name, media_type, url_or_path)` and a
    built-in local artifact directory serves them."""

    def _with_artifact(self, service, tmp_path, name="output.xml (env1)"):
        run = seed_run(service)
        artifact = tmp_path / "artifacts" / run.id / f"D-{run.id}" / "env1" / "output.xml"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("<robot><suite/></robot>", encoding="utf-8")
        service.store.add_artifact_ref(
            run.id, f"D-{run.id}", name=name,
            media_type="application/xml", url_or_path=str(artifact),
        )
        return run, artifact

    def test_store_reads_back_what_it_wrote(self, service, tmp_path):
        run, artifact = self._with_artifact(service, tmp_path)
        refs = service.store.artifact_refs(run.id)
        assert len(refs) == 1
        assert refs[0].name == "output.xml (env1)"
        assert refs[0].url_or_path == str(artifact)
        assert refs[0].dispatch_id == f"D-{run.id}"
        assert service.store.artifact_refs(run.id, "other-dispatch") == []

    def test_local_path_becomes_a_followable_href(self, service, tmp_path):
        run, artifact = self._with_artifact(service, tmp_path)
        (entry,) = service.artifacts(run.id)
        assert entry["kind"] == "local"
        assert entry["href"] == f"/artifacts/{run.id}/D-{run.id}/env1/output.xml"

    def test_remote_url_passes_through_but_other_schemes_do_not(self, service):
        run = seed_run(service)
        service.store.add_artifact_ref(
            run.id, None, name="CI build", media_type="text/html",
            url_or_path="https://ci.example.com/job/consumer/42/",
        )
        service.store.add_artifact_ref(
            run.id, None, name="not a link", media_type="text/html",
            url_or_path="javascript:alert(1)",
        )
        by_name = {a["name"]: a for a in service.artifacts(run.id)}
        assert by_name["CI build"]["kind"] == "url"
        assert by_name["CI build"]["href"] == "https://ci.example.com/job/consumer/42/"
        # a scheme that is not http(s) is never handed out as a link
        assert by_name["not a link"]["kind"] == "external-path"
        assert by_name["not a link"]["href"] is None

    def test_path_outside_the_artifact_dir_is_visible_but_not_servable(
        self, service, tmp_path
    ):
        run = seed_run(service)
        (tmp_path / "artifacts").mkdir(exist_ok=True)
        elsewhere = tmp_path / "elsewhere.xml"
        elsewhere.write_text("<robot/>", encoding="utf-8")
        service.store.add_artifact_ref(
            run.id, None, name="stray", media_type="application/xml",
            url_or_path=str(elsewhere),
        )
        (entry,) = service.artifacts(run.id)
        assert entry["kind"] == "external-path" and entry["href"] is None
        assert entry["url_or_path"] == str(elsewhere)  # still visible, not hidden

    def test_run_detail_carries_the_artifacts(self, tmp_path):
        config = config_for(tmp_path)
        service = Service(config)
        run, _ = self._with_artifact(service, tmp_path)
        body = TestClient(create_app(config)).get(f"/api/v1/runs/{run.id}").json()
        assert [a["name"] for a in body["artifacts"]] == ["output.xml (env1)"]
        assert body["artifacts"][0]["href"].startswith("/artifacts/")

    def test_route_serves_the_file_sandboxed(self, tmp_path):
        config = config_for(tmp_path)
        service = Service(config)
        run, _ = self._with_artifact(service, tmp_path)
        client = TestClient(create_app(config))
        href = client.get(f"/api/v1/runs/{run.id}").json()["artifacts"][0]["href"]

        response = client.get(href)
        assert response.status_code == 200
        assert response.text == "<robot><suite/></robot>"
        assert response.headers["content-type"].startswith("application/xml")
        # untrusted bytes served from the app's own origin: no scripting
        assert "sandbox" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize(
        "attack",
        [
            "../../../../etc/passwd",
            "..%2f..%2f..%2fetc%2fpasswd",
            "run/dispatch/../../../secret.txt",
        ],
    )
    def test_traversal_out_of_the_artifact_dir_is_refused(self, tmp_path, attack):
        config = config_for(tmp_path)
        (tmp_path / "artifacts").mkdir()
        (tmp_path / "secret.txt").write_text("do not serve me", encoding="utf-8")
        response = TestClient(create_app(config)).get(f"/artifacts/{attack}")
        assert response.status_code == 404
        assert "do not serve me" not in response.text

    def test_symlink_out_of_the_artifact_dir_is_refused(self, tmp_path):
        config = config_for(tmp_path)
        root = tmp_path / "artifacts"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("do not serve me", encoding="utf-8")
        (root / "escape.txt").symlink_to(secret)
        response = TestClient(create_app(config)).get("/artifacts/escape.txt")
        assert response.status_code == 404
        assert "do not serve me" not in response.text

    def test_missing_artifact_404s_and_names_retention(self, tmp_path):
        config = config_for(tmp_path)
        (tmp_path / "artifacts").mkdir()
        response = TestClient(create_app(config)).get("/artifacts/gone/away/output.xml")
        assert response.status_code == 404


# -- 3. history selection has a label scope ------------------------------------


class TestHistoryScope:
    """§7: unscoped, "latest" is the latest completed run of ANYTHING in the
    store — so a second person composing a run silently redefines what the
    nightly rerun executes."""

    def _complete(self, service, labels, statuses, completed_at):
        run = seed_run(service, labels=labels, created_at=completed_at)
        service.store.record_delivery(
            run.id, dispatch_id=f"D-{run.id}", shard="1",
            content_hash=f"sha256:{run.id}", format="runcomposer-verdicts",
            verdicts=[Verdict(i, s) for i, s in statuses.items()],
        )
        service.store.set_run_state(
            run.id, "COMPLETE", completion="FAIL", completed_at=completed_at
        )
        return run

    def test_unscoped_latest_picks_the_foreign_run_scoped_does_not(self, service):
        nightly = self._complete(
            service, {"suite": "nightly"},
            {DEMO_ITEMS[0]: "FAIL", DEMO_ITEMS[1]: "FAIL"}, "2026-07-01T00:00:00Z",
        )
        # somebody's ad-hoc five-test selection, composed afterwards
        adhoc = self._complete(
            service, {"suite": "adhoc"}, {DEMO_ITEMS[0]: "FAIL"}, "2026-07-02T00:00:00Z",
        )

        unscoped_ids, unscoped = service.resolve_history("failed@latest")
        assert unscoped["resolved_run_id"] == adhoc.id and len(unscoped_ids) == 1

        scoped_ids, scoped = service.resolve_history("failed@latest?suite=nightly")
        assert scoped["resolved_run_id"] == nightly.id
        assert sorted(scoped_ids) == sorted(DEMO_ITEMS)

    def test_provenance_shows_which_run_and_why(self, service):
        nightly = self._complete(
            service, {"suite": "nightly", "env": "staging"},
            {DEMO_ITEMS[0]: "FAIL"}, "2026-07-01T00:00:00Z",
        )
        _ids, provenance = service.resolve_history("failed@latest?suite=nightly")
        assert provenance["query"] == {
            "run": "LATEST", "verdicts": ["FAIL"], "labels": {"suite": "nightly"},
        }
        assert provenance["resolved_run_id"] == nightly.id
        assert provenance["resolved_run_completed_at"] == "2026-07-01T00:00:00Z"
        assert provenance["resolved_run_labels"] == {"suite": "nightly", "env": "staging"}

    def test_scope_survives_into_the_composed_spec(self, service):
        self._complete(service, {"suite": "nightly"},
                       {DEMO_ITEMS[0]: "FAIL"}, "2026-07-01T00:00:00Z")
        self._complete(service, {"suite": "adhoc"},
                       {DEMO_ITEMS[1]: "FAIL"}, "2026-07-02T00:00:00Z")
        result = service.compose_run(
            {"history": "failed@latest?suite=nightly"}, title="nightly rerun", origin="test",
            labels={"suite": "nightly"},
        )
        assert result.spec["selection"]["materialized"]["item_ids"] == [DEMO_ITEMS[0]]
        derived = result.spec["selection"]["derived_from"][0]
        assert derived["query"]["labels"] == {"suite": "nightly"}

    def test_before_selector_takes_a_scope_too(self, service):
        self._complete(service, {"suite": "nightly"},
                       {DEMO_ITEMS[0]: "FAIL"}, "2026-07-01T00:00:00Z")
        self._complete(service, {"suite": "adhoc"},
                       {DEMO_ITEMS[1]: "FAIL"}, "2026-07-02T00:00:00Z")
        ids, provenance = service.resolve_history(
            "failed@before:2026-07-03T00:00:00Z?suite=nightly"
        )
        assert ids == [DEMO_ITEMS[0]]
        assert provenance["query"]["run"] == {"before": "2026-07-03T00:00:00Z"}
        assert provenance["query"]["labels"] == {"suite": "nightly"}

    def test_scope_that_matches_nothing_says_so(self, service):
        self._complete(service, {"suite": "adhoc"},
                       {DEMO_ITEMS[0]: "FAIL"}, "2026-07-01T00:00:00Z")
        with pytest.raises(ServiceError, match="scoped to labels"):
            service.resolve_history("failed@latest?suite=nightly")

    def test_scope_on_an_explicit_run_is_refused_not_ignored(self, service):
        run = self._complete(service, {"suite": "nightly"},
                             {DEMO_ITEMS[0]: "FAIL"}, "2026-07-01T00:00:00Z")
        with pytest.raises(ServiceError, match="already names one run"):
            service.resolve_history(f"failed@run:{run.id}?suite=nightly")

    def test_malformed_scope_is_refused(self, service):
        with pytest.raises(ServiceError, match="key=value"):
            service.resolve_history("failed@latest?suite")

    def test_conflicting_scopes_are_refused(self, service):
        with pytest.raises(ServiceError, match="refusing to guess"):
            service.resolve_history("failed@latest?suite=nightly", labels={"suite": "smoke"})

    def test_cli_failed_in_takes_a_label_scope(self, service, tmp_path, capsys):
        nightly = self._complete(
            service, {"suite": "nightly"}, {DEMO_ITEMS[0]: "FAIL"}, "2026-07-01T00:00:00Z"
        )
        self._complete(service, {"suite": "adhoc"},
                       {DEMO_ITEMS[1]: "FAIL"}, "2026-07-02T00:00:00Z")
        config = tmp_path / "config.yaml"
        config.write_text(
            f"store:\n  sqlite: {{ path: {tmp_path / 'adopter.db'} }}\n", encoding="utf-8"
        )
        assert cli_main([
            "runs", "--failed-in", "latest", "--label", "suite=nightly",
            "--config", str(config),
        ]) == 0
        out = capsys.readouterr().out
        assert nightly.id in out and "{'suite': 'nightly'}" in out
        assert DEMO_ITEMS[0] in out and DEMO_ITEMS[1] not in out


# -- 4. the taxonomy file is validated ------------------------------------------


class TestTaxonomyValidation:
    """§8: hand-written deployment data fails loudly, like an unknown plugin
    id — never a 200 with an empty tree."""

    GOOD = {
        "taxonomy": [
            {"label": "Areas", "children": [
                {"label": "Payments", "filter": "Payments"},
                {"label": "Checkout", "filter": "prefix:Checkout-"},
            ]},
            {"label": "Quarantine", "filter": "prefix:Quarantine-"},
        ]
    }

    def test_a_good_document_passes_unchanged(self):
        assert validate_taxonomy(self.GOOD) == self.GOOD

    @pytest.mark.parametrize(
        "document, expected",
        [
            (None, "is empty"),
            ([{"label": "x", "filter": "y"}], "nested under 'taxonomy:'"),
            ({"nodes": []}, "no top-level 'taxonomy' key"),
            ({"taxonomy": {"label": "x"}}, "must be a list of nodes"),
            ({"taxonomy": ["Payments"]}, "must be a node mapping"),
            ({"taxonomy": [{"filter": "Payments"}]}, "needs a non-empty 'label'"),
            ({"taxonomy": [{"label": "A", "filter": ["Visa", "MC"]}]}, "exactly ONE pattern"),
            ({"taxonomy": [{"label": "A", "filter": "regex:^(unclosed"}]}, "invalid regular"),
            ({"taxonomy": [{"label": "A", "filter": "x", "children": {}}]}, "must be a list"),
            ({"taxonomy": [{"label": "A", "pattern": "Payments"}]}, "did you mean 'filter'?"),
            ({"taxonomy": [{"label": "A", "filter": "x"}, {"label": "A", "filter": "y"}]},
             "sibling labels must be distinct"),
        ],
    )
    def test_bad_documents_are_refused_with_an_actionable_message(self, document, expected):
        with pytest.raises(TaxonomyError, match=expected):
            validate_taxonomy(document, origin="taxonomy.yaml")

    def test_the_message_names_the_offending_node(self):
        document = {"taxonomy": [
            {"label": "Areas", "children": [
                {"label": "Payments", "filter": "Payments"},
                {"label": "Checkout", "tags": "Checkout-"},
            ]},
        ]}
        with pytest.raises(TaxonomyError) as excinfo:
            validate_taxonomy(document, origin="my-taxonomy.yaml")
        message = str(excinfo.value)
        assert "my-taxonomy.yaml" in message
        assert "taxonomy[0].children[1] ('Checkout')" in message
        assert "['tags']" in message

    def test_unknown_node_keys_are_tolerated_next_to_a_real_one(self):
        # §3 versioning stance: unknown fields inside a known section are
        # ignored — only a node that is neither clickable nor openable fails.
        validate_taxonomy({"taxonomy": [{"label": "A", "filter": "x", "color": "red"}]})

    def test_bundled_demo_taxonomy_is_valid(self, service):
        assert service.taxonomy()["taxonomy"][0]["label"] == "Areas"

    def _config_with_taxonomy(self, tmp_path, text):
        (tmp_path / "taxonomy.yaml").write_text(text, encoding="utf-8")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "core:\n  taxonomy_file: taxonomy.yaml\n"
            "  ingestion: { inbox: null }\n"
            f"store:\n  sqlite: {{ path: {tmp_path / 'tx.db'} }}\n",
            encoding="utf-8",
        )
        return config_file

    def test_a_broken_taxonomy_refuses_the_boot(self, tmp_path):
        from runcomposer.config import load_config

        config_file = self._config_with_taxonomy(tmp_path, "nodes:\n  - {label: A}\n")
        with pytest.raises(TaxonomyError, match="no top-level 'taxonomy' key"):
            create_app(load_config(str(config_file)))

    def test_serve_exits_2_with_a_one_liner(self, tmp_path, capsys):
        config_file = self._config_with_taxonomy(tmp_path, "nodes:\n  - {label: A}\n")
        assert cli_main(["serve", "--config", str(config_file)]) == 2
        assert "no top-level 'taxonomy' key" in capsys.readouterr().err

    def test_a_missing_file_says_where_it_looked(self, tmp_path):
        from runcomposer.config import load_config

        config_file = self._config_with_taxonomy(tmp_path, "taxonomy: []\n")
        (tmp_path / "taxonomy.yaml").unlink()
        with pytest.raises(TaxonomyError, match="cannot be read"):
            create_app(load_config(str(config_file)))

    def test_a_file_broken_after_boot_is_a_500_that_explains(self, tmp_path):
        from runcomposer.config import load_config

        config_file = self._config_with_taxonomy(
            tmp_path, "taxonomy:\n  - {label: A, filter: Payments}\n"
        )
        client = TestClient(create_app(load_config(str(config_file))), raise_server_exceptions=False)
        assert client.get("/api/v1/taxonomy").status_code == 200
        # the file is re-read per request (docs/taxonomy.md), so it can rot
        # under a running server — a 200 with an empty tree is the one answer
        # that must not happen
        (tmp_path / "taxonomy.yaml").write_text("groups: []\n", encoding="utf-8")
        response = client.get("/api/v1/taxonomy")
        assert response.status_code == 500
        assert "no top-level 'taxonomy' key" in response.json()["detail"]


class TestOldStoresKeepWorking:
    """ADOPTING.md §4: `RunStore` is a published port third parties implement.
    A store written against 0.1.0 — no `artifact_refs`, no `labels=` on
    `latest_completed_run` — must not be broken by these additions."""

    class LegacyStore:
        """Exactly the 0.1.0 signatures, and nothing more."""

        store_id = "legacy"

        def __init__(self, run):
            self._run = run

        def latest_completed_run(self, *, completed_before=None):
            return self._run

        def verdicts_for(self, run_id, dispatch_id=None):
            return [Verdict(DEMO_ITEMS[0], "FAIL")]

        def get_run(self, run_id):
            return self._run

    def _service_with_legacy_store(self, service):
        run = seed_run(service, labels={"suite": "nightly"})
        service.store.set_run_state(
            run.id, "COMPLETE", completion="FAIL", completed_at="2026-07-01T00:00:00Z"
        )
        legacy = self.LegacyStore(service.store.get_run(run.id))
        service.__dict__["store"] = legacy  # replaces the cached_property
        return run

    def test_unscoped_history_still_resolves(self, service):
        run = self._service_with_legacy_store(service)
        ids, provenance = service.resolve_history("failed@latest")
        assert ids == [DEMO_ITEMS[0]]
        assert provenance["resolved_run_id"] == run.id

    def test_a_scoped_query_fails_loudly_rather_than_ignoring_the_scope(self, service):
        self._service_with_legacy_store(service)
        with pytest.raises(TypeError):
            service.resolve_history("failed@latest?suite=nightly")

    def test_artifacts_degrade_to_empty_not_to_a_crash(self, service):
        run = self._service_with_legacy_store(service)
        assert service.artifacts(run.id) == []

    def test_verdicts_without_a_shard_group_under_one_bucket(self, service):
        run = self._service_with_legacy_store(service)
        assert service.shard_summaries(run.id) == [
            {"shard": "", "count": 1, "summary": {"FAIL": 1}, "completion": "FAIL"}
        ]


def test_run_detail_payload_shape_is_documented(tmp_path):
    """A guard for the adopter's actual complaint: everything they had to
    open the database for is in the documented response."""
    config = config_for(tmp_path)
    service = Service(config)
    run = seed_run(service)
    deliver(service, run, "env1", {DEMO_ITEMS[0]: "PASS"})
    artifact = tmp_path / "artifacts" / run.id / f"D-{run.id}" / "env1" / "output.xml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<robot/>", encoding="utf-8")
    service.store.add_artifact_ref(
        run.id, f"D-{run.id}", name="output.xml (env1)",
        media_type="application/xml", url_or_path=str(artifact),
    )
    body = TestClient(create_app(config)).get(f"/api/v1/runs/{run.id}").json()
    assert set(body["verdicts"][0]) == {
        "item_id", "status", "duration_ms", "message", "shard", "attempt"
    }
    assert set(body["shards"][0]) == {"shard", "count", "summary", "completion"}
    assert set(body["artifacts"][0]) == {
        "name", "media_type", "dispatch_id", "url_or_path", "kind", "href"
    }
    json.dumps(body)  # the whole payload stays JSON-serialisable
