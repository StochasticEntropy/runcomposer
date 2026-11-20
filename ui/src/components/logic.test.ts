import { describe, expect, it } from "vitest";

import { ROOT_ID, fromAst, positivePatterns, toAst } from "../lib/editor.ts";
import { formatFilter } from "../lib/filter.ts";
import type { RunDetail, VerdictRow } from "../lib/types.ts";
import { failedCount, loadHref } from "../views/RunView.tsx";
import { plannedVerdicts } from "../views/RunsView.tsx";
import { resultRows, runProgress } from "./RunResults.tsx";
import { applyPicks } from "./TagPicker.tsx";

const verdict = (item_id: string, status: string, extra: Partial<VerdictRow> = {}): VerdictRow => ({
  item_id,
  status,
  duration_ms: 10,
  message: "",
  shard: "1",
  attempt: 1,
  live: false,
  ...extra,
});

describe("applyPicks", () => {
  const root = fromAst({ op: "AND", items: [{ op: "OR", items: ["Payments", "Checkout"] }, "Smoke"] });
  const initial = positivePatterns(root);

  it("a tick taken away takes that tag out of the filter", () => {
    const outcome = applyPicks(root, ROOT_ID, initial, new Set(["Checkout", "Smoke"]), "any");
    expect(outcome.removed).toEqual(["Payments"]);
    expect(outcome.added).toEqual([]);
    expect(formatFilter(toAst(outcome.root))).toBe("Checkout AND Smoke");
  });

  it("new ticks are added as one condition; nothing changes without a change", () => {
    const same = applyPicks(root, ROOT_ID, initial, new Set(initial), "any");
    expect(same.root).toBe(root);
    const more = applyPicks(root, ROOT_ID, initial, new Set([...initial, "Cart", "Catalog"]), "any");
    expect(more.added).toEqual(["Cart", "Catalog"]);
    expect(formatFilter(toAst(more.root))).toBe("(Payments OR Checkout) AND Smoke AND (Cart OR Catalog)");
  });

  it("falls back to the root when the target group was emptied away", () => {
    const groupId = root.children[0]!.id;
    const outcome = applyPicks(root, groupId, initial, new Set(["Smoke", "Auth"]), "any");
    expect(formatFilter(toAst(outcome.root))).toBe("Smoke AND Auth");
  });
});

describe("resultRows", () => {
  it("gives every row a key of its own under fan-out and retries", () => {
    const rows = resultRows([
      verdict("Shop.Auth.SSO.T001", "PASS", { shard: "chromium-1" }),
      verdict("Shop.Auth.SSO.T001", "PASS", { shard: "firefox-1" }),
      verdict("Shop.Auth.SSO.T002", "FAIL", { shard: "chromium-1", attempt: 1 }),
      verdict("Shop.Auth.SSO.T002", "PASS", { shard: "chromium-1", attempt: 2 }),
      verdict("Shop.Auth.SSO.T003", "PASS", { shard: "chromium-1", live: true }),
      verdict("Shop.Auth.SSO.T003", "PASS", { shard: "chromium-1", live: true }),
    ]);
    expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
  });

  it("puts failures first and earlier attempts last", () => {
    const rows = resultRows([
      verdict("Shop.Cart.Core.T001", "PASS"),
      verdict("Shop.Cart.Core.T002", "SKIP"),
      verdict("Shop.Cart.Core.T003", "MISSING", { message: "not-reported" }),
      verdict("Shop.Cart.Core.T010", "FAIL"),
      verdict("Shop.Cart.Core.T009", "FAIL"),
      verdict("Shop.Cart.Core.T004", "ERROR"),
      verdict("Shop.Cart.Core.T005", "FAIL", { attempt: 1 }),
      verdict("Shop.Cart.Core.T005", "PASS", { attempt: 2 }),
    ]);
    expect(rows.map((row) => `${row.status}:${row.item_id.slice(-4)}${row.superseded ? ":old" : ""}`)).toEqual([
      "FAIL:T009",
      "FAIL:T010",
      "ERROR:T004",
      "MISSING:T003",
      "SKIP:T002",
      "PASS:T001",
      "PASS:T005",
      "FAIL:T005:old",
    ]);
  });
});

const run = (extra: Partial<RunDetail>): RunDetail =>
  ({
    id: "01RUN",
    title: "Nightly regression",
    state: "RUNNING",
    planned_count: 3,
    verdict_summary: {},
    dispatches: [],
    dispatch_id: null,
    verdicts: [],
    shards: [],
    selection: { tag_filter: null, item_ids: [], derived_from: [] },
    ...extra,
  }) as RunDetail;

describe("runProgress", () => {
  it("counts delivered shards by status and live rows of the others, against what is owed", () => {
    const progress = runProgress(
      run({
        dispatch_id: "d1",
        dispatches: [{ dispatch_id: "d1", planned_verdicts: 6 }] as RunDetail["dispatches"],
        shards: [
          { shard: "chromium-1", declared: true, delivered: true, count: 3, planned: 3, summary: { PASS: 2, FAIL: 1 }, completion: "FAIL" },
          { shard: "firefox-1", declared: true, delivered: false, count: 0, planned: 3, summary: {}, completion: null },
        ],
        verdicts: [verdict("a", "PASS", { shard: "firefox-1", live: true }), verdict("b", "PASS", { shard: "chromium-1" })],
      }),
    );
    expect(progress).toEqual({ counts: { PASS: 2, FAIL: 1, RUNNING: 1 }, done: 4, planned: 6 });
  });

  it("falls back to the planned items when a runner declared no shard plan", () => {
    const progress = runProgress(run({ dispatch_id: "d1", dispatches: [{ dispatch_id: "d1", planned_verdicts: null }] as RunDetail["dispatches"] }));
    expect(progress).toEqual({ counts: {}, done: 0, planned: 3 });
    expect(plannedVerdicts(run({}))).toBe(3);
  });
});

describe("loading a run's selection back", () => {
  it("a filter alone travels as the filter", () => {
    const href = loadHref(run({ selection: { tag_filter: { op: "AND", items: ["Payments", { not: "prefix:Quarantine-" }] }, item_ids: [], derived_from: [] } }));
    expect(decodeURIComponent(href.replace(/\+/g, " "))).toBe("#/compose?f=Payments AND NOT prefix:Quarantine-");
  });

  it("fixed ids or a history query pin the run's items", () => {
    const fixed = loadHref(run({ selection: { tag_filter: "Payments", item_ids: ["Shop.Payments.Cards.T001"], derived_from: [] } }));
    expect(fixed).toBe("#/compose?f=Payments&from=01RUN");
    const derived = loadHref(run({ selection: { tag_filter: null, item_ids: [], derived_from: [{ resolved_run_id: "01OLD" }] } }));
    expect(derived).toBe("#/compose?from=01RUN");
  });

  it("counts what a rerun of the failures would carry", () => {
    expect(failedCount(run({ verdict_summary: { PASS: 10, FAIL: 2, ERROR: 1, MISSING: 3, SKIP: 4 } }))).toBe(6);
    expect(failedCount(run({ verdict_summary: { PASS: 10 } }))).toBe(0);
  });
});
