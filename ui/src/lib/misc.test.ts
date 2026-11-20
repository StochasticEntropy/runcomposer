import { describe, expect, it } from "vitest";

import { absoluteTime, elapsedMs, formatDuration, naturalCompare, parseLabel, relativeTime, sumCounts } from "./format.ts";
import { createTranslator } from "./i18n.ts";
import { composeHref, formatHash, parseHash, runHref, runsHref } from "./router.ts";

describe("router", () => {
  it("reads the four views, a run id and the query", () => {
    expect(parseHash("")).toMatchObject({ name: "compose", query: {} });
    expect(parseHash("#/")).toMatchObject({ name: "compose" });
    expect(parseHash("#/compose?f=Payments+AND+Smoke")).toEqual({ name: "compose", id: "", query: { f: "Payments AND Smoke" } });
    expect(parseHash("#/runs?q=nightly&offset=25")).toEqual({ name: "runs", id: "", query: { q: "nightly", offset: "25" } });
    expect(parseHash("#/runs/01ABC?tab=activity")).toEqual({ name: "run", id: "01ABC", query: { tab: "activity" } });
    expect(parseHash("#/inbox")).toMatchObject({ name: "inbox" });
  });

  it("knows what it does not know", () => {
    expect(parseHash("#/nowhere").name).toBe("notfound");
    expect(parseHash("#/runs/01ABC/extra").name).toBe("notfound");
    expect(parseHash("#/compose/extra").name).toBe("notfound");
  });

  it("writes links that read back, leaving out what is empty", () => {
    expect(formatHash("/runs", { q: "", state: null, offset: undefined })).toBe("#/runs");
    const href = composeHref({ f: "Payments AND (Smoke OR Regression)", from: null });
    expect(parseHash(href).query).toEqual({ f: "Payments AND (Smoke OR Regression)" });
    expect(parseHash(runHref("01 A/B", { tab: "selection" }))).toEqual({ name: "run", id: "01 A/B", query: { tab: "selection" } });
    expect(runsHref({ offset: 50 })).toBe("#/runs?offset=50");
  });
});

describe("format", () => {
  it("writes durations a person reads at a glance", () => {
    expect(formatDuration(950)).toBe("950 ms");
    expect(formatDuration(1240)).toBe("1.2 s");
    expect(formatDuration(1240, "de")).toBe("1,2 s");
    expect(formatDuration(42_000)).toBe("42 s");
    expect(formatDuration(75_000)).toBe("1 min 15 s");
    expect(formatDuration(120_000)).toBe("2 min");
    expect(formatDuration(3_780_000)).toBe("1 h 3 min");
    expect(formatDuration(null)).toBe("–");
    expect(formatDuration(0, "en", { coarse: true })).toBe("< 1 s");
    expect(formatDuration(7000, "en", { coarse: true })).toBe("7.0 s");
    expect(elapsedMs("2026-03-01T12:00:00Z", "2026-03-01T12:00:22Z")).toBe(22_000);
    expect(elapsedMs("2026-03-01T12:00:00Z", null)).toBeNull();
  });

  it("writes relative times in the viewer's language", () => {
    const now = Date.parse("2026-03-01T12:00:00Z");
    expect(relativeTime("2026-03-01T11:57:00Z", now, "en")).toBe("3 minutes ago");
    expect(relativeTime("2026-03-01T11:57:00Z", now, "de")).toBe("vor 3 Minuten");
    expect(relativeTime("2026-02-28T12:00:00Z", now, "en")).toBe("yesterday");
    expect(relativeTime("2026-03-01T12:00:00Z", now, "en")).toBe("now");
    expect(relativeTime("2026-03-01T11:59:20Z", now, "en")).toBe("now");
    expect(relativeTime("2026-03-01T11:59:20Z", now, "de")).toBe("jetzt");
    expect(relativeTime(null, now)).toBe("–");
    expect(relativeTime("not a time", now)).toBe("not a time");
    expect(absoluteTime("not a time")).toBe("not a time");
  });

  it("orders naturally and reads labels", () => {
    expect(["Sprint-10", "Sprint-2", "sprint-1"].sort(naturalCompare)).toEqual(["sprint-1", "Sprint-2", "Sprint-10"]);
    expect(sumCounts({ PASS: 3, FAIL: 2 })).toBe(5);
    expect(parseLabel("suite=nightly")).toEqual(["suite", "nightly"]);
    expect(parseLabel(" env = staging=eu ")).toEqual(["env", "staging=eu"]);
    expect(parseLabel("suite")).toBeNull();
    expect(parseLabel("=nightly")).toBeNull();
  });
});

describe("translator", () => {
  const messages = {
    hello: "Hello {name}",
    items: { one: "{count} item", other: "{count} items" },
    event: { run: { completed: "Completed" } },
    verdict: { PASS: "passed" },
  };

  it("resolves nested keys, placeholders and plural forms", () => {
    const { t } = createTranslator(messages, "en");
    expect(t("hello", { name: "there" })).toBe("Hello there");
    expect(t("items", { count: 1 })).toBe("1 item");
    expect(t("items", { count: 2500 })).toBe("2,500 items");
    expect(t("event.run.completed")).toBe("Completed");
    expect(createTranslator(messages, "de").t("items", { count: 2500 })).toBe("2.500 items");
  });

  it("is loud about a missing key and quiet about a missing server value", () => {
    const { t, tOr, has } = createTranslator(messages, "en");
    expect(t("no.such.key")).toBe("no.such.key");
    expect(tOr("verdict.PASS", "PASS")).toBe("passed");
    expect(tOr("verdict.NOT_A_STATUS", "NOT_A_STATUS")).toBe("NOT_A_STATUS");
    expect(has("verdict.PASS")).toBe(true);
    expect(has("verdict.NOT_A_STATUS")).toBe(false);
    expect(t("hello")).toBe("Hello {name}");
  });
});
