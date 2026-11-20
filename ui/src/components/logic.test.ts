import { describe, expect, it } from "vitest";

import { ROOT_ID, fromAst, positivePatterns, toAst } from "../lib/editor.ts";
import { formatFilter } from "../lib/filter.ts";
import type { RunDetail, VerdictRow } from "../lib/types.ts";
import { failedItems, hasFailures, loadHref, runDuration } from "../views/RunView.tsx";
import { plannedVerdicts } from "../views/RunsView.tsx";
import { reportedOnly, resultRows, runProgress } from "./RunResults.tsx";
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
        dispatches: [{ dispatch_id: "d1", planned_verdicts: 6, state: "RUNNING" }] as RunDetail["dispatches"],
        shards: [
          { shard: "chromium-1", declared: true, delivered: true, count: 3, planned: 3, summary: { PASS: 2, FAIL: 1 }, completion: "FAIL" },
          { shard: "firefox-1", declared: true, delivered: false, count: 0, planned: 3, summary: {}, completion: null },
        ],
        // firefox-1 has not delivered: one item passed live, one is running right now.
        live_summary: { "firefox-1": { PASS: 1, RUNNING: 1 } },
        verdicts: [
          verdict("a", "PASS", { shard: "firefox-1", live: true }),
          verdict("c", "RUNNING", { shard: "firefox-1", live: true }),
          verdict("b", "PASS", { shard: "chromium-1" }),
        ],
      }),
    );
    // RUNNING is work in progress: shown as running, never counted as a result.
    expect(progress).toEqual({ counts: { PASS: 3, FAIL: 1 }, done: 4, planned: 6, running: 1 });
    expect(reportedOnly({ PASS: 1, RUNNING: 2 })).toEqual({ PASS: 1 });
  });

  it("falls back to the planned items when a runner declared no shard plan", () => {
    const progress = runProgress(
      run({ dispatch_id: "d1", dispatches: [{ dispatch_id: "d1", planned_verdicts: null, state: "RUNNING" }] as RunDetail["dispatches"] }),
    );
    expect(progress).toEqual({ counts: {}, done: 0, planned: 3, running: 0 });
    expect(plannedVerdicts(run({}))).toBe(3);
  });
});

describe("a dispatch that is over", () => {
  const cancelled = run({
    state: "CANCELLED",
    dispatch_id: "d1",
    dispatches: [{ dispatch_id: "d1", planned_verdicts: 3, state: "CANCELLED", summary: { MISSING: 3 } }] as unknown as RunDetail["dispatches"],
    shards: [{ shard: "chromium-1", declared: true, delivered: false, count: 0, planned: 2, summary: {}, completion: null }],
    verdicts: [
      verdict("Shop.Auth.SSO.T001", "PASS", { shard: "chromium-1", live: true }),
      verdict("Shop.Auth.SSO.T002", "RUNNING", { shard: "chromium-1", live: true }),
      verdict("Shop.Auth.SSO.T001", "MISSING", {
        shard: "chromium-1",
        message: "not-reported",
        last_live: { status: "PASS", duration_ms: 12, message: "", shard: "chromium-1" },
      }),
      verdict("Shop.Auth.SSO.T002", "MISSING", {
        shard: "chromium-1",
        message: "not-reported",
        last_live: { status: "RUNNING", duration_ms: 0, message: "", shard: "chromium-1" },
      }),
      verdict("Shop.Auth.SSO.T003", "MISSING", { shard: "chromium-1", message: "not-reported", last_live: null }),
    ],
  });

  it("counts only what was delivered — a live observation is not a result", () => {
    expect(runProgress(cancelled)).toEqual({ counts: { MISSING: 3 }, done: 0, planned: 3, running: 0 });
  });

  it("shows one row per item: the verdict that counts, with what was seen live as a note", () => {
    const rows = resultRows(cancelled.verdicts, true);
    expect(rows.map((row) => [row.item_id, row.status, row.live, row.seenLive])).toEqual([
      ["Shop.Auth.SSO.T001", "MISSING", false, "PASS"],
      ["Shop.Auth.SSO.T002", "MISSING", false, "RUNNING"],
      ["Shop.Auth.SSO.T003", "MISSING", false, null],
    ]);
    // While the dispatch still runs, live rows are rows — what is running comes before what passed.
    expect(resultRows(cancelled.verdicts.slice(0, 2)).map((row) => [row.status, row.live])).toEqual([
      ["RUNNING", true],
      ["PASS", true],
    ]);
  });

  it("measures its duration from its own start, not from the run's first dispatch", () => {
    const twice = run({
      dispatches: [
        { dispatch_id: "d1", created_at: "2026-03-01T12:00:00Z", completed_at: "2026-03-01T12:00:06Z" },
        { dispatch_id: "d2", created_at: "2026-03-01T12:13:00Z", completed_at: "2026-03-01T12:13:07Z" },
      ] as RunDetail["dispatches"],
    });
    expect(runDuration(twice)).toBe(7000);
    expect(runDuration(run({}))).toBeNull();
  });
});

describe("loading a run's selection back", () => {
  it("a filter alone travels as the filter", () => {
    const href = loadHref(run({ selection: { tag_filter: { op: "AND", items: ["Payments", { not: "prefix:Quarantine-" }] }, item_ids: [], derived_from: [] } }));
    expect(decodeURIComponent(href.replace(/\+/g, " "))).toBe("#/compose?f=Payments AND NOT prefix:Quarantine-&like=01RUN");
  });

  it("fixed ids or a history query pin the run's items", () => {
    const fixed = loadHref(run({ selection: { tag_filter: "Payments", item_ids: ["Shop.Payments.Cards.T001"], derived_from: [] } }));
    expect(fixed).toBe("#/compose?f=Payments&from=01RUN&like=01RUN");
    const derived = loadHref(run({ selection: { tag_filter: null, item_ids: [], derived_from: [{ resolved_run_id: "01OLD" }] } }));
    expect(derived).toBe("#/compose?from=01RUN&like=01RUN");
  });

  it("counts the ITEMS a rerun of the failures would carry, not the verdicts", () => {
    const dispatches = [{ dispatch_id: "d1" }] as RunDetail["dispatches"];
    const fannedOut = run({
      dispatch_id: "d1",
      dispatches,
      verdict_summary: { PASS: 3, FAIL: 2, MISSING: 1 },
      verdicts: [
        verdict("Shop.Auth.SSO.T001", "FAIL", { shard: "chromium-1" }),
        verdict("Shop.Auth.SSO.T001", "FAIL", { shard: "firefox-1" }),
        verdict("Shop.Auth.SSO.T002", "MISSING", { shard: "firefox-1" }),
        verdict("Shop.Auth.SSO.T002", "PASS", { shard: "chromium-1" }),
        verdict("Shop.Auth.SSO.T003", "FAIL", { shard: "chromium-1", attempt: 1 }),
        verdict("Shop.Auth.SSO.T003", "PASS", { shard: "chromium-1", attempt: 2 }),
        verdict("Shop.Auth.SSO.T003", "PASS", { shard: "firefox-1" }),
      ],
    });
    expect(hasFailures(fannedOut)).toBe(true);
    expect(failedItems(fannedOut)).toBe(2);
    // An earlier dispatch on screen says nothing about the latest one.
    expect(failedItems(run({ dispatch_id: "d0", dispatches, verdict_summary: { FAIL: 1 } }))).toBeNull();
    expect(hasFailures(run({ verdict_summary: { PASS: 10, SKIP: 2 } }))).toBe(false);
  });
});
