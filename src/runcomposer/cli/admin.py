"""Operating a deployment: serve · doctor · demo."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from .common import EXIT_NO, EXIT_OK, EXIT_USAGE, add_config_arg, add_json_arg, emit_json
from .logs import configure_logging


def register(sub: Any) -> dict[str, Any]:
    p_demo = sub.add_parser("demo", help="boot the neutral web-shop demo end-to-end")
    p_demo.add_argument(
        "--workspace",
        metavar="DIR",
        help="where to seed the demo's config + store (default: ./runcomposer-demo). "
        "The directory must be empty or a previous demo workspace; delete it to "
        "undo the demo entirely (DESIGN.md §12)",
    )

    p_serve = sub.add_parser("serve", help="run the API + UI server")
    p_serve.add_argument("--host", help="bind host (default: from config, 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="bind port (default: from config, 8100)")
    p_serve.add_argument("--log-level", default="info",
                         choices=("debug", "info", "warning", "error"),
                         help="what the runcomposer.* loggers print (default: info)")
    add_config_arg(p_serve)

    p_doctor = sub.add_parser(
        "doctor",
        help="check a deployment: config, plugins (distribution + version), runner health, "
        "taxonomy drift",
    )
    p_doctor.add_argument("-v", "--details", action="store_true",
                          help="also print every runner's health data (what its checks found) "
                          "— the same data --json and the API carry")
    add_json_arg(p_doctor)
    add_config_arg(p_doctor)

    return {"demo": cmd_demo, "serve": cmd_serve, "doctor": cmd_doctor}


def _data_lines(data: Any, indent: str = "") -> list[str]:
    """A runner's health ``data`` as indented ``key: value`` lines — the
    structure is the runner's own, so this renders whatever it holds. A list
    of ``{check, ok, detail}`` entries reads as a checklist."""
    lines: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{indent}{key}:")
                lines.extend(_data_lines(value, indent + "  "))
            else:
                lines.append(f"{indent}{key}: {value}")
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and "check" in entry and "ok" in entry:
                mark = "ok  " if entry["ok"] else "FAIL"
                detail = f" — {entry['detail']}" if entry.get("detail") else ""
                lines.append(f"{indent}[{mark}] {entry['check']}{detail}")
            elif isinstance(entry, (dict, list)):
                lines.extend(_data_lines(entry, indent + "- "))
            else:
                lines.append(f"{indent}- {entry}")
    return lines


#: How many items a failed suite-root check names before it only counts.
_NAMED_ITEMS = 3


def _check_suite_roots(runner: dict[str, Any], items: list[Any], problems: list[str]) -> None:
    """A ``robot-pool`` runner executes items from its suite roots, and a
    dispatch whose items the roots would not find is refused. The same
    check over the whole catalog, as a row of the runner's health checks:
    a catalog built from other suites than the runner executes shows here,
    not when the first run is refused."""
    health = runner.get("health") or {}
    data = health.get("data")
    if not isinstance(data, dict) or not data.get("suite_roots") or not data.get("robot_version"):
        return  # no roots or no Robot: health says so already
    from runcomposer.sdk.robot import root_mismatches

    mismatches = root_mismatches(
        [str(root) for root in data["suite_roots"]], [item.id for item in items], items,
        name=data.get("suite_name") or None,
    )
    row: dict[str, Any] = {"check": "catalog items under the suite roots", "ok": not mismatches}
    if mismatches:
        named = "; ".join(mismatches[:_NAMED_ITEMS])
        more = len(mismatches) - _NAMED_ITEMS
        row["detail"] = (
            f"{len(mismatches)} of {len(items)} catalog item(s) are not under the suite roots "
            f"— a run selecting them is refused: {named}" + (f"; … and {more} more" if more > 0 else "")
        )
        problems.append(f"runner {runner['id']}: {row['detail']}")
    else:
        row["detail"] = f"all {len(items)} catalog item(s) are under the suite roots"
    data["checks"] = [*(data.get("checks") or []), row]


def cmd_demo(args: argparse.Namespace) -> int:
    from runcomposer.demo.demo import DemoError, run_demo

    try:
        return run_demo(workspace=args.workspace)
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from runcomposer.app.config import load_config
    from runcomposer.http import create_app

    configure_logging(level=getattr(logging, args.log_level.upper()), server=True)
    config = load_config(args.config)
    host = args.host or config.api["host"]
    port = args.port or config.api["port"]
    # Builds every plugin AND validates the taxonomy; fails loudly (§8). The
    # address it is served on decides the default of core.api.hosts.
    app = create_app(config, bind_host=host)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Everything that can be wrong with a deployment before a run is
    started, in one report. Exit 1 when something is."""
    from runcomposer import __version__
    from runcomposer.app.config import load_config
    from runcomposer.app.service import Service
    from runcomposer.core.errors import RunComposerError
    from runcomposer.core.registry import installed_plugins

    from .compose import taxonomy_drift

    report: dict[str, Any] = {"version": __version__, "problems": []}
    problems: list[str] = report["problems"]
    config = load_config(args.config)
    report["config"] = str(config.path) if config.path else "(built-in defaults — no config file)"
    report["plugins"] = [
        {"group": p.group, "name": p.name, "distribution": p.distribution, "version": p.version}
        for p in installed_plugins()
    ]
    with Service(config) as service:
        try:
            service.validate()
            report["valid"] = True
        except RunComposerError as exc:
            report["valid"] = False
            problems.append(f"configuration: {exc}")
        if report["valid"]:
            report["configured"] = service.configured_plugins()
            stats = service.catalog.stats()
            report["catalog"] = stats
            if stats["duplicate_ids"]:
                problems.append(
                    f"catalog: {stats['duplicate_ids']} id(s) belong to more than one item"
                )
            report["runners"] = service.runner_infos(refresh=True)
            items = None
            for runner in report["runners"]:
                if runner.get("plugin") == "robot-pool" and not runner.get("error"):
                    items = service.catalog.items() if items is None else items
                    _check_suite_roots(runner, items, problems)
            for runner in report["runners"]:
                health = runner.get("health") or {}
                if runner.get("error"):
                    problems.append(f"runner {runner['id']}: {runner['error']}")
                elif health.get("status") in ("down", "degraded"):
                    problems.append(
                        f"runner {runner['id']}: {health['status']} — {health.get('detail', '')}"
                    )
            leaves, unclaimed, dead, tag_count = taxonomy_drift(service)
            report["taxonomy"] = {
                "configured": config.taxonomy_configured,
                "leaves": len(leaves),
                "tags": tag_count,
                "unclaimed_tags": len(unclaimed),
                "dead_leaves": len(dead),
            }
            if config.taxonomy_configured and (unclaimed or dead):
                problems.append(
                    f"taxonomy: {len(unclaimed)} tag(s) no leaf claims, {len(dead)} leaf/leaves "
                    "matching nothing (details: runcomposer taxonomy-check)"
                )
            stale = service.recover()
            report["recovered_stale"] = [dispatch.run_id for dispatch in stale]

    if args.as_json:
        emit_json(report)
        return EXIT_NO if problems else EXIT_OK
    print(f"runcomposer {report['version']}")
    print(f"config: {report['config']}")
    print("installed plugins:")
    for plugin in report["plugins"]:
        print(f"  {plugin['group'].split('.')[-1]:<10} {plugin['name']:<22} "
              f"{plugin['distribution']} {plugin['version']}")
    if report.get("valid"):
        print("configured (what this deployment loads):")
        for plugin in report["configured"]:
            origin = f"{plugin['distribution']} {plugin['version']}".strip() or "(no distribution)"
            print(f"  {plugin['section']:<10} {plugin['id']:<22} {plugin['plugin']}  {origin}")
        catalog = report["catalog"]
        print(f"catalog: {catalog['items']} items, {catalog['tags']} tags, "
              f"sources {catalog['sources']}, snapshot {catalog['snapshot'][:19]}…")
        print("runners:" if report["runners"] else "runners: none configured (export only)")
        for runner in report["runners"]:
            if runner.get("error"):
                print(f"  {runner['id']}: ERROR {runner['error']}")
                continue
            health = runner["health"]
            print(f"  {runner['id']} ({runner['plugin']}): {health['status']}"
                  + (f" — {health['detail']}" if health["detail"] else ""))
            if args.details or args.verbose:
                for line in _data_lines(health.get("data") or {}):
                    print(f"      {line}")
            for parameter in runner["parameters"]:
                choices = f" {parameter['choices']}" if parameter["choices"] else ""
                print(f"      --param {parameter['name']}=<{parameter['kind']}>{choices}"
                      f"  default {parameter['default']!r}")
        taxonomy = report["taxonomy"]
        print(f"taxonomy: {'configured' if taxonomy['configured'] else 'bundled demo taxonomy'}, "
              f"{taxonomy['leaves']} leaf pattern(s); {taxonomy['unclaimed_tags']} unclaimed "
              f"tag(s), {taxonomy['dead_leaves']} dead leaf/leaves")
        if report["recovered_stale"]:
            print(f"recovered: {len(report['recovered_stale'])} interrupted run(s) marked STALE")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_NO
    print("\nno problems found")
    return EXIT_OK
