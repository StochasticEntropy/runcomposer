import { describe, expect, it } from "vitest";

import { SAVED_KEY, deleteSelection, loadSaved, renameSelection, saveSelection } from "./saved.ts";
import type { SavedSelection } from "./saved.ts";

function memory(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
  };
}

const entry = (name: string, filter = "Payments AND Smoke"): SavedSelection => ({
  name,
  filter,
  excluded: ["Shop.Payments.Cards.T002"],
  runner: "demo",
  params: { seed: "a" },
  title: "Payments smoke",
  labels: { suite: "adhoc" },
  savedAt: "2026-03-01T12:00:00Z",
});

describe("saved selections", () => {
  it("are kept by name, replaced when saved again, and read back whole", () => {
    const storage = memory();
    expect(loadSaved(storage)).toEqual([]);
    expect(saveSelection(entry("Smoke 10"), storage).ok).toBe(true);
    saveSelection(entry("Smoke 2"), storage);
    saveSelection(entry(" Smoke 2 ", "Checkout"), storage);
    const list = loadSaved(storage);
    // Sorted naturally, one per name, the later save wins.
    expect(list.map((saved) => [saved.name, saved.filter])).toEqual([
      ["Smoke 2", "Checkout"],
      ["Smoke 10", "Payments AND Smoke"],
    ]);
    expect(list[1]).toEqual(entry("Smoke 10"));
  });

  it("can be renamed — not onto another one, not to nothing — and deleted", () => {
    const storage = memory();
    saveSelection(entry("Every night"), storage);
    saveSelection(entry("Smoke"), storage);
    expect(renameSelection("Every night", "Smoke", storage).ok).toBe(false);
    expect(renameSelection("Every night", "  ", storage).ok).toBe(false);
    expect(renameSelection("Every night", "Nightly regression", storage).list.map((saved) => saved.name)).toEqual(["Nightly regression", "Smoke"]);
    expect(deleteSelection("Smoke", storage).list.map((saved) => saved.name)).toEqual(["Nightly regression"]);
  });

  it("work without storage: nothing is saved, nothing breaks", () => {
    expect(loadSaved(null)).toEqual([]);
    expect(saveSelection(entry("Smoke"), null)).toEqual({ ok: false, list: [] });
    const refusing = { getItem: () => null, setItem: () => { throw new Error("quota"); } };
    expect(saveSelection(entry("Smoke"), refusing).ok).toBe(false);
    const throwing = { getItem: () => { throw new Error("denied"); }, setItem: () => undefined };
    expect(loadSaved(throwing)).toEqual([]);
  });

  it("ignores what is not a saved selection, and fills in what an older entry lacks", () => {
    const storage = memory({ [SAVED_KEY]: JSON.stringify([{ name: "From last year", filter: "Cart" }, { nonsense: true }, "text"]) });
    expect(loadSaved(storage)).toEqual([{ name: "From last year", filter: "Cart", excluded: [], params: {}, labels: {}, title: "", runner: null }]);
    expect(loadSaved(memory({ [SAVED_KEY]: "{not json" }))).toEqual([]);
    expect(loadSaved(memory({ [SAVED_KEY]: '{"a":1}' }))).toEqual([]);
  });
});
