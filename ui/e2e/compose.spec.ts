// Composing: the filter builder, the picker, and a preview that can be
// trusted — it is the answer to the filter on screen, or it says it is not.

import type { Page } from "@playwright/test";

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

/** The filter the address bar carries. */
const filterOf = (page: Page) => new URLSearchParams(new URL(page.url()).hash.split("?")[1] ?? "").get("f");

interface Compiled {
  count: number;
  total: number;
  items: { id: string; name: string }[];
}

test("the landing state says what is there and offers a way in", async ({ page, open, api }) => {
  await open();
  await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
  await expect(page.getByText(msg("compose.landing.catalog", { items: 60, tags: 47 }))).toBeVisible();
  // Nothing is selected, so nothing can be run — and the page says why.
  await expect(page.getByRole("button", { name: msg("compose.run", { count: 0 }) })).toBeDisabled();
  await expect(page.getByTestId("run-summary")).toHaveText(msg("compose.blocked.empty"));

  // A quick start carries the number of items behind it.
  await page.getByRole("button", { name: /^Payments · 17$/ }).click();
  await expectPreview(page, 17, 60);

  // The whole catalog is a selection too.
  await page.getByRole("button", { name: msg("filter.clear") }).click();
  await page.getByRole("button", { name: msg("compose.landing.all", { count: 60 }) }).click();
  await expectPreview(page, 60, 60);
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: msg("filter.everything") }) })).toBeVisible();
  const everything = await api.post<Compiled>("/selection/compile", { tag_filter: { not: "regex:^$" }, limit: 0 });
  expect(everything.count).toBe(60);
});

