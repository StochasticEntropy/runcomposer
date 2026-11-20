// Composing: the filter builder, the picker, and a preview that can be
// trusted — it is the answer to the filter on screen, or it says it is not.

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

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
  await addRule(page, "regex:(");
  const error = page.getByTestId("filter-error");
  await expect(error).toContainText(msg("error.code.invalid-filter"));
  await expect(error).toContainText("regex:(");
  // The offending rule — and only that one — is marked.
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "regex:(" }) })).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Payments" }) })).not.toHaveAttribute("aria-invalid", "true");
  // The old rows are still there, but said to be old; nothing can be run.
  await expect(page.locator(".preview-scroll")).toHaveClass(/is-stale/);
  await expect(page.getByText(msg("preview.staleHint"))).toBeVisible();
  await expect(page.getByRole("button", { name: msg("compose.run", { count: 0 }) })).toBeDisabled();
  await expect(page.getByRole("button", { name: msg("compose.export") })).toBeDisabled();

  await page.getByRole("button", { name: msg("filter.removeRule", { pattern: "regex:(" }) }).click();
  await expect(error).toHaveCount(0);
  await expectPreview(page, 17, 60);
  await expect(runButton(page, 17)).toBeEnabled();
});

test("when the server cannot be reached the preview says so, keeps nothing runnable, and recovers", async ({ page, open }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);
  await page.route("**/api/v1/selection/compile", (route) => route.abort("connectionrefused"));
  await addRule(page, "Smoke");
  const alert = page.locator(".preview").getByRole("alert");
  await expect(alert).toContainText(msg("error.code.network"));
  // The 17 rows of the last answer are still shown — as old, and not runnable.
  await expect(page.locator(".preview-scroll")).toHaveClass(/is-stale/);
  await expect(page.getByTestId("run-summary")).toHaveText(msg("compose.blocked.error"));
  await expect(page.getByRole("button", { name: msg("compose.run", { count: 0 }) })).toBeDisabled();

  await page.unroute("**/api/v1/selection/compile");
  await alert.getByRole("button", { name: msg("action.retry") }).click();
  await expectPreview(page, 4, 60);
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

test("a link whose filter cannot be read says so instead of guessing", async ({ page, open }) => {
  await open("#/compose?f=" + encodeURIComponent("Payments AND ("));
  await expect(page.getByText(msg("compose.unreadableLink"))).toBeVisible();
  await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
});
