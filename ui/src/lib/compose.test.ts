import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  IDLE_PREVIEW,
  MAX_LINKED_EXCLUSIONS,
  PreviewController,
  dispatchSelection,
  isFresh,
  linkFromQuery,
  linkQuery,
  previewRows,
  previewSelection,
  selectionKey,
  tristate,
} from "./compose.ts";
import type { PreviewState } from "./compose.ts";
import { naturalCompare } from "./format.ts";
import { ApiError } from "./request.ts";
import type { CatalogItem, CompileResult, Selection } from "./types.ts";

const item = (id: string, name = id, tags: string[] = [], hierarchy: string[] = ["Shop"]): CatalogItem => ({ id, name, tags, hierarchy });
const result = (items: CatalogItem[], total = 60): CompileResult => ({
  count: items.length,
  total,
  items,
  warnings: [],
  derived_from: [],
  snapshot: "sha256:0",
});

const CARDS = [item("Shop.Payments.Cards.T001"), item("Shop.Payments.Cards.T002"), item("Shop.Payments.Cards.T003")];

describe("what is sent", () => {
  it("previews the filter and the pinned ids, and nothing when there is neither", () => {
    expect(previewSelection(null, null)).toBeNull();
    expect(previewSelection(null, [])).toBeNull();
    expect(previewSelection("Payments", null)).toEqual({ tag_filter: "Payments" });
    expect(previewSelection("Payments", ["a"])).toEqual({ tag_filter: "Payments", item_ids: ["a"] });
    expect(previewSelection(null, ["a"])).toEqual({ item_ids: ["a"] });
  });

  it("dispatches the filter as it is while nothing matched is unticked", () => {
    expect(dispatchSelection("Payments", null, CARDS, new Set())).toEqual({ tag_filter: "Payments" });
    // An exclusion that hits nothing in the current match changes nothing.
    expect(dispatchSelection("Payments", null, CARDS, new Set(["Shop.Auth.SSO.T001"]))).toEqual({ tag_filter: "Payments" });
  });

  it("names the remaining ids once something matched is unticked", () => {
    expect(dispatchSelection("Payments", null, CARDS, new Set(["Shop.Payments.Cards.T002"]))).toEqual({
      tag_filter: "Payments",
      item_ids: ["Shop.Payments.Cards.T001", "Shop.Payments.Cards.T003"],
    });
  });
});

describe("PreviewController", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  /** A compile whose answers are released by hand, in any order. */
  function harness(debounceMs = 100) {
    const calls: { selection: Selection; resolve: (value: CompileResult) => void; reject: (error: unknown) => void; signal: AbortSignal }[] = [];
    const states: PreviewState[] = [];
    const controller = new PreviewController(
      (selection, signal) => new Promise<CompileResult>((resolve, reject) => calls.push({ selection, resolve, reject, signal })),
      (state) => states.push(state),
      debounceMs,
    );
    return { calls, states, controller };
  }

  it("an early answer that arrives late never replaces a later one", async () => {
    const { calls, controller } = harness();
    controller.request({ tag_filter: "Payments" });
    await vi.advanceTimersByTimeAsync(100);
    controller.request({ tag_filter: { op: "AND", items: ["Payments", "Smoke"] } });
    await vi.advanceTimersByTimeAsync(100);
    expect(calls).toHaveLength(2);
    expect(calls[0]!.signal.aborted).toBe(true);

    calls[1]!.resolve(result(CARDS.slice(0, 1)));
    await vi.runAllTimersAsync();
    expect(controller.current.result?.count).toBe(1);
    expect(isFresh(controller.current)).toBe(true);

    // The slow answer to the FIRST request shows up now — and is ignored.
    calls[0]!.resolve(result(CARDS, 17));
    await vi.runAllTimersAsync();
    expect(controller.current.result?.count).toBe(1);
    expect(controller.current.resultKey).toBe(selectionKey({ tag_filter: { op: "AND", items: ["Payments", "Smoke"] } }));
  });

  it("is not fresh from the moment the selection changes", async () => {
    const { calls, controller } = harness();
    controller.request({ tag_filter: "Payments" }, { immediate: true });
    calls[0]!.resolve(result(CARDS));
    await vi.runAllTimersAsync();
    expect(isFresh(controller.current)).toBe(true);

    controller.request({ tag_filter: "Smoke" });
    expect(controller.current.status).toBe("loading");
    expect(isFresh(controller.current)).toBe(false);
    // The old rows are still there to look at, but they are stale.
    expect(controller.current.result?.count).toBe(3);
  });

  it("a failed compile leaves the old rows stale, not current", async () => {
    const { calls, controller } = harness(0);
    controller.request({ tag_filter: "Payments" });
    calls[0]!.resolve(result(CARDS));
    await vi.runAllTimersAsync();

    controller.request({ tag_filter: "regex:(" });
    calls[1]!.reject(new ApiError(400, "invalid-filter", "tag_filter: invalid regular expression"));
    await vi.runAllTimersAsync();
    expect(controller.current.status).toBe("error");
    expect(controller.current.error?.code).toBe("invalid-filter");
    expect(controller.current.result?.count).toBe(3);
    expect(isFresh(controller.current)).toBe(false);
  });

  it("goes idle at once when the selection is emptied — nothing stays loading", async () => {
    const { calls, controller } = harness();
    controller.request({ tag_filter: "Payments" });
    controller.request(null);
    expect(controller.current).toEqual(IDLE_PREVIEW);
    await vi.advanceTimersByTimeAsync(500);
    expect(calls).toHaveLength(0);
    expect(controller.current.status).toBe("idle");
  });

  it("debounces typing into one request", async () => {
    const { calls, controller } = harness();
    for (const filter of ["P", "Pay", "Paym", "Payments"]) {
      controller.request({ tag_filter: filter });
      await vi.advanceTimersByTimeAsync(30);
    }
    await vi.advanceTimersByTimeAsync(100);
    expect(calls.map((call) => call.selection.tag_filter)).toEqual(["Payments"]);
  });

  it("retry asks again for the same selection", async () => {
    const { calls, controller } = harness(0);
    controller.request({ tag_filter: "Payments" });
    calls[0]!.reject(new ApiError(0, "network", "offline"));
    await vi.runAllTimersAsync();
    expect(controller.current.status).toBe("error");
    controller.retry();
    calls[1]!.resolve(result(CARDS));
    await vi.runAllTimersAsync();
    expect(isFresh(controller.current)).toBe(true);
  });

  it("can be used again after dispose", async () => {
    const { calls, controller } = harness(0);
    controller.request({ tag_filter: "Payments" });
    controller.dispose();
    controller.request({ tag_filter: "Payments" });
    expect(calls).toHaveLength(2);
  });
});

