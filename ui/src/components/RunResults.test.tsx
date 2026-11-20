// @vitest-environment jsdom
import { cleanup, screen, within } from "@testing-library/react";
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
  url_or_path: `/somewhere/${shard}/${file}`,
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

    // The first result row is the failure, and it carries the log and the report of ITS shard.
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
