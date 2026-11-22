// @vitest-environment jsdom
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { Artifact, GroupSummary, RunDetail, ShardSummary, VerdictRow } from "../lib/types.ts";
import { renderWithI18n, translator } from "../test/render.tsx";
import { RunResults, attention, byAttention, rowFiles } from "./RunResults.tsx";

afterEach(cleanup);
// jsdom has no layout, so no media queries: the wide layout is what is tested.
beforeAll(() => {
  window.matchMedia = ((query: string) => ({ matches: false, media: query, addEventListener: () => undefined, removeEventListener: () => undefined })) as unknown as typeof window.matchMedia;
});

const DISPATCH = "d-1";
const lane = (index: number) => `lane-${String(index).padStart(2, "0")}`;

const artifact = (shard: string, file: string, media: string): Artifact => ({
  name: `${file} (${shard})`,
  media_type: media,
  dispatch_id: DISPATCH,
  shard,
  // Over HTTP: relative to the artifact directory, never a server path.
  url_or_path: `r-1/${DISPATCH}/${shard}/${file}`,
  kind: "local",
  href: `/artifacts/r-1/${DISPATCH}/${shard}/${file}`,
});

/** A run of `count` shards with two items each: one shard has a failure, one has not delivered yet. */
function run(count: number, options: { failing: number; pending: number | null; groups?: boolean; state?: string }): RunDetail {
  const shards: ShardSummary[] = [];
  const verdicts: VerdictRow[] = [];
  const artifacts: Artifact[] = [];
  const shardGroups: Record<string, string> = {};
  for (let index = 1; index <= count; index += 1) {
    const name = lane(index);
    const delivered = index !== options.pending;
    const failed = index === options.failing;
    shards.push({
      shard: name,
      declared: true,
      delivered,
      count: delivered ? 2 : 0,
      planned: 2,
      summary: delivered ? (failed ? { PASS: 1, FAIL: 1 } : { PASS: 2 }) : {},
      completion: delivered ? (failed ? "FAIL" : "PASS") : null,
    });
    if (options.groups) shardGroups[name] = index % 2 === 0 ? "south" : "north";
    if (!delivered) continue;
    for (const item of [1, 2]) {
      verdicts.push({
        item_id: `shop.checkout.${name}.check ${item}`,
        status: failed && item === 2 ? "FAIL" : "PASS",
        duration_ms: 5,
        message: failed && item === 2 ? "total is 12.00, expected 11.50" : "",
        shard: name,
        attempt: 1,
        live: false,
      });
    }
    artifacts.push(artifact(name, "log.html", "text/html"), artifact(name, "report.html", "text/html"), artifact(name, "output.xml", "application/xml"));
  }
  const groups: GroupSummary[] = options.groups
    ? ["north", "south"].map((group) => {
        const members = shards.filter((entry) => shardGroups[entry.shard] === group);
        const summary: Record<string, number> = {};
        for (const member of members) for (const [status, value] of Object.entries(member.summary)) summary[status] = (summary[status] ?? 0) + value;
        return {
          group,
          shards: members.map((entry) => entry.shard),
          declared_shards: members.length,
          delivered_shards: members.filter((entry) => entry.delivered).length,
          planned: members.length * 2,
          count: members.reduce((sum, entry) => sum + entry.count, 0),
          summary,
          live: {},
          completion: members.every((entry) => entry.delivered) ? (summary.FAIL ? "FAIL" : "PASS") : null,
        };
      })
    : [];
  return {
    id: "r-1",
    dispatch_id: DISPATCH,
    dispatches: [{ dispatch_id: DISPATCH, state: options.state ?? "RUNNING", mode: "pool", links: {}, shards: shards.map((entry) => entry.shard) }],
    verdicts,
    shards,
    groups,
    shard_groups: shardGroups,
    live_summary: {},
    artifacts,
  } as unknown as RunDetail;
}

const show = (detail: RunDetail) => renderWithI18n(<RunResults run={detail} query={{}} onQuery={() => undefined} />);
const cardsOf = (scope: Element) => [...scope.querySelectorAll(".shard-card")].map((card) => card.getAttribute("data-shard"));