describe("the link", () => {
  it("carries filter, exclusions and the pinned run, and reads them back", () => {
    const link = { filter: "Payments AND Smoke", excluded: ["Shop.Payments.Cards.T002"], from: "01RUN" };
    const query = linkQuery(link);
    expect(query).toEqual({ f: "Payments AND Smoke", from: "01RUN", x: '["Shop.Payments.Cards.T002"]' });
    expect(linkFromQuery(query as Record<string, string>)).toEqual(link);
  });

  it("is absent when the URL says nothing about a selection", () => {
    expect(linkFromQuery({})).toBeNull();
    expect(linkFromQuery({ tab: "results" })).toBeNull();
    expect(linkFromQuery({ f: "" })).toEqual({ filter: "", excluded: [], from: null });
  });

  it("drops a long exclusion list from the link and survives a mangled one", () => {
    const many = Array.from({ length: MAX_LINKED_EXCLUSIONS + 1 }, (_, index) => `Shop.Cart.Core.T${index}`);
    expect(linkQuery({ filter: "Cart", excluded: many, from: null }).x).toBeNull();
    expect(linkFromQuery({ f: "Cart", x: "[not json" })).toEqual({ filter: "Cart", excluded: [], from: null });
  });
});

describe("previewRows", () => {
  const items = [
    item("Shop.Cart.Core.T010", "Remove item", ["Cart"], ["Shop", "Cart", "Core"]),
    item("Shop.Cart.Core.T002", "Add item", ["Cart", "Smoke"], ["Shop", "Cart", "Core"]),
    item("Shop.Auth.SSO.T001", "SSO login", ["Auth"], ["Shop", "Auth", "SSO"]),
  ];
  const options = { search: "", sort: "catalog" as const, grouped: false, compare: naturalCompare };

  it("keeps the catalog's order unless asked otherwise, and sorts naturally", () => {
    expect(previewRows(items, options).map((row) => row.item!.id)).toEqual(items.map((entry) => entry.id));
    expect(previewRows(items, { ...options, sort: "id" }).map((row) => row.item!.id)).toEqual([
      "Shop.Auth.SSO.T001",
      "Shop.Cart.Core.T002",
      "Shop.Cart.Core.T010",
    ]);
    expect(previewRows(items, { ...options, sort: "name" }).map((row) => row.item!.name)).toEqual(["Add item", "Remove item", "SSO login"]);
  });

  it("searches id, name and tags", () => {
    expect(previewRows(items, { ...options, search: "smoke" })).toHaveLength(1);
    expect(previewRows(items, { ...options, search: "sso" })).toHaveLength(1);
    expect(previewRows(items, { ...options, search: "shop." })).toHaveLength(3);
    expect(previewRows(items, { ...options, search: "nothing like it" })).toHaveLength(0);
  });

  it("groups by suite with a header row per suite", () => {
    const rows = previewRows(items, { ...options, grouped: true });
    expect(rows.map((row) => `${row.kind}:${row.label}`)).toEqual([
      "group:Shop › Cart › Core",
      "item:Shop.Cart.Core.T010",
      "item:Shop.Cart.Core.T002",
      "group:Shop › Auth › SSO",
      "item:Shop.Auth.SSO.T001",
    ]);
    expect(rows[0]!.members).toHaveLength(2);
    const sorted = previewRows(items, { ...options, grouped: true, sort: "name" });
    expect(sorted.filter((row) => row.kind === "group").map((row) => row.label)).toEqual(["Shop › Auth › SSO", "Shop › Cart › Core"]);
    expect(sorted.filter((row) => row.kind === "item").map((row) => row.item!.name)).toEqual(["SSO login", "Add item", "Remove item"]);
  });
});

describe("tristate", () => {
  it("is all, some or none", () => {
    expect(tristate(CARDS, new Set())).toBe("all");
    expect(tristate(CARDS, new Set([CARDS[0]!.id]))).toBe("some");
    expect(tristate(CARDS, new Set(CARDS.map((entry) => entry.id)))).toBe("none");
    expect(tristate([], new Set())).toBe("none");
  });
});