test("everything is written * — as a rule, as text, in the link and as the title of the run", async ({ page, open, api }) => {
  await open();
  await page.getByRole("button", { name: msg("compose.landing.all", { count: 60 }) }).click();
  await expectPreview(page, 60, 60);

  // One plain rule that says what it is: not a negation, not an error, nothing to negate.
  const rule = page.locator(".expr .chip");
  await expect(rule).toHaveCount(1);
  await expect(rule).toHaveAttribute("data-pattern", "*");
  await expect(rule).not.toHaveClass(/is-negated|is-invalid/);
  await expect(rule).toContainText(msg("filter.everything"));
  await expect(rule.locator("[aria-pressed]")).toHaveCount(0);
  // The way "everything" travels to the server shows nowhere on the page or in the link.
  await expect(page.getByTestId("filter-text")).toHaveText("*");
  await expect(page).toHaveURL(/#\/compose\?f=(\*|%2A)$/);
  await expect(page.locator(".compose")).not.toContainText("regex:^$");
  expect(decodeURIComponent(page.url())).not.toContain("regex");
  const title = page.getByRole("textbox", { name: msg("compose.title") });
  await expect(title).toHaveAttribute("placeholder", msg("filter.everything"));

  // As text it is `*`, and `*` typed is everything again.
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  const area = page.getByRole("textbox", { name: msg("filter.textLabel") });
  await expect(area).toHaveValue("*");
  await area.fill("Payments");
  await area.press("Enter");
  await expectPreview(page, 17, 60);
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  await area.fill("*");
  await area.press("Enter");
  await expectPreview(page, 60, 60);
  await expect(rule).toHaveAttribute("data-pattern", "*");

  // The link opens as the same selection, and the run it starts is called what the rule says.
  await page.reload();
  await expectPreview(page, 60, 60);
  await expect(rule).toHaveAttribute("data-pattern", "*");
  await runButton(page, 60).click();
  const runId = await currentRunId(page);
  await expect(page.getByRole("heading", { level: 1, name: msg("filter.everything") })).toBeVisible();
  const stored = await api.get<{ title: string; planned_count: number }>(`/runs/${runId}`);
  expect(stored.title).toBe(msg("filter.everything"));
  expect(stored.planned_count).toBe(60);
});

test("build a filter: picker with counts, a group, NOT and a raw pattern — kept in the URL", async ({ page, open, api }) => {
  await open();
  await page.getByRole("button", { name: msg("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  const tree = dialog.getByRole("tree");

  // Every node says how many items are behind it.
  await expect(tree.getByRole("treeitem", { name: /^Payments/ })).toContainText(msg("picker.items", { count: 17 }));
  await expect(tree.getByRole("treeitem", { name: /^Smoke/ })).toContainText(msg("picker.items", { count: 14 }));

  await tree.getByRole("treeitem", { name: /^Payments/ }).click();
  await tree.getByRole("treeitem", { name: /^Checkout/ }).click();
  // The dialog says what applying will do, and what the result matches.
  const summary = dialog.locator(".picker-summary");
  await expect(summary).toContainText(msg("picker.adds", { count: 2 }));
  await expect(summary).toContainText(msg("picker.result", { count: 30 }));
  await dialog.getByRole("button", { name: msg("picker.apply") }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("filter-text")).toHaveText("Payments OR Checkout");
  await expectPreview(page, 30, 60);

  // Narrow it: AND a tag typed in, AND NOT a raw prefix pattern.
  await addRule(page, "Smoke");
  await addRule(page, "prefix:Quarantine-");
  await page.getByRole("button", { name: msg("filter.negateRule", { pattern: "prefix:Quarantine-" }) }).click();
  const expected = "(Payments OR Checkout) AND Smoke AND NOT prefix:Quarantine-";
  await expect(page.getByTestId("filter-text")).toHaveText(expected);
  const ast = { op: "AND", items: [{ op: "OR", items: ["Payments", "Checkout"] }, "Smoke", { not: "prefix:Quarantine-" }] };
  const compiled = await api.post<Compiled>("/selection/compile", { tag_filter: ast });
  expect(compiled.count).toBeGreaterThan(0);
  await expectPreview(page, compiled.count, 60);

  // The selection is in the URL: a reload — or a colleague opening the link — sees the same thing.
  expect(new URL(page.url()).hash).toContain("f=");
  await page.reload();
  await expect(page.getByTestId("filter-text")).toHaveText(expected);
  await expectPreview(page, compiled.count, 60);
  await expect(page.locator(".preview tbody tr[data-item-id]").first()).toContainText(compiled.items[0]!.name);
});

test("a taxonomy leaf with parentheses in its pattern lands as one rule, quoted in the text", async ({ page, open, api }) => {
  await open();
  await page.getByRole("button", { name: msg("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  // The demo taxonomy's Cart leaf is `filter: "regex:^Cart(V2)?$"`, written as itself.
  await dialog.getByRole("treeitem", { name: /^Cart\b/ }).first().click();
  await dialog.getByRole("button", { name: msg("picker.apply") }).click();
  await expect(dialog).toBeHidden();
  const rule = page.locator(".expr .chip");
  await expect(rule).toHaveCount(1);
  await expect(rule).toHaveAttribute("data-pattern", "regex:^Cart(V2)?$");
  // One rule is its own sentence; its text form — the link's — is the pattern quoted.
  await expect.poll(() => filterOf(page)).toBe('"regex:^Cart(V2)?$"');
  // That text is filter text: the server reads it back to that one pattern.
  const compiled = await api.post<Compiled>("/selection/compile", { tag_filter: '"regex:^Cart(V2)?$"', limit: 0 });
  expect(compiled.count).toBeGreaterThan(0);
  await expectPreview(page, compiled.count, 60);
});

test("in the picker a tick is a switch: unticking takes the tag out of the filter", async ({ page, open, api }) => {
  await open("#/compose?f=" + encodeURIComponent("(Payments OR Checkout) AND Smoke"));
  const before = await api.post<Compiled>("/selection/compile", {
    tag_filter: { op: "AND", items: [{ op: "OR", items: ["Payments", "Checkout"] }, "Smoke"] },
    limit: 0,
  });
  await expectPreview(page, before.count, 60);
  await page.getByRole("button", { name: msg("filter.pick") }).first().click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  const payments = dialog.getByRole("treeitem", { name: /^Payments/ });
  // What the filter already includes is ticked.
  await expect(payments).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByRole("treeitem", { name: /^Smoke/ })).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByRole("treeitem", { name: /^Catalog/ })).toHaveAttribute("aria-checked", "false");
  await expect(dialog.getByRole("button", { name: msg("picker.apply") })).toBeDisabled();

  await payments.click();
  await expect(dialog.locator(".picker-summary")).toContainText(msg("picker.removes", { count: 1 }));
  await dialog.getByRole("button", { name: msg("picker.apply") }).click();
  await expect(page.getByTestId("filter-text")).toHaveText("Checkout AND Smoke");
  await expect(page.getByRole("button", { name: msg("filter.removeRule", { pattern: "Payments" }) })).toHaveCount(0);
});

test("the picker searches, and closing it by Escape or Cancel changes nothing", async ({ page, open }) => {
  await open("#/compose?f=Smoke");
  await expectPreview(page, 14, 60);
  await page.getByRole("button", { name: msg("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  await dialog.getByRole("searchbox").fill("sso");
  // The hit is shown inside the branch that explains it.
  await expect(dialog.getByRole("treeitem")).toHaveCount(2);
  await expect(dialog.getByRole("treeitem", { name: /^Auth-SSO/ })).toBeVisible();
  await dialog.getByRole("treeitem", { name: /^Auth-SSO/ }).click();
  await dialog.getByRole("searchbox").fill("no such tag anywhere");
  await expect(dialog.getByText(msg("picker.noMatches"))).toBeVisible();
  // Escape in a search field with text clears the text first — the ticks are
  // not thrown away by one keystroke. The next Escape closes the dialog.
  await page.keyboard.press("Escape");
  await expect(dialog.getByRole("searchbox")).toHaveValue("");
  await expect(dialog.getByRole("treeitem", { name: /^Payments/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Smoke" }) })).toBeVisible();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Auth-SSO" }) })).toHaveCount(0);
});

test("the expression can be typed as text, and a mistake is explained in place", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  const area = page.getByRole("textbox", { name: msg("filter.textLabel") });
  await area.fill("Auth AND (Smoke OR");
  await area.press("Enter");
  await expect(page.getByRole("alert")).toContainText(msg("filter.parse.unexpected-end"));
  // A pattern with parentheses typed bare: the message says what to do about it.
  await area.fill("Auth AND regex:(Smoke|Regression)");
  await area.press("Enter");
  await expect(page.getByRole("alert")).toContainText(msg("filter.parse.needs-quotes", { position: 10, token: "regex:" }));
  await area.fill("Auth AND (Smoke OR Sprint-14) AND NOT Auth-SSO");
  await area.press("Enter");
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND (Smoke OR Sprint-14) AND NOT Auth-SSO");
  await expect(page.getByRole("group", { name: msg("filter.group", { op: "OR" }) })).toBeVisible();
  await expect(page.getByRole("button", { name: msg("filter.negateRule", { pattern: "Auth-SSO" }) })).toHaveAttribute("aria-pressed", "true");
});

test("a slow early answer never overwrites a later one: preview and run agree", async ({ page, open, api }) => {
  await open();
  // Hold back the answer to the FIRST compile until a later one has been answered.
  let first: (() => void) | null = null;
  let seen = 0;
  await page.route("**/api/v1/selection/compile", async (route) => {
    seen += 1;
    if (seen === 1) {
      await new Promise<void>((release) => (first = release));
    }
    // The page may have given up on this request by now; that is its right.
    await route.continue().catch(() => undefined);
  });
  await addRule(page, "Payments");
  await expect.poll(() => seen).toBe(1);
  await addRule(page, "Smoke");
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
  await expectPreview(page, 4, 60);
  // Now the stale answer (17 items for "Payments") arrives — and must not be shown.
  expect(first).not.toBeNull();
  first!();
  await page.unroute("**/api/v1/selection/compile");
  await expectPreview(page, 4, 60);
  await expect(page.getByTestId("run-summary")).toContainText(msg("compose.summary", { selected: 4, matched: 4, total: 60 }));

  await runButton(page, 4).click();
  const run = await api.run(await currentRunId(page));
  expect(run.planned_count).toBe(4);
});

test("an invalid pattern marks its rule, makes the preview stale and blocks the run", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  // In quotes it is one pattern as far as the grammar goes; what is wrong with it only the server can say.
  await addRule(page, '"regex:("');
  const error = page.getByTestId("filter-error");
  await expect(error).toContainText(msg("error.code.invalid-filter"));
  await expect(error).toContainText("regex:(");
  // The message points at the rule that is marked — not at a path into the data.
  await expect(error).toContainText(msg("filter.errorAtRule"));
  await expect(error).not.toContainText("tag_filter");
  await expect(error).not.toContainText("items[");
  // The offending rule — and only that one — is marked.
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "regex:(" }) })).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Payments" }) })).not.toHaveAttribute("aria-invalid", "true");
  // The old rows are still there, but said to be old; nothing can be run.
  await expect(page.locator(".preview-scroll")).toHaveClass(/is-stale/);
  await expect(page.getByText(msg("preview.staleHint"))).toBeVisible();
  // No count stands next to a filter it is not the count of — in the preview's head or on the button.
  await expect(page.getByTestId("preview-count")).toHaveCount(0);
  await expect(page.getByRole("button", { name: msg("compose.runUncounted"), exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: msg("compose.export") })).toBeDisabled();

  await page.getByRole("button", { name: msg("filter.removeRule", { pattern: "regex:(" }) }).click();
  await expect(error).toHaveCount(0);
  await expectPreview(page, 17, 60);
  await expect(runButton(page, 17)).toBeEnabled();
});