describe("artifacts", () => {
  it("are linked by the URL the server serves them under, and the rest is named without a server path", () => {
    const detail = run(1, { failing: 1, pending: null, state: "COMPLETE" });
    detail.artifacts.push(
      { name: "console.txt", media_type: "text/plain", dispatch_id: DISPATCH, shard: lane(1), url_or_path: "console.txt", kind: "external-path", href: null },
      { name: "agent log", media_type: "text/plain", dispatch_id: DISPATCH, shard: lane(1), url_or_path: "agent.log", kind: "external-path", href: null },
      { name: "build", media_type: "text/html", dispatch_id: DISPATCH, shard: lane(1), url_or_path: "https://ci.shop.example/job/shop-nightly/7/", kind: "url", href: "https://ci.shop.example/job/shop-nightly/7/" },
      { name: "screenshots", media_type: "inode/directory", dispatch_id: DISPATCH, shard: lane(1), url_or_path: `r-1/${DISPATCH}/${lane(1)}/screenshots`, kind: "directory", href: null },
    );
    const { container } = show(detail);
    const card = container.querySelector(`.shard-card[data-shard="${lane(1)}"]`)!;
    expect(within(card as HTMLElement).getByRole("link", { name: /^log\.html/ }).getAttribute("href")).toBe(`/artifacts/r-1/${DISPATCH}/${lane(1)}/log.html`);
    expect(within(card as HTMLElement).getByRole("link", { name: /^build/ }).getAttribute("href")).toBe("https://ci.shop.example/job/shop-nightly/7/");
    const external = [...card.querySelectorAll('[data-artifact-kind="external-path"]')].map((entry) => entry.textContent);
    // A bare file name that is the name is said once; another one is shown beside it.
    expect(external).toEqual(["console.txt", "agent log: agent.log"]);
    expect(card.querySelector('[data-artifact-kind="directory"]')!.textContent).toBe("screenshots");
    expect(card.textContent).not.toContain("/somewhere");
  });
});

describe("a dispatch that is over", () => {
  it("lists what an item was last seen doing on its MISSING row, and nothing as running", () => {
    const detail = run(1, { failing: 0, pending: 1, state: "CANCELLED" });
    // As the server answers now: no live rows, no RUNNING in the live summary — what was seen is `last_live`.
    detail.verdicts = [
      { item_id: "shop.checkout.lane-01.check 1", status: "MISSING", duration_ms: 0, message: "", shard: lane(1), attempt: 1, live: false, last_live: { status: "RUNNING", duration_ms: 0, message: "", shard: lane(1) } },
      { item_id: "shop.checkout.lane-01.check 2", status: "MISSING", duration_ms: 0, message: "", shard: lane(1), attempt: 1, live: false, last_live: null },
    ];
    detail.live_summary = {};
    const { container } = show(detail);
    const rows = [...container.querySelectorAll(".verdict-table tbody tr[data-item-id]")];
    expect(rows.map((row) => row.getAttribute("data-status"))).toEqual(["MISSING", "MISSING"]);
    expect(container.textContent).toContain(translator.t("run.lastLiveRunning"));
    expect(container.querySelector(".live-dot")).toBeNull();
  });
});

