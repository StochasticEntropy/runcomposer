// German, the keyboard, a phone-sized screen, and the dark theme: the main
// flow has to work in all of them, not only be reachable.

import type { Page } from "@playwright/test";

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

const de = (key: string, vars: Record<string, string | number> = {}) => msg(key, vars, "de");

/** Nothing sticks out sideways: the page is exactly as wide as the screen. */
async function expectNoSidewaysScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "the page scrolls sideways").toBeLessThanOrEqual(0);
}

/** Every control on the page has a name a screen reader can say. */
async function expectNamedControls(page: Page): Promise<void> {
  const unnamed = await page.evaluate(() => {
    const nameOf = (element: Element): string => {
      const labelled = element.getAttribute("aria-labelledby");
      if (labelled) return labelled.split(/\s+/).map((id) => document.getElementById(id)?.textContent ?? "").join(" ");
      const control = element as HTMLInputElement;
      return (
        element.getAttribute("aria-label") ??
        (control.labels && control.labels.length > 0 ? [...control.labels].map((label) => label.textContent).join(" ") : null) ??
        element.textContent ??
        ""
      );
    };
    return [...document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, [role=tree]")]
      .filter((element) => nameOf(element).trim() === "")
      .map((element) => element.outerHTML.slice(0, 160));
  });
  expect(unnamed, "controls without an accessible name").toEqual([]);
}

test("German: the language switch covers every view, enums and errors, and is remembered", async ({ page, open, api }) => {
  const run = await api.finishedRun("Regression", "b", { title: "Nightly regression" });
  await open("#/compose?f=Payments");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.getByRole("combobox", { name: msg("prefs.language") }).selectOption("de");

  // The page, not only its strings: the document says it is German.
  await expect(page.locator("html")).toHaveAttribute("lang", "de");
  const nav = page.getByRole("navigation", { name: de("nav.label") });
  await expect(nav.getByRole("link", { name: de("nav.compose") })).toHaveAttribute("aria-current", "page");
  await expect(page).toHaveTitle(new RegExp(de("nav.compose")));
  await expectPreview(page, 17, 60, "de");
  await expect(page.getByRole("button", { name: de("compose.run", { count: 17 }) })).toBeEnabled();
  await expect(page.getByRole("combobox", { name: de("runner.label") })).toBeVisible();
  // The builder kept its state through the switch.
  await expect(page.getByRole("button", { name: de("filter.editPattern", { pattern: "Payments" }) })).toBeVisible();

  // An error from the server is said in German, with the server's detail next to it.
  await addRule(page, "regex:(", "de");
  await expect(page.getByTestId("filter-error")).toContainText(de("error.code.invalid-filter"));
  await expect(page.getByText(de("preview.staleHint"))).toBeVisible();
  await page.getByRole("button", { name: de("filter.removeRule", { pattern: "regex:(" }) }).click();

  // The picker.
  await page.getByRole("button", { name: de("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: de("picker.title") });
  await expect(dialog.getByRole("treeitem", { name: /^Smoke/ })).toContainText(de("picker.items", { count: 14 }));
  await expect(dialog.getByRole("treeitem", { name: new RegExp(`^${de("picker.unassigned")}`) })).toBeVisible();
  await dialog.getByRole("button", { name: de("action.cancel") }).click();

  // Runs: states, results and verdict words, relative time.
  await nav.getByRole("link", { name: de("nav.runs") }).click();
  const row = page.locator(`tr[data-run-id="${run.id}"]`);
  await expect(row).toContainText(de("runState.COMPLETE"));
  await expect(row).toContainText(de("completion.FAIL"));
  await expect(row).toContainText(`${run.verdict_summary.PASS} ${de("verdict.PASS")}`);
  await expect(row.locator("time")).toHaveText(/^(vor |jetzt|gerade)/);
  await expect(page.getByLabel(de("runs.state")).getByRole("option", { name: de("runState.AWAITING_RESULTS") })).toBeAttached();

  // A run: actions, tabs, events.
  await row.getByRole("link", { name: "Nightly regression" }).click();
  await expect(page.getByRole("button", { name: de("run.rerunAll") })).toBeVisible();
  await expect(page.getByTestId("run-status")).toContainText(de("completion.FAIL"));
  await expect(page.getByRole("button", { name: new RegExp(`^${de("run.filter.failed")}`) })).toBeVisible();
  await page.getByRole("link", { name: de("run.tab.activity") }).click();
  await expect(page.locator(".events")).toContainText(de("event.run.completed"));
  await expect(page.locator(".events")).toContainText(de("event.delivery.recorded"));
  await expect(page.locator(".events")).not.toContainText("run.completed");

  // The inbox.
  await nav.getByRole("link", { name: new RegExp(`^${de("nav.inbox")}`) }).click();
  await expect(page.getByText(de("inbox.empty"))).toBeVisible();

  // Nothing on any of these pages is an untranslated key.
  await expect(page.locator("body")).not.toContainText(/\b(nav|run|runs|compose|filter|preview|picker|inbox|error)\.[a-z]+[A-Z.]\w*/);

  // The choice is the viewer's and survives a reload.
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("lang", "de");
  await expect(page.getByRole("heading", { level: 1, name: de("inbox.title") })).toBeVisible();
});

test.describe("a server whose default language is German", () => {
  test.use({ config: "german" });

  test("starts in German without anybody choosing", async ({ page, open }) => {
    await open();
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
    await expect(page.locator(".landing .state-title")).toHaveText(de("compose.landing.title"));
    await expect(page.getByRole("combobox", { name: de("prefs.language") })).toHaveValue("de");
    // …and the viewer can still choose otherwise.
    await page.getByRole("combobox", { name: de("prefs.language") }).selectOption("en");
    await expect(page.locator(".landing .state-title")).toHaveText(msg("compose.landing.title"));
  });
});

test("keyboard only: compose a filter, pick tags in the tree, start a run, read it", async ({ page, open, api }) => {
  await open();
  const keys = page.keyboard;

  // The first Tab reaches the skip link; it leads into the content.
  await keys.press("Tab");
  await expect(page.getByRole("link", { name: msg("nav.skip") })).toBeFocused();
  await keys.press("Enter");
  await expect(page.locator("main")).toBeFocused();

  // Type a tag into the filter.
  const field = page.getByRole("combobox", { name: msg("filter.addTag"), exact: true });
  await field.focus();
  await keys.type("Auth");
  await keys.press("Enter");
  await expectPreview(page, 12, 60);
  // Focus stays in the field, ready for the next rule.
  await expect(field).toBeFocused();

  // Open the picker from the keyboard and tick a tag in the tree.
  await page.getByRole("button", { name: msg("filter.pick") }).focus();
  await keys.press("Enter");
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  await expect(dialog.getByRole("searchbox")).toBeFocused();
  await keys.type("smoke");
  const tree = dialog.getByRole("tree");
  await expect(tree.getByRole("treeitem")).toHaveCount(2);
  await keys.press("Tab"); // the "items must have" choice is off until something new is ticked
  await expect(tree).toBeFocused();
  await keys.press("ArrowDown");
  await expect(tree.getByRole("treeitem", { name: /^Smoke/ })).toHaveAttribute("aria-selected", "true");
  await keys.press(" ");
  await expect(tree.getByRole("treeitem", { name: /^Smoke/ })).toHaveAttribute("aria-checked", "true");
  await dialog.getByRole("button", { name: msg("picker.apply") }).focus();
  await keys.press("Enter");
  await expect(dialog).toBeHidden();
  // Focus goes back to where the dialog was opened from — also after Escape.
  const pick = page.getByRole("button", { name: msg("filter.pick"), exact: true });
  await expect(pick).toBeFocused();
  await keys.press("Enter");
  await expect(dialog.getByRole("searchbox")).toBeFocused();
  await keys.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(pick).toBeFocused();
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND Smoke");
  const expected = await api.post<{ count: number }>("/selection/compile", { tag_filter: { op: "AND", items: ["Auth", "Smoke"] }, limit: 0 });
  await expectPreview(page, expected.count, 60);

  // Negate a rule and take it back, from the keyboard.
  await page.getByRole("button", { name: msg("filter.negateRule", { pattern: "Smoke" }) }).focus();
  await keys.press(" ");
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND NOT Smoke");
  await keys.press(" ");
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND Smoke");
  // Removing a rule does not drop the focus on the floor.
  await page.getByRole("button", { name: msg("filter.removeRule", { pattern: "Smoke" }) }).focus();
  await keys.press("Enter");
  await expect(field).toBeFocused();
  await expectPreview(page, 12, 60);

  // The preview's rows are ONE stop for Tab: arrow keys move between them, Space ticks.
  const boxes = page.locator(".preview tbody tr[data-item-id]").getByRole("checkbox");
  await page.getByRole("checkbox", { name: msg("preview.selectAll") }).focus();
  await keys.press("Tab");
  await expect(boxes.nth(0)).toBeFocused();
  await keys.press("ArrowDown");
  await keys.press("ArrowDown");
  await expect(boxes.nth(2)).toBeFocused();
  await keys.press("ArrowUp");
  await expect(boxes.nth(1)).toBeFocused();
  await keys.press(" ");
  await expect(boxes.nth(1)).not.toBeChecked();
  // The next Tab leaves the list — it does not go to the next of twelve rows.
  await keys.press("Tab");
  await expect(page.getByRole("combobox", { name: msg("runner.label") })).toBeFocused();

  // Choose the runner and start.
  const runner = page.getByRole("combobox", { name: msg("runner.label") });
  await runner.focus();
  await runner.selectOption("demo");
  const title = page.getByRole("textbox", { name: msg("compose.title") });
  await title.focus();
  await keys.type("Lauf per Tastatur");
  await keys.press("Enter"); // Enter in the form starts the run
  const runId = await currentRunId(page);
  await api.waitFor(runId, ["COMPLETE"]);

  // On the run page, focus is in the content and every part is reachable.
  await expect(page.locator("main")).toBeFocused();
  await expect(page.getByRole("heading", { level: 1, name: "Lauf per Tastatur" })).toBeVisible();
  await page.getByRole("link", { name: msg("run.tab.selection") }).focus();
  await keys.press("Enter");
  await expect(page.getByRole("link", { name: msg("run.tab.selection") })).toHaveAttribute("aria-current", "page");
  expect((await api.run(runId)).planned_count).toBe(11);
});

test("every control has a name, and status changes are announced", async ({ page, open, api }) => {
  const run = await api.finishedRun("Auth", "b", { title: "Anmeldung prüfen" });
  await open("#/compose?f=Auth");
  await expectPreview(page, 12, 60);
  await expectNamedControls(page);
  // The result of an edit is spoken through the page's live region.
  await expect(page.getByTestId("announcer")).toHaveText(msg("preview.matched", { count: 12, total: 60 }));
  await addRule(page, "Smoke");
  await expect(page.getByTestId("announcer")).not.toHaveText(msg("preview.matched", { count: 12, total: 60 }));

  await page.getByRole("button", { name: msg("filter.pick") }).first().click();
  await expectNamedControls(page);
  await page.keyboard.press("Escape");

  for (const hash of ["#/runs", `#/runs/${run.id}`, `#/runs/${run.id}?tab=selection`, `#/runs/${run.id}?tab=activity`, "#/inbox"]) {
    await open(hash);
    await expect(page.locator("h1")).toBeVisible();
    await expectNamedControls(page);
  }
  // Tables say what their columns are.
  await open(`#/runs/${run.id}`);
  await expect(page.locator(".verdict-table thead th[scope=col]")).toHaveCount(6);
});

test.describe("on a phone (375 px)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("the whole journey fits: compose, pick, run, read, list, inbox", async ({ page, open, api }) => {
    await open();
    await expectNoSidewaysScroll(page);
    // Everything in the header is on screen.
    for (const name of [msg("nav.compose"), msg("nav.runs")]) {
      await expect(page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name })).toBeInViewport();
    }
    await expect(page.getByRole("combobox", { name: msg("prefs.language") })).toBeInViewport();
    await expect(page.getByRole("combobox", { name: msg("prefs.theme") })).toBeInViewport();

    // The picker opens inside the screen.
    await page.getByRole("button", { name: msg("filter.pick") }).click();
    const dialog = page.getByRole("dialog", { name: msg("picker.title") });
    const box = (await dialog.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
    expect(box.y + box.height).toBeLessThanOrEqual(812);
    await dialog.getByRole("treeitem", { name: /^Auth/ }).click();
    await dialog.getByRole("treeitem", { name: /^Checkout/ }).click();
    await dialog.getByRole("button", { name: msg("picker.apply") }).click();
    await expect(page.getByTestId("filter-text")).toHaveText("Auth OR Checkout");
    await expectPreview(page, 25, 60);
    await expectNoSidewaysScroll(page);

    // The preview shows name and id of an item, and its checkbox is usable.
    const row = page.locator(".preview tbody tr[data-item-id]").first();
    await expect(row.locator(".cell-main")).toBeVisible();
    await expect(row.locator(".cell-sub")).toBeVisible();
    await row.getByRole("checkbox").uncheck();
    // A tap on an item's name ticks it too — the checkbox alone is a small target.
    const second = page.locator(".preview tbody tr[data-item-id]").nth(1);
    await second.locator(".cell-main").click();
    await expect(second.getByRole("checkbox")).not.toBeChecked();
    await second.locator(".cell-main").click();
    await expect(second.getByRole("checkbox")).toBeChecked();
    expect((await second.getByRole("checkbox").boundingBox())!.width).toBeGreaterThanOrEqual(22);

    await page.getByRole("combobox", { name: msg("runner.label") }).selectOption("demo");
    await page.getByRole("group", { name: msg("runner.parameters") }).getByRole("textbox", { name: "Seed" }).fill("b");
    const start = runButton(page, 24);
    await start.scrollIntoViewIfNeeded();
    await expect(start).toBeInViewport();
    await start.click();
    const runId = await currentRunId(page);
    const done = await api.waitFor(runId, ["COMPLETE"]);
    await expect(page.getByTestId("run-status")).toContainText(msg("runState.COMPLETE"));
    await expectNoSidewaysScroll(page);

    // Results: a failure with its item, shard and message, all inside the screen.
    const failed = done.verdict_summary.FAIL ?? 0;
    expect(failed).toBeGreaterThan(0);
    const verdict = page.locator(".verdict-table tbody tr[data-item-id]").first();
    await verdict.scrollIntoViewIfNeeded();
    await expect(verdict).toHaveAttribute("data-status", "FAIL");
    for (const cell of [".col-status", ".col-item", ".col-message"]) {
      const cellBox = (await verdict.locator(cell).boundingBox())!;
      expect(cellBox.x + cellBox.width, cell).toBeLessThanOrEqual(375);
    }
    // …and with room: the item's id is readable, not squeezed into a sliver.
    expect((await verdict.locator(".col-item").boundingBox())!.width).toBeGreaterThan(200);
    expect((await verdict.boundingBox())!.width).toBeGreaterThan(320);
    // The actions wrap instead of running off the side.
    await expect(page.getByRole("button", { name: msg("run.rerunFailed", { count: failed }) })).toBeVisible();
    for (const tab of ["selection", "activity"] as const) {
      await page.getByRole("link", { name: msg(`run.tab.${tab}`) }).click();
      await expectNoSidewaysScroll(page);
    }

    // The runs list is a stack of cards with labelled values.
    await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: msg("nav.runs") }).click();
    const card = page.locator(`tr[data-run-id="${runId}"]`);
    await expect(card).toContainText(msg("completion.FAIL"));
    await expect(card.locator('td[data-label]').first()).toBeVisible();
    await expectNoSidewaysScroll(page);

    await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: new RegExp(`^${msg("nav.inbox")}`) }).click();
    await expect(page.getByText(msg("inbox.empty"))).toBeVisible();
    await expectNoSidewaysScroll(page);
  });
});