test("when the server cannot be reached the preview says so, keeps nothing runnable, and recovers by itself", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  let refused = 0;
  await page.route("**/api/v1/selection/compile", (route) => {
    refused += 1;
    return route.abort("connectionrefused");
  });
  await addRule(page, "Smoke");
  const alert = page.locator(".preview").getByRole("alert");
  await expect(alert).toContainText(msg("error.code.network"));
  // The 17 rows of the last answer are still shown — as old, and not runnable.
  await expect(page.locator(".preview-scroll")).toHaveClass(/is-stale/);
  // The run bar says what is true: the server is out of reach — there is no error of the viewer's to fix.
  await expect(page.getByTestId("run-summary")).toHaveText(msg("compose.blocked.network"));
  await expect(page.getByTestId("run-summary")).not.toHaveText(msg("compose.blocked.error"));
  // The count of the filter before this one is gone: "17 of 60" is not what "Payments AND Smoke" matches.
  await expect(page.getByTestId("preview-count")).toHaveCount(0);
  await expect(page.getByRole("button", { name: msg("compose.runUncounted"), exact: true })).toBeDisabled();
  // The page keeps asking by itself — and the message stays put while it does: no flicker between error and spinner.
  await expect.poll(() => refused, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  await expect(alert).toContainText(msg("error.code.network"));
  await expect(alert.getByRole("button", { name: msg("action.retry") })).toBeVisible();

  // The server is back: nobody presses anything, and the preview is the answer to the filter on screen.
  await page.unroute("**/api/v1/selection/compile");
  await expectPreview(page, 4, 60);
  await expect(alert).toHaveCount(0);
  await expect(runButton(page, 4)).toBeEnabled();
});

test("emptying the filter right after an edit leaves nothing loading", async ({ page, open }) => {
  await open();
  // Answers are slow, so the filter is emptied while the first request is still out.
  await page.route("**/api/v1/selection/compile", async (route) => {
    await new Promise((release) => setTimeout(release, 400));
    await route.continue().catch(() => undefined);
  });
  await addRule(page, "Payments");
  await page.getByRole("button", { name: msg("filter.clear") }).click();
  await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
  await expect(page.locator(".preview .spinner")).toHaveCount(0);
  await addRule(page, "Smoke");
  await expectPreview(page, 14, 60);
});

