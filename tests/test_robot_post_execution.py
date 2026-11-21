"""After every Robot execution of a shard, in the worker: the built-in
``clean_output`` and a deployment's ``post_execution`` steps
(``runcomposer.sdk.robot``, `robot-pool`; DESIGN.md §6.2a, §6.6) — with real
Robot Framework and real worker processes.

- ``robot_options`` such as ``removekeywords`` shape the log and report;
  they leave the execution's ``output.xml`` as big as it was.
- ``clean_output`` rewrites the ``output.xml`` itself, before it is parsed,
  merged, recorded or pushed — and what it holds about every test (status,
  message, tags, duration, the id) is exactly what it held before.
- A ``post_execution`` step gets the output's path and what ``worker_setup``
  gets, plus which execution it was; one that raises makes that execution's
  tests ``ERROR``, naming the step.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser
from runcomposer.sdk import ConfigError
from runcomposer.sdk.robot import (
    Execution,
    ShardPayload,
    clean_output,
    read_shard_result,
    run_shard,
    supervise,
)

from .conftest import REPO, needs_robot
from .test_robot_pool import (
    execute,
    executed_tests,
    pool_service,
    shop_suite,
    verdict_map,
    workdir,
)

pytestmark = needs_robot

HERE = "tests.test_robot_post_execution"

#: Five tests with keyword data worth removing: loops that log, one failure,
#: tags — and a suite teardown that passes.
CATALOG = """\
*** Settings ***
Suite Teardown    Log    catalog closed

*** Test Cases ***
Catalog Lists Every Product
    [Tags]    Catalog
    FOR    ${index}    IN RANGE    30
        Log    product ${index} is listed on the storefront
    END

Catalog Filters By Size
    [Tags]    Catalog    Cart
    FOR    ${index}    IN RANGE    30
        Log    size filter ${index} applied
    END

Catalog Rejects An Unknown Category
    [Tags]    Catalog
    Log    looking up the category
    Fail    category 'garden' is unknown

Catalog Shows Prices In Euro
    [Tags]    Catalog
    Log    prices shown in EUR

Catalog Search Finds Shoes
    [Tags]    Catalog
    Log    searched for shoes