describe("what needs a look comes first", () => {
  it("ranks a part: something failed or never came, then still at work, then all well", () => {
    expect(attention({ summary: { PASS: 3, FAIL: 1 }, done: true }, false)).toBe(0);
    expect(attention({ summary: { MISSING: 2 }, done: true }, true)).toBe(0);
    expect(attention({ summary: { PASS: 1 }, done: false }, false)).toBe(1);
    // A part that never delivered, of a dispatch that is over, did not pass.
    expect(attention({ summary: {}, done: false }, true)).toBe(0);
    expect(attention({ summary: { PASS: 4, SKIP: 1 }, done: true }, true)).toBe(2);
  });

  it("orders by that rank, and by name within it — the way names with numbers are read", () => {
    const parts = [
      { name: "lane-10", rank: 2 },
      { name: "lane-9", rank: 2 },
      { name: "lane-30", rank: 0 },
      { name: "lane-2", rank: 1 },
    ];
    expect(byAttention(parts, (part) => part.rank, (part) => part.name).map((part) => part.name)).toEqual(["lane-30", "lane-2", "lane-9", "lane-10"]);
  });

  it("offers a row the readable files of its own shard, by their short names", () => {
    const files = [artifact("lane-01", "output.xml", "application/xml"), artifact("lane-01", "log.html", "text/html"), artifact("lane-01", "report.html", "text/html"), artifact("lane-02", "log.html", "text/html")];
    expect(rowFiles(files, "lane-01")).toEqual([
      { label: "log", href: "/artifacts/r-1/d-1/lane-01/log.html" },
      { label: "report", href: "/artifacts/r-1/d-1/lane-01/report.html" },
    ]);
    // Nothing a browser can show: the first files there are, under their own names.
    expect(rowFiles([artifact("lane-03", "results.json", "application/json")], "lane-03")).toEqual([{ label: "results.json", href: "/artifacts/r-1/d-1/lane-03/results.json" }]);
    // A recorded directory has no link, so it is nothing to offer.
    expect(rowFiles([{ ...artifact("lane-04", "files", "inode/directory"), href: null }], "lane-04")).toEqual([]);
  });
});

describe("a run of fifty shards", () => {
  it("shows the failed shard first, then the one still running — and folds the 48 without a failure into one line", () => {
    const { container } = show(run(50, { failing: 37, pending: 12 }));
    const section = screen.getByRole("heading", { name: translator.t("run.shards", { count: 50 }) }).parentElement!;
    const folded = section.querySelector("details.passed-shards") as HTMLDetailsElement;
    // In reading order: the failure, the shard in flight, then everything that is fine — folded.
    expect(cardsOf(section).slice(0, 3)).toEqual([lane(37), lane(12), lane(1)]);
    expect(cardsOf(folded)).toHaveLength(48);
    expect(folded.open).toBe(false);
    expect(within(folded).getByText(translator.t("run.passedShards", { count: 48 }))).toBeTruthy();
    expect([...section.querySelectorAll(":scope > ul .shard-card")].map((card) => card.getAttribute("data-shard"))).toEqual([lane(37), lane(12)]);

    // The first result row is the failure, and it carries the log and the report of its own shard.
    const first = container.querySelector(".verdict-table tbody tr[data-item-id]")!;
    expect(first.getAttribute("data-status")).toBe("FAIL");
    const links = [...first.querySelectorAll(".col-files a")] as HTMLAnchorElement[];
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["log", `/artifacts/r-1/d-1/${lane(37)}/log.html`],
      ["report", `/artifacts/r-1/d-1/${lane(37)}/report.html`],
    ]);
    expect(links[0]!.getAttribute("aria-label")).toBe(translator.t("run.fileOf", { file: "log", item: first.getAttribute("data-item-id")! }));
  });

  it("with nothing wrong, is one folded line — not fifty cards", () => {
    show(run(50, { failing: 0, pending: null, state: "COMPLETE" }));
    const section = screen.getByRole("heading", { name: translator.t("run.shards", { count: 50 }) }).parentElement!;
    expect(section.querySelectorAll(":scope > ul .shard-card")).toHaveLength(0);
    const folded = section.querySelector("details.passed-shards") as HTMLDetailsElement;
    expect(folded.open).toBe(false);
    expect(cardsOf(folded)).toHaveLength(50);
  });

  it("keeps a handful of shards as cards: there is nothing to fold", () => {
    show(run(4, { failing: 0, pending: null, state: "COMPLETE" }));
    const section = screen.getByRole("heading", { name: translator.t("run.shards", { count: 4 }) }).parentElement!;
    expect(section.querySelector("details.passed-shards")).toBeNull();
    expect(cardsOf(section)).toEqual([lane(1), lane(2), lane(3), lane(4)]);
  });

  it("keeps the outline: a shard's heading is one level below its group's, and right below the section's without groups", () => {
    const levels = (container: Element, selector: string) =>
      [...new Set([...container.querySelectorAll(`${selector} .shard-head > :is(h2, h3, h4, h5)`)].map((heading) => heading.localName))];
    const grouped = show(run(4, { failing: 2, pending: null, groups: true, state: "COMPLETE" })).container;
    expect(screen.getByRole("heading", { level: 2, name: translator.t("run.groups", { count: 2 }) })).toBeTruthy();
    expect([...grouped.querySelectorAll(".group-card > .shard-head > *:first-child")].map((heading) => heading.localName)).toEqual(["h3", "h3"]);
    expect(levels(grouped, ".group-card .shard-card")).toEqual(["h4"]);
    cleanup();
    const flat = show(run(4, { failing: 2, pending: null, state: "COMPLETE" })).container;
    expect(screen.getByRole("heading", { level: 2, name: translator.t("run.shards", { count: 4 }) })).toBeTruthy();
    expect(levels(flat, ".shard-card")).toEqual(["h3"]);
  });

  it("in groups: the group with the failure is first and open, the group that passed is folded", () => {
    const { container } = show(run(50, { failing: 38, pending: null, groups: true, state: "COMPLETE" }));
    const groups = [...container.querySelectorAll(".group-card")];
    expect(groups.map((group) => [group.getAttribute("data-group"), group.getAttribute("data-attention")])).toEqual([
      ["south", "0"],
      ["north", "2"],
    ]);
    const [south, north] = groups as [Element, Element];
    expect((south.querySelector("details.group-shards") as HTMLDetailsElement).open).toBe(true);
    // Inside the open group the failing shard is the one card; its 24 neighbours are a line.
    expect([...south.querySelectorAll("details.group-shards:not(.passed-shards) > ul .shard-card")].map((card) => card.getAttribute("data-shard"))).toEqual([lane(38)]);
    expect(cardsOf(south.querySelector("details.passed-shards")!)).toHaveLength(24);
    expect((north.querySelector("details.group-shards") as HTMLDetailsElement).open).toBe(false);
  });
});