test("search, sort, group and untick — and exclusions survive a filter edit", async ({ page, open, api }) => {
  await open("#/compose?f=Auth");
  await expectPreview(page, 12, 60);
  const rows = page.locator(".preview tbody tr[data-item-id]");
  await expect(rows).toHaveCount(12);

  // Search narrows what is shown, not what is selected.
  await page.getByRole("searchbox", { name: msg("preview.search") }).fill("sso");
  await expect(rows).toHaveCount(3);
  await expect(page.getByText(msg("preview.shown", { count: 3, total: 12 }))).toBeVisible();

  // Untick one item; the header checkbox shows "some".
  const first = rows.first();
  const excludedId = (await first.getAttribute("data-item-id"))!;
  await first.getByRole("checkbox").uncheck();
  const header = page.getByRole("checkbox", { name: msg("preview.selectAll") });
  await expect(header).toHaveJSProperty("indeterminate", true);
  await expect(page.getByTestId("preview-count")).toContainText(msg("preview.excluded", { count: 1 }));
  await page.getByRole("searchbox", { name: msg("preview.search") }).fill("");
  await expect(runButton(page, 11)).toBeEnabled();

  // Sorting and grouping rearrange the rows.
  await page.getByLabel(msg("preview.sort")).selectOption("name");
  const names = await rows.locator(".cell-main").allTextContents();
  expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b, "en", { numeric: true, sensitivity: "base" })));
  await page.getByRole("checkbox", { name: msg("preview.group") }).check();
  await expect(page.locator(".preview tbody tr.group-row")).toHaveCount(3);
  const suite = page.getByRole("checkbox", { name: msg("preview.selectGroup", { group: "Shop › Auth › SSO" }) });
  await expect(suite).toHaveJSProperty("indeterminate", true);
  await page.getByRole("checkbox", { name: msg("preview.group") }).uncheck();

  // Edit the filter: the excluded item is still excluded when it matches again.
  await addRule(page, "Regression");
  const narrowed = await api.post<Compiled>("/selection/compile", { tag_filter: { op: "AND", items: ["Auth", "Regression"] } });
  await expectPreview(page, narrowed.count, 60);
  await page.getByRole("button", { name: msg("filter.removeRule", { pattern: "Regression" }) }).click();
  await expectPreview(page, 12, 60);
  await expect(page.locator(`.preview tr[data-item-id="${excludedId}"]`).getByRole("checkbox")).not.toBeChecked();
  await expect(runButton(page, 11)).toBeEnabled();

  // The exclusion is in the link, and it is what gets dispatched.
  await page.reload();
  await expectPreview(page, 12, 60);
  await expect(page.locator(`.preview tr[data-item-id="${excludedId}"]`).getByRole("checkbox")).not.toBeChecked();
  await runButton(page, 11).click();
  const runId = await currentRunId(page);
  const items = await api.get<{ item_ids: string[]; count: number }>(`/runs/${runId}/items`);
  expect(items.count).toBe(11);
  expect(items.item_ids).not.toContain(excludedId);

  // "Tick all again" is one click.
  await page.goBack();
  await expectPreview(page, 12, 60);
  await page.getByRole("button", { name: msg("preview.clearExclusions") }).click();
  await expect(runButton(page, 12)).toBeEnabled();
});

test("title and labels survive a trip to another view and a reload", async ({ page, open }) => {
  await open("#/compose?f=Catalog");
  await expectPreview(page, 9, 60);
  await page.getByRole("textbox", { name: msg("compose.title") }).fill("Katalog prüfen");
  const labels = page.getByRole("textbox", { name: msg("labels.label") });
  await labels.fill("suite=nightly");
  await labels.press("Enter");
  await expect(page.getByRole("button", { name: msg("labels.remove", { label: "suite=nightly" }) })).toBeVisible();

  await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: msg("nav.runs") }).click();
  await expect(page.getByRole("heading", { name: msg("runs.title"), level: 1 })).toBeVisible();
  await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: msg("nav.compose") }).click();
  await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("Katalog prüfen");
  await expect(page.getByTestId("filter-text")).toHaveCount(0); // one tag needs no sentence
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Catalog" }) })).toBeVisible();
  await expectPreview(page, 9, 60);

  await page.reload();
  await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("Katalog prüfen");
  await expect(page.getByRole("button", { name: msg("labels.remove", { label: "suite=nightly" }) })).toBeVisible();

  // "Start over" really does.
  await page.getByRole("button", { name: msg("compose.reset") }).click();
  await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
  await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("");
});

test("Back takes back the last edit of the filter, Forward restores it", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  await addRule(page, "Smoke");
  await expectPreview(page, 4, 60);
  await addRule(page, "Sprint-12");
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke AND Sprint-12");
  // Unticking an item is not a step of its own.
  await page.locator(".preview tbody tr[data-item-id]").first().getByRole("checkbox").uncheck();

  await page.goBack();
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
  await expectPreview(page, 4, 60);
  await page.goBack();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Payments" }) })).toBeVisible();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Smoke" }) })).toHaveCount(0);
  await expectPreview(page, 17, 60);
  await page.goForward();
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
  await expect(page).toHaveURL(/#\/compose/);
});