"""
IDS = [
    "Shop.Catalog.Catalog Lists Every Product",
    "Shop.Catalog.Catalog Filters By Size",
    "Shop.Catalog.Catalog Rejects An Unknown Category",
    "Shop.Catalog.Catalog Shows Prices In Euro",
    "Shop.Catalog.Catalog Search Finds Shoes",
]
FAILING = IDS[2]
CLEAN = {"remove_keywords": ["passed"], "flatten_keywords": ["for"]}


def catalog_root(tmp_path: Path) -> Path:
    return shop_suite(tmp_path, CATALOG, name="catalog.robot")


def verdict_fields(output: Path) -> list[tuple[str, str, int, str]]:
    """What the parser makes of an output, field by field."""
    return [
        (v.native_name, v.status, v.duration_ms, v.message) for v in RobotOutputXmlParser().parse(output)
    ]


def robots_fields(output: Path) -> list[tuple[str, str, str, list[str], int]]:
    """What Robot itself reads: id, status, message, tags, elapsed ms."""
    from robot.api import ExecutionResult

    return [
        (t.longname, t.status, t.message, [str(tag) for tag in t.tags], t.elapsedtime)
        for t in ExecutionResult(str(output)).suite.all_tests
    ]


# -- steps a deployment configures ------------------------------------------------------


def note_setup(info: dict) -> dict:
    """worker_setup: contributes a variable, so the step can see it."""
    return {"STOREFRONT": "eu.shop.example"}


def record_step(output: str, info: dict, *, settings: dict) -> None:
    """post_execution: notes what it was handed, where it ran."""
    entry = {
        "output": output, "exists": Path(output).is_file(), "pid": os.getpid(),
        "settings": settings, "info": info,
    }
    with open(Path(info["out_dir"]) / "steps.jsonl", "a", encoding="utf-8") as notes:
        notes.write(json.dumps(entry) + "\n")


def refuse_the_first(output: str, info: dict) -> None:
    if info["execution"] == 1:
        raise RuntimeError("the archive refused the file")


def keep_raw_then_clean(output: str, info: dict) -> None:
    """Keeps the output as Robot wrote it beside, then cleans it — so the
    two can be compared."""
    shutil.copy(output, Path(output).with_name("raw.xml"))
    clean_output(output, info, settings=CLEAN)


def steps_of(out_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (out_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()]


# -- robot_options do not touch output.xml --------------------------------------------------


class TestRobotOptionsShapeTheLogOnly:
    def test_removekeywords_leaves_the_output_as_big_as_it_was(self, tmp_path):
        root = catalog_root(tmp_path)
        plain, shaped = tmp_path / "plain", tmp_path / "shaped"
        run_shard(ShardPayload(shard="a", out_dir=str(plain), executions=(Execution(str(root), tuple(IDS)),)))
        run_shard(ShardPayload(
            shard="b", out_dir=str(shaped), executions=(Execution(str(root), tuple(IDS)),),
            robot_options={"removekeywords": ["passed"], "flattenkeywords": ["for"]},
        ))
        text = (shaped / "output.xml").read_text(encoding="utf-8")
        assert "product 29 is listed on the storefront" in text
        size_plain, size_shaped = ((d / "output.xml").stat().st_size for d in (plain, shaped))
        assert abs(size_plain - size_shaped) < size_plain * 0.02, (size_plain, size_shaped)


# -- clean_output -----------------------------------------------------------------------


class TestCleanOutput:
    def test_every_verdict_field_is_kept_and_the_file_shrinks(self, tmp_path):
        out = tmp_path / "out"
        run_shard(ShardPayload(
            shard="eu", out_dir=str(out), executions=(Execution(str(catalog_root(tmp_path)), tuple(IDS)),),
            post_execution=(f"{HERE}:keep_raw_then_clean",),
        ))
        raw, cleaned = out / "raw.xml", out / "output.xml"
        assert verdict_fields(cleaned) == verdict_fields(raw)
        assert robots_fields(cleaned) == robots_fields(raw)
        assert [name for name, *_rest in verdict_fields(cleaned)] == IDS
        assert cleaned.stat().st_size < raw.stat().st_size / 3, (cleaned.stat().st_size, raw.stat().st_size)
        text = cleaned.read_text(encoding="utf-8")
        assert "product 29 is listed" not in text and "looking up the category" in text  # failed: kept

    def test_the_payload_option_cleans_every_execution_rerun_and_the_shard_s_merge(self, tmp_path):
        root = catalog_root(tmp_path)
        executions = (Execution(str(root), tuple(IDS[:2])), Execution(str(root), tuple(IDS[2:])))
        plain, cleaned = tmp_path / "plain", tmp_path / "cleaned"
        for out, clean in ((plain, {}), (cleaned, CLEAN)):
            run_shard(ShardPayload(shard="eu", out_dir=str(out), executions=executions, rerun_failed=1,
                                   clean_output=clean))
        result = read_shard_result(cleaned)
        assert result is not None and result.error is None
        assert [(o.attempt, Path(o.path).parent.name) for o in result.outputs] == [
            (1, "execution-1"), (1, "execution-2"), (2, "attempt-2"),
        ]
        for output in [*(Path(o.path) for o in result.outputs), cleaned / "output.xml"]:
            text = output.read_text(encoding="utf-8")
            assert "product 29 is listed" not in text and "size filter 7" not in text, output
        # The same verdicts as without the cleaning — but for the durations of two executions.
        def statuses(out: Path) -> dict[str, tuple[str, list[str], str]]:
            return executed_tests(out / "output.xml")

        assert statuses(cleaned) == statuses(plain)
        assert (cleaned / "output.xml").stat().st_size < (plain / "output.xml").stat().st_size / 2

    def test_robot_pool_cleans_before_parsing_recording_and_merging(self, make_service, tmp_path):
        root = catalog_root(tmp_path)
        plain = pool_service(make_service, root, partitions=["staging", "preview"], partition_mode="split",
                             merge_groups=True, max_tests_per_chunk=1, chunk_shards=True)
        run_plain = execute(plain, "Catalog")
        cleaned = pool_service(make_service, root, partitions=["staging", "preview"], partition_mode="split",
                               merge_groups=True, max_tests_per_chunk=1, chunk_shards=True,
                               clean_output=CLEAN)
        run = execute(cleaned, "Catalog")
        assert (run.state, run.completion) == ("COMPLETE", "FAIL")
        def by_item(service: Any, done: Any) -> dict[str, str]:
            return {item: status for (_shard, item, _attempt), status in verdict_map(service, done).items()}

        assert by_item(cleaned, run) == by_item(plain, run_plain)
        artifacts = {a["name"]: Path(a["url_or_path"]) for a in cleaned.artifacts.list(run.id, server_paths=True)}
        merged = [path for name, path in artifacts.items() if name.startswith("output.xml (")]
        assert any("(run)" in name for name in artifacts) and len(merged) >= 3
        for path in merged:
            assert "product 29 is listed" not in path.read_text(encoding="utf-8"), path

    def test_robot_s_own_selectors_are_accepted_and_others_refused_before_anything_runs(self):
        from robot.errors import DataError
        from robot.result.flattenkeywordmatcher import validate_flatten_keyword
        from robot.result.keywordremover import KeywordRemover

        def robot_accepts(key: str, value: str) -> bool:
            try:
                KeywordRemover.from_config(value) if key == "remove_keywords" else validate_flatten_keyword([value])
            except DataError:
                return False
            return True

        def payload_accepts(key: str, value: str) -> bool:
            try:
                ShardPayload(shard="eu", out_dir="out", executions=(), clean_output={key: [value]})
            except ValueError:
                return False
            return True

        for key in ("remove_keywords", "flatten_keywords"):
            for value in ("passed", "PASSED", "all", "for", "while", "wuks", "iteration", "foritem",
                          "name:Shop.*", "NAME:Log", "tag:slow", "tag:", "garden", "passed,for", ""):
                assert payload_accepts(key, value) == robot_accepts(key, value), (key, value)

    @pytest.mark.parametrize(
        ("clean", "message"),
        [
            ({"remove_keywords": ["garden"]}, "'garden' is not a selector"),
            ({"removekeywords": ["passed"]}, "takes remove_keywords and flatten_keywords"),
            (["passed"], "is a mapping"),
        ],
    )
    def test_robot_pool_refuses_startup_with_a_wrong_selector(self, make_service, tmp_path, clean, message):
        with pytest.raises(ConfigError, match=message):
            pool_service(make_service, catalog_root(tmp_path), clean_output=clean).validate()


# -- post_execution -----------------------------------------------------------------------


class TestPostExecution:
    def test_each_execution_and_each_re_execution_reaches_the_steps_in_the_worker(self, tmp_path):
        root = catalog_root(tmp_path)
        out = tmp_path / "out"
        shard = ShardPayload(
            shard="eu-1", out_dir=str(out), partition="eu", run_id="run-1", dispatch_id="dispatch-1",
            executions=(Execution(str(root), tuple(IDS[:2])), Execution(str(root), tuple(IDS[2:]))),
            rerun_failed=1, worker_setup=(f"{HERE}:note_setup",), worker_data={"booking": "staging-eu-7"},
            variables={"REGION": "eu"}, clean_output=CLEAN,
            post_execution=({"call": f"{HERE}:record_step", "with": {"archive": "eu.shop.example"}},),
        )
        (outcome,) = supervise([shard], max_workers=1)
        assert outcome.problem is None, outcome.problem

        steps = steps_of(out)
        assert [(s["info"]["execution"], s["info"]["attempt"], s["info"]["rerun"]) for s in steps] == [
            (1, 1, False), (2, 1, False), (None, 2, True),
        ]
        assert [s["info"]["item_ids"] for s in steps] == [IDS[:2], IDS[2:], [FAILING]]
        assert [Path(s["output"]).parent.name for s in steps] == ["execution-1", "execution-2", "attempt-2"]
        first = steps[0]
        assert first["exists"] and first["pid"] != os.getpid()
        assert first["settings"] == {"archive": "eu.shop.example"}
        info = first["info"]
        # What worker_setup gets — the final variables included — and which execution.
        assert {key: info[key] for key in ("shard", "partition", "run_id", "dispatch_id", "data")} == {
            "shard": "eu-1", "partition": "eu", "run_id": "run-1", "dispatch_id": "dispatch-1",
            "data": {"booking": "staging-eu-7"},
        }
        assert Path(info["out_dir"]) == out.resolve()
        assert info["variables"] == {"REGION": "eu", "STOREFRONT": "eu.shop.example"}
        assert info["root"] == str(root)
        # The steps run after the cleaning: they see the cleaned file.
        assert "product 29 is listed" not in Path(first["output"]).read_text(encoding="utf-8")

    def test_a_step_that_raises_makes_its_execution_error_naming_the_step(self, make_service, tmp_path):
        service = pool_service(
            make_service, catalog_root(tmp_path), max_workers=1, max_tests_per_chunk=2,
            post_execution=[f"{HERE}:refuse_the_first"], pythonpath=[str(REPO)],
        )
        run = execute(service, "Catalog")
        assert run.state == "COMPLETE"
        verdicts = {v.item_id: v for v in service.store.verdicts_for(run.id)}
        for item in IDS[:2]:  # the first execution: its results are not used
            assert verdicts[item].status == "ERROR"
            assert verdicts[item].message.startswith(
                f"post_execution step '{HERE}:refuse_the_first' raised after execution 1 of "
            )
            assert "RuntimeError: the archive refused the file" in verdicts[item].message
            assert "its 2 test(s) are reported as ERROR" in verdicts[item].message
        assert {item: verdicts[item].status for item in IDS[2:]} == {
            IDS[2]: "FAIL", IDS[3]: "PASS", IDS[4]: "PASS",
        }
        # The shard's merged result holds what is trusted only.
        assert set(executed_tests(workdir(service, run) / "default" / "output.xml")) == set(IDS[2:])
        said = [(e.kind, str(e.data.get("message"))) for e in service.store.events(run.id)]
        assert any(kind in ("warning", "error") and "refuse_the_first" in message for kind, message in said)

    def test_robot_pool_rejects_a_malformed_entry(self, make_service, tmp_path):
        with pytest.raises(ConfigError, match="post_execution entries are import paths"):
            pool_service(make_service, catalog_root(tmp_path), post_execution=["no_colon"]).validate()


class TestTheShippedExample:
    def test_robot_regions_cleans_its_outputs(self, tmp_path, capsys):
        """`examples/robot-pool/config.yaml`, entry `robot-regions`: with
        ``clean_output`` the recorded outputs — each shard's, each region's
        merge and the run's — carry Robot's note where passing keywords were
        removed, and the run passes as it does without it."""
        import shutil

        from runcomposer.cli.main import main

        root = tmp_path / "examples"
        for name in ("robot-pool", "robot-shop"):
            shutil.copytree(REPO / "examples" / name, root / name,
                            ignore=shutil.ignore_patterns("state", "__pycache__"))
        code = main(["run", "Cart-Basics", "--runner", "robot-regions",
                     "--config", str(root / "robot-pool" / "config.yaml"), "--json"])
        run = json.loads(capsys.readouterr().out)
        assert code == 0 and (run["state"], run["completion"]) == ("COMPLETE", "PASS"), run
        outputs = [a for a in run["artifacts"] if a["name"].startswith("output.xml (")]
        assert {"output.xml (group eu)", "output.xml (group us)", "output.xml (run)"} <= {a["name"] for a in outputs}
        work = root / "robot-pool" / "state" / "artifacts" / run["id"]
        recorded = sorted(work.rglob("output.xml"))
        assert recorded
        for output in recorded:
            assert "Content removed using the --remove-keywords option." in output.read_text(encoding="utf-8")