describe("how much was reported", () => {
  /** A cancelled run in two groups: of each group's shards one delivered, the others never did. */
  function cancelled(): RunDetail {
    const detail = run(8, { failing: 0, pending: null, groups: true, state: "CANCELLED" });
    for (const shard of detail.shards) {
      // lane-01 and lane-02 delivered; the rest were cut off, and their items are MISSING.
      if (shard.shard === lane(1) || shard.shard === lane(2)) continue;
      Object.assign(shard, { delivered: false, count: 0, summary: { MISSING: 2 }, completion: null });
    }
    detail.verdicts = detail.verdicts.map((row) => (row.shard === lane(1) || row.shard === lane(2) ? row : { ...row, status: "MISSING", message: "not-reported" }));
    for (const group of detail.groups ?? []) {
      // As the server rolls a group up: MISSING is in the summary and in no count.
      Object.assign(group, { delivered_shards: 1, count: 2, summary: { PASS: 2, MISSING: 6 }, completion: null });
    }
    return detail;
  }

  it("is the same number in a group's line as in its shards' lines added up — never a MISSING item", () => {
    const { container } = show(cancelled());
    for (const group of container.querySelectorAll(".group-card")) {
      const line = group.querySelector('[data-testid="group-reported"]')!.textContent;
      expect(line).toContain(translator.t("run.shardProgress", { done: 2, total: 8 }));
      const perShard = [...group.querySelectorAll(".shard-card .shard-numbers")].map((entry) => Number.parseInt(entry.textContent ?? "", 10));
      expect(perShard).toHaveLength(4);
      expect(perShard.reduce((sum, value) => sum + value, 0)).toBe(2);
      // The bar's spoken label counts what was received, not what is missing.
      const bar = group.querySelector(".verdict-bar")!.getAttribute("aria-label")!;
      expect(bar.startsWith(translator.t("progress.label", { done: 2, total: 8 }))).toBe(true);
    }
  });

  it("while shards are at work, counts what they said live on top of what was delivered", () => {
    const detail = run(4, { failing: 0, pending: 3, groups: true });
    const north = detail.groups!.find((group) => group.group === "north")!;
    north.live = { PASS: 1, RUNNING: 1 };
    detail.live_summary = { [lane(3)]: { PASS: 1, RUNNING: 1 } };
    const { container } = show(detail);
    const card = container.querySelector('.group-card[data-group="north"]')!;
    // lane-01 delivered two; lane-03 has said one live and is running another.
    expect(card.querySelector('[data-testid="group-reported"]')!.textContent).toContain(translator.t("run.shardProgress", { done: 3, total: 4 }));
  });
});