test("the link to a selection can be copied, and says that it was", async ({ page, open, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  const copy = page.getByRole("button", { name: msg("compose.copyLink") });
  await copy.click();
  await expect(page.getByRole("button", { name: msg("action.copied") })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(page.url());
});

test("the run bar is always in reach and covers nothing", async ({ page, open }) => {
  await open("#/compose?f=Regression");
  await expectPreview(page, 46, 60);
  const bar = page.getByRole("group", { name: msg("compose.runBar") });
  // At the top of the page: what would run, with what, and the actions.
  await expect(bar).toBeInViewport({ ratio: 1 });
  await expect(bar.getByTestId("run-summary")).toHaveText(msg("compose.summary", { selected: 46, matched: 46, total: 60 }));
  await expect(bar).toContainText(msg("compose.withRunner", { runner: "demo" }));
  for (const name of [msg("compose.plan"), msg("compose.export"), msg("compose.run", { count: 46 })]) {
    await expect(bar.getByRole("button", { name, exact: true })).toBeInViewport({ ratio: 1 });
  }
  // Scrolled to the very end it is still there, and the last thing on the page ends above it.
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(bar).toBeInViewport({ ratio: 1 });
  const labelsHint = page.locator(".run-panel").getByText(msg("labels.hint"));
  await expect(labelsHint).toBeInViewport({ ratio: 1 });
  const hintBox = (await labelsHint.boundingBox())!;
  const barBox = (await bar.boundingBox())!;
  expect(hintBox.y + hintBox.height).toBeLessThanOrEqual(barBox.y);
  // A dialog is above the bar, not under it.
  await page.getByRole("button", { name: msg("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  await dialog.getByRole("treeitem", { name: /^Smoke/ }).click();
  await dialog.getByRole("button", { name: msg("picker.apply") }).click();
  await expect(page.getByTestId("filter-text")).toHaveText("Regression AND Smoke");
});

test("NOT is a labelled switch, and no control but a chip's × is a sign alone", async ({ page, open }) => {
  await open("#/compose?f=" + encodeURIComponent("(Payments OR Checkout) AND NOT prefix:Quarantine-"));
  await expectPreview(page, 28, 60);
  // The switch says NOT in words, and its name says what it does to which rule.
  const off = page.getByRole("button", { name: msg("filter.negateRule", { pattern: "Payments" }) });
  await expect(off).toHaveText(msg("filter.not"));
  await expect(off).toHaveAttribute("aria-pressed", "false");
  const on = page.getByRole("button", { name: msg("filter.negateRule", { pattern: "prefix:Quarantine-" }) });
  await expect(on).toHaveText(msg("filter.not"));
  await expect(on).toHaveAttribute("aria-pressed", "true");
  // One control is a sign: a rule is a chip, and a chip is closed with ×. It says in a name and a tooltip what it removes.
  const remove = page.getByRole("button", { name: msg("filter.removeRule", { pattern: "Payments" }), exact: true });
  await expect(remove).toHaveText("×");
  await expect(remove).toHaveAttribute("title", msg("filter.removeRule", { pattern: "Payments" }));

  // Every other button on the page — and in a dialog — carries visible words.
  const wordless = () =>
    page.evaluate(() =>
      [...document.querySelectorAll("button")]
        .filter((button) => !/[\p{L}]{2,}/u.test(button.textContent ?? ""))
        .filter((button) => !(button.classList.contains("chip-remove") && button.title !== "" && button.getAttribute("aria-label") === button.title))
        .map((button) => button.outerHTML.slice(0, 120)),
    );
  expect(await wordless()).toEqual([]);
  await page.getByRole("button", { name: msg("filter.pick") }).first().click();
  await expect(page.getByRole("dialog", { name: msg("picker.title") }).getByRole("button", { name: msg("action.close") })).toHaveText(msg("action.close"));
  expect(await wordless()).toEqual([]);
});

test("a link whose filter cannot be read says so instead of guessing", async ({ page, open }) => {
  await open("#/compose?f=" + encodeURIComponent("Payments AND ("));
  await expect(page.getByText(msg("compose.unreadableLink"))).toBeVisible();
  await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
});

test("text that is typed and not applied never outlives the selection it was typed for", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  const area = page.getByRole("textbox", { name: msg("filter.textLabel") });

  // Another link opened while a draft is open: the draft is gone, the builder shows the link.
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  await area.fill("Payments AND Smoke");
  await page.evaluate(() => {
    window.location.hash = "#/compose?f=Checkout";
  });
  await expectPreview(page, 13, 60);
  await expect(area).toHaveCount(0);
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Checkout" }) })).toBeVisible();
  // Opening the text again shows the selection of the link, not what was typed before.
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  await expect(area).toHaveValue("Checkout");

  // Back while a draft is open: the same — the draft belonged to the step that was left.
  await area.fill("Checkout AND Auth");
  await page.goBack();
  await expectPreview(page, 17, 60);
  await expect(area).toHaveCount(0);
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Payments" }) })).toBeVisible();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Checkout" }) })).toHaveCount(0);
});

