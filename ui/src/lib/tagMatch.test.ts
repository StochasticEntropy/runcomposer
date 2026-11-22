import { describe, expect, it } from "vitest";

import { editDistance, fromPython, matchesAnyTag, nearestTags, patternChecker, tagIndex } from "./tagMatch.ts";

const TAGS = ["Payments", "Payments-Cards", "Payments-Wallets", "Checkout", "Cart", "CartV2", "Smoke", "Regression", "Sprint-12", "Sprint-13", "Quarantine-Flaky"];
const index = tagIndex(TAGS);
// A slip of the fingers: two neighbouring letters of a word the wrong way round.
const slip = (word: string) => word.slice(0, 4) + word[5] + word[4] + word.slice(6);
const TYPO = slip("Payments");
const NO_SUCH_PREFIX = `prefix:${slip("Sprint-")}`;

describe("does any tag match", () => {
  it("a plain tag, without regard to case — as the server compares it", () => {
    expect(matchesAnyTag("Payments", index)).toBe(true);
    expect(matchesAnyTag("payments", index)).toBe(true);
    expect(matchesAnyTag(TYPO, index)).toBe(false);
    // A tag is the whole tag: the beginning of one is no match.
    expect(matchesAnyTag("Pay", index)).toBe(false);
    expect(matchesAnyTag("Payments AND Smoke", index)).toBe(false);
  });

  it("a prefix and a regular expression, searched inside each tag, case-sensitive", () => {
    expect(matchesAnyTag("prefix:Sprint-", index)).toBe(true);
    expect(matchesAnyTag("prefix:sprint-", index)).toBe(false);
    expect(matchesAnyTag("prefix:print", index)).toBe(false);
    expect(matchesAnyTag("regex:print-1[23]$", index)).toBe(true);
    expect(matchesAnyTag("regex:^Cart(V2)?$", index)).toBe(true);
    expect(matchesAnyTag("regex:^Sprint-2\\d$", index)).toBe(false);
    // `prefix:` is `regex:^…` by definition, not by escaping.
    expect(matchesAnyTag("prefix:Cart(V2)?$", index)).toBe(true);
  });

  it("claims nothing about an expression the browser cannot read", () => {
    expect(matchesAnyTag("regex:(?i)payments", index)).toBeNull();
    expect(matchesAnyTag("regex:(", index)).toBeNull();
  });
});

describe("a Python expression read as Python reads it", () => {
  // [expression, a tag, does Python's re.search find it (each checked against Python's re.search)]
  const SAME: [string, string, boolean][] = [
    ["\\ACart", "Cart", true],
    ["\\ACart", "Checkout", false],
    ["Cart\\Z", "Cart", true],
    ["Cart\\Z", "CartV2", false],
    ["Cart$", "Cart\n", true],
    ["Cart\\Z", "Cart\n", false],
    ["^Cart.$", "Cart\r", true],
    ["^Cart.$", "Cart\n", false],
    ["^\\w+$", "Zahlung-Übersicht", false],
    ["^\\w+$", "Übersicht", true],
    ["^\\d+$", "١٢", true],
    ["\\bCards\\b", "Payments-Cards", true],
    ["\\bSmoke\\b", "ÜSmoke", false],
    ["^Sprint-\\s?1", "Sprint- 1", true],
    ["{Cart}", "{Cart}", true],
    ["^a{,2}$", "aa", true],
    ["^a{,2}$", "aaa", false],
    ["^CartV{,}2$", "CartV2", true],
    ["^Cart{,}$", "Car", true],
    ["^Cart{,}$", "Cart{,}", false],
    ["^a{}$", "a{}", true],
    ["^Cart{1}V2$", "CartV2", true],
    ["[]x]", "]", true],
    ["[^]x]", "]", false],
    ["\\-Cards", "Payments-Cards", true],
    ["(?<=Payments-)Cards", "Payments-Cards", true],
    ["^(Cart|Checkout)$", "Checkout", true],
    ["\\x43art", "Cart", true],
    ["(a)\\1", "aa", true],
  ];
  it.each(SAME)("%s on %j is %s", (expression, tag, expected) => {
    const translated = fromPython(expression);
    expect(translated).not.toBeNull();
    expect(new RegExp(translated!, "u").test(tag)).toBe(expected);
  });

  const NOT_HERE = [
    "(?i)cart", "Cart(?i)", "(?s)Cart.", "a*+", "a++", "a?+", "a{2}+", "(?>Cart)", "(?P<area>Cart)", "(?P=area)",
    "(?#note)Cart", "(?(1)a|b)", "\\N{LATIN SMALL LETTER A}", "Cart\\z", "[\\W]", "[\\b]", "(?<area>Cart)", "[Cart",
  ];
  it.each(NOT_HERE)("%s is not translated, and no hint is shown for it", (expression) => {
    expect(fromPython(expression)).toBeNull();
    expect(matchesAnyTag(`regex:${expression}`, index)).toBeNull();
  });

  it("a tag Python finds is never reported as matching nothing", () => {
    expect(matchesAnyTag("regex:\\ACart", index)).toBe(true);
    expect(matchesAnyTag("regex:Smoke\\Z", index)).toBe(true);
  });
});