describe("an item in the results", () => {
  it("is shown by the name people know it by, with its id beneath — and found by either", () => {
    const detail = run(3, { failing: 2, pending: null, state: "COMPLETE" });
    const failed = detail.verdicts.find((row) => row.status === "FAIL")!;
    const names = new Map([[failed.item_id, "Total after a voucher"]]);
    const view = renderWithI18n(<RunResults run={detail} names={names} query={{}} onQuery={() => undefined} />);
    const first = view.container.querySelector(".verdict-table tbody tr[data-item-id]")!;
    expect(first.querySelector(".item-name")!.textContent).toBe("Total after a voucher");
    expect(first.querySelector(".item-id")!.textContent).toBe(failed.item_id);
    // An item the catalog has no name for is its id, as before.
    const second = view.container.querySelectorAll(".verdict-table tbody tr[data-item-id]")[1]!;
    expect(second.querySelector(".item-name")).toBeNull();
    expect(second.querySelector(".col-item")!.textContent).toBe(second.getAttribute("data-item-id"));

    // The search reads the name as well as the id.
    const found = (q: string) => {
      cleanup();
      const { container } = renderWithI18n(<RunResults run={detail} names={names} query={{ q }} onQuery={() => undefined} />);
      return [...container.querySelectorAll(".verdict-table tbody tr[data-item-id]")].map((row) => row.getAttribute("data-item-id"));
    };
    expect(found("voucher")).toEqual([failed.item_id]);
    expect(found(failed.item_id.slice(-12))).toEqual([failed.item_id]);
  });
});

describe("a run where most shards failed", () => {
  /** Forty shards, every one of them with a failure. */
  function allFailing(): RunDetail {
    const detail = run(40, { failing: 0, pending: null, state: "COMPLETE" });
    for (const shard of detail.shards) Object.assign(shard, { summary: { PASS: 1, FAIL: 1 }, completion: "FAIL" });
    detail.verdicts = detail.verdicts.map((row, index) => (index % 2 === 1 ? { ...row, status: "FAIL", message: "total is 12.00, expected 11.50" } : row));
    return detail;
  }

  it("shows the first few as cards and the rest behind one line — the item list is not forty cards down", () => {
    show(allFailing());
    const section = screen.getByRole("heading", { name: translator.t("run.shards", { count: 40 }) }).parentElement!;
    expect(section.querySelectorAll(":scope > ul .shard-card")).toHaveLength(8);
    const more = section.querySelector("details.more-shards") as HTMLDetailsElement;
    expect(more.open).toBe(false);
    expect(more.querySelector("summary")!.textContent).toBe(translator.t("run.moreShards", { count: 32 }));
    expect(cardsOf(more)).toHaveLength(32);
    // In order: the cards continue where the visible ones stop.
    expect(cardsOf(section).slice(0, 9)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9].map(lane));
  });

  it("offers one step from the top to the failed results, whatever shard or group was chosen", () => {
    const asked: object[] = [];
    renderWithI18n(<RunResults run={allFailing()} query={{ shard: lane(3) }} onQuery={(changes) => asked.push(changes)} />);
    const jump = screen.getByTestId("jump-to-results");
    // The number is the run's, not the chosen shard's.
    expect(jump.textContent).toBe(translator.t("run.jumpFailed", { count: 40 }));
    fireEvent.click(jump);
    expect(asked).toEqual([{ status: "failed", shard: "", group: "" }]);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: translator.t("run.verdicts") }));
  });

  it("offers nothing to jump to when nothing failed", () => {
    show(run(12, { failing: 0, pending: null, state: "COMPLETE" }));
    expect(screen.queryByTestId("jump-to-results")).toBeNull();
  });
});