test("while typed text differs from the filter in effect, nothing can be run, planned or exported", async ({ page, open, api }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  const bar = page.getByRole("group", { name: msg("compose.runBar") });
  const actions = [msg("compose.plan"), msg("compose.export"), msg("compose.run", { count: 17 })].map((name) => bar.getByRole("button", { name, exact: true }));
  for (const action of actions) await expect(action).toBeEnabled();

  // Opening the text editor changes nothing yet: the text IS the filter.
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  const area = page.getByRole("textbox", { name: msg("filter.textLabel") });
  for (const action of actions) await expect(action).toBeEnabled();

  // Typed and not applied: the screen says which filter is in effect, and every action is off.
  await area.fill("Payments AND Smoke");
  await expect(page.getByTestId("applied-filter")).toContainText(msg("filter.applied"));
  await expect(page.getByTestId("applied-filter").locator("code")).toHaveText("Payments");
  await expect(page.getByTestId("run-summary")).toHaveText(msg("compose.blocked.textDraft"));
  for (const action of actions) await expect(action).toBeDisabled();
  // Enter in the form does not start a run either.
  await page.getByRole("textbox", { name: msg("compose.title") }).press("Enter");
  await expect(page).toHaveURL(/#\/compose/);
  expect(await api.runs()).toHaveLength(0);

  // Text that is no expression: still off, with the mistake explained — and the filter in effect unchanged.
  await area.fill("Payments AND (");
  await area.press("Enter");
  await expect(page.locator(".expr-text").getByRole("alert")).toBeVisible();
  await expect(page.getByTestId("applied-filter").locator("code")).toHaveText("Payments");
  for (const action of actions) await expect(action).toBeDisabled();
  await expectPreview(page, 17, 60);

  // Applied: the text is the filter, and the actions are back — for the new selection.
  await area.fill("Payments AND Smoke");
  await area.press("Enter");
  await expectPreview(page, 4, 60);
  await expect(runButton(page, 4)).toBeEnabled();
  await expect(bar.getByRole("button", { name: msg("compose.export"), exact: true })).toBeEnabled();

  // Cancelled: what was typed is dropped, and the filter in effect can be run as before.
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  await area.fill("Checkout");
  await expect(runButton(page, 4)).toBeDisabled();
  await page.locator(".expr-text").getByRole("button", { name: msg("action.cancel") }).click();
  await expect(runButton(page, 4)).toBeEnabled();
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
});

test("a step in the history that arrives while the page still digests the one before is never written over", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  const steps = ["Payments AND Smoke", "Payments AND Smoke AND Sprint-12", "Payments AND Smoke AND Sprint-12 AND Regression"];
  for (const [index, tag] of ["Smoke", "Sprint-12", "Regression"].entries()) {
    await addRule(page, tag);
    await expect(page.getByTestId("filter-text")).toHaveText(steps[index]!);
    await expect.poll(() => filterOf(page)).toBe(steps[index]);
  }

  // The exact collision: Back lands, the page takes the step in — and before
  // it has rendered it, the address moves again (here: a link is followed).
  // What the page then holds comes from an address that is no longer the
  // current one, and must not be written into the current one.
  // (A listener added now runs after the page's own: the page has taken the
  // step in, and has not yet rendered what it made of it.)
  await page.evaluate((landing) => {
    const onStep = () => {
      // Only the step back itself: the event of the edit before may still be on its way.
      if (new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("f") !== landing) return;
      window.removeEventListener("hashchange", onStep);
      window.location.hash = "#/compose?f=Checkout";
    };
    window.addEventListener("hashchange", onStep);
  }, steps[1]!);
  await page.goBack();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Checkout" }) })).toBeVisible();
  await expectPreview(page, 13, 60);
  expect(filterOf(page)).toBe("Checkout");
  // The step Back went to is still its own entry, one further back.
  await page.goBack();
  await expect(page.getByTestId("filter-text")).toHaveText(steps[1]!);
  expect(filterOf(page)).toBe(steps[1]);
  await page.goBack();
  await expect(page.getByTestId("filter-text")).toHaveText(steps[0]!);
  await page.goForward();
  await page.goForward();
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Checkout" }) })).toBeVisible();
  expect(filterOf(page)).toBe("Checkout");
});

test("Back pressed faster than a slow page can follow lands where the browser is, and keeps the history whole", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  const steps = ["Payments AND Smoke", "Payments AND Smoke AND Sprint-12", "Payments AND Smoke AND Sprint-12 AND Regression"];
  for (const [index, tag] of ["Smoke", "Sprint-12", "Regression"].entries()) {
    await addRule(page, tag);
    await expect(page.getByTestId("filter-text")).toHaveText(steps[index]!);
    await expect.poll(() => filterOf(page)).toBe(steps[index]);
  }
  // A page that is busy for a moment on every step — a slow machine.
  await page.evaluate(() => {
    window.addEventListener(
      "hashchange",
      () => {
        const until = performance.now() + 40;
        while (performance.now() < until);
      },
      true,
    );
  });

  for (let round = 0; round < 3; round += 1) {
    // Three steps back in one breath…
    await page.evaluate(() => {
      history.back();
      history.back();
      history.back();
    });
    await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Smoke" }) })).toHaveCount(0);
    await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Payments" }) })).toBeVisible();
    await expectPreview(page, 17, 60);
    await expect.poll(() => filterOf(page)).toBe("Payments");
    // …and nothing was written over on the way: every step forward is the step that was taken.
    for (const step of steps) {
      await page.goForward();
      await expect(page.getByTestId("filter-text")).toHaveText(step);
      expect(filterOf(page)).toBe(step);
    }
  }
});