test.describe("on a tablet (768 px)", () => {
  test.use({ viewport: { width: 768, height: 1024 } });

  test("compose and a run page fit without sideways scrolling", async ({ page, open, api }) => {
    const run = await api.finishedRun("Regression", "b", { title: "Nightly regression", labels: { suite: "nightly", env: "staging" } });
    await open("#/compose?f=" + encodeURIComponent("(Payments OR Checkout) AND NOT prefix:Quarantine-"));
    await expect(page.locator(".preview-table")).toBeVisible();
    await expectNoSidewaysScroll(page);
    await expect(page.getByRole("combobox", { name: msg("prefs.theme") })).toBeInViewport();
    for (const hash of ["#/runs", `#/runs/${run.id}`, "#/inbox"]) {
      await open(hash);
      await expect(page.locator("h1")).toBeVisible();
      await expectNoSidewaysScroll(page);
    }
  });
});

test("the dark theme is a choice, applies at once, and is remembered", async ({ page, open }) => {
  await open();
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const light = await background();
  await page.getByRole("combobox", { name: msg("prefs.theme") }).selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect.poll(background).not.toBe(light);
  const dark = await background();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await background()).toBe(dark);
  // Text stays readable: the body text is light on the dark page.
  const [text] = await page.evaluate(() => [getComputedStyle(document.body).color]);
  expect(text).not.toBe(dark);
});