describe("which tags were probably meant", () => {
  it("counts a swap of neighbours as one slip", () => {
    expect(editDistance(TYPO.toLowerCase(), "payments", 2)).toBe(1);
    expect(editDistance("smoke", "smoke", 2)).toBe(0);
    expect(editDistance("cart", "checkout", 2)).toBe(3);
  });

  it("offers the closest spelling first, then tags the word begins", () => {
    expect(nearestTags(TYPO, index)).toEqual(["Payments"]);
    expect(nearestTags("smok", index)).toEqual(["Smoke"]);
    expect(nearestTags("Sprint-1", index)).toEqual(["Sprint-12", "Sprint-13"]);
    expect(nearestTags("Pay", index)).toEqual(["Payments", "Payments-Cards", "Payments-Wallets"]);
    expect(nearestTags("nothing like any of them", index)).toEqual([]);
    expect(nearestTags("   ", index)).toEqual([]);
  });
});

describe("checking the rules of a filter against a catalog", () => {
  it("says what matches nothing and, for a tag, what was probably meant", () => {
    const check = patternChecker(TAGS);
    expect(check("Payments")).toEqual({ matches: true, nearest: [] });
    expect(check(TYPO)).toEqual({ matches: false, nearest: ["Payments"] });
    // A pattern is not a misspelled tag: nothing is offered for it.
    expect(check(NO_SUCH_PREFIX)).toEqual({ matches: false, nearest: [] });
    expect(check("regex:(?i)x")).toEqual({ matches: null, nearest: [] });
    // Asked again, it is the same answer object — nothing is computed twice.
    expect(check(TYPO)).toBe(check(TYPO));
  });

  it("answers for a catalog of thousands of tags without anybody waiting", () => {
    const areas = ["Payments", "Checkout", "Cart", "Catalog", "Auth"];
    const many = Array.from({ length: 1600 }, (_, position) => `${areas[position % areas.length]}-${Math.floor(position / areas.length) + 1}`);
    const started = performance.now();
    const check = patternChecker(many);
    const typos = [slip("Payments-12"), slip("Checkout-7"), "Regression".replace("ss", "s")];
    const rules = [...many.slice(0, 20), ...typos, "Cart-9999", "prefix:Auth-3", "regex:^Catalog-1\\d$", `prefix:${slip("Catalog")}-`, slip("Catalog-3"), "auth-17", "Smoke"];
    const answers = rules.map(check);
    const elapsed = performance.now() - started;
    expect(answers.filter((answer) => answer.matches === false)).toHaveLength(7);
    expect(check(typos[0]!).nearest[0]).toBe("Payments-12");
    expect(check(typos[1]!).nearest[0]).toBe("Checkout-7");
    // Thirty rules against 1,600 tags: a few milliseconds of work. The bound is loose on purpose.
    expect(elapsed).toBeLessThan(250);
  });
});