test("many rules in a group are chips that wrap: the operator is said once, and the panel stays in view", async ({ page, open, api }) => {
  // 31 tags of the catalog, as somebody picks a sprint's tickets one by one.
  const catalog = await api.get<{ tags: { tag: string }[] }>("/catalog/tags");
  const tags = catalog.tags.map((entry) => entry.tag).sort().slice(0, 31);
  expect(tags).toHaveLength(31);
  await open("#/compose?f=" + encodeURIComponent(tags.join(" OR ")));
  await expect(page.locator(".preview")).toHaveAttribute("aria-busy", "false");
  const chips = page.locator(".expr .chip");
  await expect(chips).toHaveCount(31);

  // Chips share rows instead of taking one each, and the whole panel is on one screen.
  const boxes = await chips.evaluateAll((elements) => elements.slice(0, 6).map((element) => element.getBoundingClientRect().top));
  expect(new Set(boxes.map((top) => Math.round(top))).size).toBeLessThan(6);
  const panel = (await page.locator(".filter-panel").boundingBox())!;
  expect(panel.height).toBeLessThan(700);
  // The operator is said by the group, once — not between every two rules.
  await expect(page.getByRole("combobox", { name: msg("filter.combine") })).toHaveCount(1);
  await expect(page.getByRole("combobox", { name: msg("filter.combine") })).toHaveValue("OR");
  await expect(page.locator(".expr-rules")).not.toContainText(/\bOR\b/);
  await expect(page.locator(".expr-rules")).toHaveClass(/is-long/);
  await expect(page.getByText(msg("filter.ruleCount", { count: 31 }))).toBeVisible();

  // Every rule keeps its controls, in the order they read: the pattern, NOT, Remove — also the last one, scrolled to by focus.
  const last = tags[30]!;
  const named = (key: string) => page.getByRole("button", { name: msg(key, { pattern: last }), exact: true });
  await named("filter.editPattern").focus();
  await expect(named("filter.editPattern")).toBeInViewport({ ratio: 1 });
  await page.keyboard.press("Tab");
  await expect(named("filter.negateRule")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(named("filter.removeRule")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(chips).toHaveCount(30);
  const negate = page.getByRole("button", { name: msg("filter.negateRule", { pattern: tags[0]! }), exact: true });
  await negate.click();
  await expect(negate).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(`.chip[data-pattern="${tags[0]!}"]`)).toHaveClass(/is-negated/);
});

test("the preview is as tall as its rows, and at most reaches down to the run bar", async ({ page, open }) => {
  // Many rows: the table uses the window — it ends just above the run bar, not half-way down.
  await open("#/compose?f=Regression");
  await expectPreview(page, 46, 60);
  const bar = page.getByRole("group", { name: msg("compose.runBar") });
  const scroll = page.locator(".preview-scroll");
  const gap = async () => {
    const [table, barBox] = [(await page.locator(".preview").boundingBox())!, (await bar.boundingBox())!];
    return barBox.y - (table.y + table.height);
  };
  expect(await gap()).toBeGreaterThanOrEqual(0);
  expect(await gap()).toBeLessThan(48);
  expect(await scroll.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  // A taller window: more rows, the same small gap.
  await page.setViewportSize({ width: 1440, height: 1200 });
  await expect.poll(gap).toBeLessThan(48);
  expect(await gap()).toBeGreaterThanOrEqual(0);

  // Few rows: the panel ends with them — no empty box, nothing to scroll.
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  expect(await scroll.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(false);
  const rows = (await scroll.boundingBox())!;
  expect(rows.height).toBeLessThan(200);
});

test("the add field reads what is typed with the filter's grammar — the same text means the same everywhere", async ({ page, open, api }) => {
  await open();
  const field = page.getByRole("combobox", { name: msg("filter.addTag"), exact: true });
  const chips = page.locator(".expr .chip");

  // An expression is the rules it says: two rules, the count the API gives for that expression.
  const both = await api.post<Compiled>("/selection/compile", { tag_filter: { op: "AND", items: ["Payments", "Smoke"] }, limit: 0 });
  await addRule(page, "Payments AND Smoke");
  await expect(chips).toHaveCount(2);
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
  await expectPreview(page, both.count, 60);
  expect(both.count).toBeGreaterThan(0);
  expect(filterOf(page)).toBe("Payments AND Smoke");
  // The text editor holds the same text, and reads it to the same filter.
  await page.getByRole("button", { name: msg("filter.editAsText") }).click();
  await expect(page.getByRole("textbox", { name: msg("filter.textLabel") })).toHaveValue("Payments AND Smoke");
  await page.locator(".expr-text").getByRole("button", { name: msg("action.cancel") }).click();

  // The other operator is a group of its own; NOT is the switch of its rule.
  await addRule(page, "Sprint-12 OR Sprint-13");
  // The preview of the last accepted filter is awaited — its request has
  // left and been answered — before the requests that follow are counted.
  const lastPreview = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/selection/compile") && (response.request().postData() ?? "").includes("Quarantine-"),
  );
  await addRule(page, "NOT prefix:Quarantine-");
  await lastPreview;
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke AND (Sprint-12 OR Sprint-13) AND NOT prefix:Quarantine-");
  await expect(page.getByRole("button", { name: msg("filter.negateRule", { pattern: "prefix:Quarantine-" }) })).toHaveAttribute("aria-pressed", "true");

  // Operators are capitals. Small ones are refused in place — nothing is added, nothing is sent, the place is named.
  let asked = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/selection/compile")) asked += 1;
  });
  await addRule(page, "Cart and Checkout");
  const refusal = page.locator(".expr-group.is-root > .expr-add").getByRole("alert");
  await expect(refusal).toHaveText(msg("filter.operatorCase", { position: 6, operator: "AND", token: "and" }));
  await expect(field).toHaveValue("Cart and Checkout");
  await expect(field).toHaveAttribute("aria-invalid", "true");
  await expect(field).toBeFocused();
  await expect(chips).toHaveCount(5);
  // Half an expression, and a pattern without a pattern: refused the same way.
  await addRule(page, "Cart AND (");
  await expect(refusal).toHaveText(msg("filter.parse.unexpected-end"));
  await addRule(page, "prefix:");
  await expect(refusal).toHaveText(msg("filter.parse.empty", { position: 1 }));
  await expect(chips).toHaveCount(5);
  expect(asked).toBe(0);
  // Typing on takes the message away.
  await field.fill("Cart");
  await expect(refusal).toHaveCount(0);
  await field.fill("");

  // A tag that has a space or an operator word in it is typed in quotes — and is then ONE rule.
  await page.getByRole("button", { name: msg("filter.clear") }).click();
  await addRule(page, '"Payments AND Smoke"');
  await expect(chips).toHaveCount(1);
  await expect(chips).toHaveAttribute("data-pattern", "Payments AND Smoke");
  await expectPreview(page, 0, 60);
  expect(filterOf(page)).toBe('"Payments AND Smoke"');
});

test("a rule no tag matches is flagged on its chip, with the tag that was probably meant", async ({ page, open }) => {
  await open("#/compose?f=Smoke");
  await expectPreview(page, 14, 60);
  // A slip of the fingers: two letters of a tag the wrong way round.
  const slip = (tag: string) => tag.slice(0, 4) + tag[5] + tag[4] + tag.slice(6);
  const typo = slip("Payments");
  const NO_SUCH_PREFIX = `prefix:${slip("Sprint-")}`;
  await addRule(page, typo);
  await expectPreview(page, 0, 60);

  const chip = page.locator(`.chip[data-pattern="${typo}"]`);
  await expect(chip).toHaveClass(/is-unmatched/);
  await expect(chip.locator(".chip-flag")).toHaveText(msg("filter.noTag"));
  await expect(chip.locator(".chip-flag")).toHaveAttribute("title", msg("filter.noTagHint"));
  await expect(page.locator('.chip[data-pattern="Smoke"]')).not.toHaveClass(/is-unmatched/);
  const note = page.getByRole("status", { name: msg("filter.unmatchedList") });
  await expect(note).toContainText(msg("filter.matchesNoTag", { pattern: typo }));

  // One click on what was meant, and the rule matches.
  await note.getByRole("button", { name: msg("filter.replaceWith", { pattern: typo, tag: "Payments" }) }).click();
  await expect(page.getByTestId("filter-text")).toHaveText("Smoke AND Payments");
  await expectPreview(page, 4, 60);
  await expect(page.locator(".chip.is-unmatched")).toHaveCount(0);
  await expect(note).toHaveCount(0);

  // A pattern nothing matches is flagged too; case alone is no mismatch for a tag, as on the server.
  await addRule(page, NO_SUCH_PREFIX);
  await addRule(page, "smoke");
  await expect(page.locator(".chip.is-unmatched")).toHaveCount(1);
  await expect(page.locator(".chip.is-unmatched")).toHaveAttribute("data-pattern", NO_SUCH_PREFIX);
});

test("three rules fit in one row of the filter column", async ({ page, open }) => {
  await open("#/compose?f=" + encodeURIComponent("Payments AND Checkout AND Smoke AND (Cart OR Catalog OR Auth)"));
  await expect(page.locator(".preview")).toHaveAttribute("aria-busy", "false");
  const tops = async (scope: string) => (await page.locator(`${scope} > li > .chip`).evaluateAll((chips) => chips.map((chip) => Math.round(chip.getBoundingClientRect().top))));
  const root = await tops(".expr-group.is-root > .expr-rules");
  expect(root).toHaveLength(3);
  expect(new Set(root).size).toBe(1);
  // Also inside a group, which is narrower.
  const inner = await tops(".expr-group:not(.is-root) > .expr-rules");
  expect(inner).toHaveLength(3);
  expect(new Set(inner).size).toBe(1);
  // Each rule still has its three controls: the pattern, NOT in a word, and the ×.
  const chip = page.locator('.chip[data-pattern="Checkout"]');
  await expect(chip.getByRole("button")).toHaveCount(3);
  await expect(chip.getByRole("button", { name: msg("filter.negateRule", { pattern: "Checkout" }) })).toHaveText(msg("filter.not"));
});
