// German, the keyboard, a phone-sized screen, and the dark theme: the main
// flow has to work in all of them, not only be reachable.

import type { Locator, Page } from "@playwright/test";

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

const de = (key: string, vars: Record<string, string | number> = {}) => msg(key, vars, "de");

/** Nothing sticks out sideways: the page is exactly as wide as the screen. */
async function expectNoSidewaysScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "the page scrolls sideways").toBeLessThanOrEqual(0);
}

/**
 * Move the focus to `target` the way a keyboard does: Tab (or Shift+Tab), one
 * stop at a time. Fails when the control is not among the next stops — a
 * control that only a mouse or a script can reach is not reachable.
 */
async function tabTo(page: Page, target: Locator, options: { back?: boolean; within?: number } = {}): Promise<void> {
  const focused = () => target.evaluate((element) => element === document.activeElement);
  for (let stops = 0; stops < (options.within ?? 40) && !(await focused()); stops += 1) {
    await page.keyboard.press(options.back ? "Shift+Tab" : "Tab");
  }
  await expect(target, "not reached with the Tab key").toBeFocused();
}

/** The mark the stylesheet puts in front of text that is not in the page's language ("none" when there is none). */
const languageMark = (text: Locator) => text.evaluate((element) => getComputedStyle(element, "::before").content);

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

  // The NOT switch says NICHT.
  await expect(page.getByRole("button", { name: de("filter.negateRule", { pattern: "Payments" }) })).toHaveText("NICHT");
  await expect(page.getByRole("group", { name: de("compose.runBar") }).getByRole("button", { name: de("compose.plan"), exact: true })).toBeVisible();

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

  test.describe("seen from a browser that asks for a language the UI does not have", () => {
    test.use({ locale: "fr-FR" });

    test("starts in German without anybody choosing", async ({ page, open }) => {
      await open();
      await expect(page.locator("html")).toHaveAttribute("lang", "de");
      await expect(page.locator(".landing .state-title")).toHaveText(de("compose.landing.title"));
      await expect(page.getByRole("combobox", { name: de("prefs.language") })).toHaveValue("de");
      // …and the viewer can still choose otherwise.
      await page.getByRole("combobox", { name: de("prefs.language") }).selectOption("en");
      await expect(page.locator(".landing .state-title")).toHaveText(msg("compose.landing.title"));
      await expect(page.locator("html")).toHaveAttribute("lang", "en");
    });
  });

  test("a browser that asks for English gets English: the default is for those whose language the UI does not have", async ({ page, open }) => {
    await open();
    await expect(page.locator(".landing .state-title")).toHaveText(msg("compose.landing.title"));
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByRole("combobox", { name: msg("prefs.language") })).toHaveValue("en");
  });
});

test.describe("a browser that asks for German", () => {
  test.use({ locale: "de-DE" });

  test("gets German from the first screen on — and the document says so", async ({ page, open }) => {
    await open("#/compose?f=Payments");
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
    await expect(page.getByRole("combobox", { name: de("prefs.language") })).toHaveValue("de");
    await expectPreview(page, 17, 60, "de");
    // Nobody chose: nothing is stored, the browser's wish is simply followed.
    expect(await page.evaluate(() => window.localStorage.getItem("runcomposer.locale"))).toBeNull();
    // A choice wins over the browser, and stays.
    await page.getByRole("combobox", { name: de("prefs.language") }).selectOption("en");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expectPreview(page, 17, 60);
  });

  test("the built-in runners speak German; what a runner or the server words itself is marked as theirs", async ({ page, open }) => {
    await open("#/compose?f=Auth-SSO");
    await expectPreview(page, 3, 60, "de");
    const runner = page.getByRole("combobox", { name: de("runner.label") });
    const status = page.locator(".runner-status");
    const form = page.getByRole("group", { name: de("runner.parameters") });

    // The reference runners: title, description, health and their own options, in German.
    await expect(runner.getByRole("option", { name: `demo — ${de("builtin.runner.demo.title")}`, exact: true })).toBeAttached();
    await expect(runner.getByRole("option", { name: `scripted — ${de("builtin.runner.command.title")}`, exact: true })).toBeAttached();
    await runner.selectOption("demo");
    await expect(status).toContainText(de("builtin.runner.demo.description"));
    await expect(status.locator(".runner-health")).toContainText(de("builtin.health.demo.ok"));
    await expect(form.getByRole("textbox", { name: de("builtin.param.demo.seed.label") })).toBeVisible();
    await expect(form).toContainText(de("builtin.param.demo.seed.help"));
    await runner.selectOption("scripted");
    await expect(status).toContainText(de("builtin.runner.command.description"));
    await expect(status.locator(".runner-health")).toContainText(de("builtin.health.command.ok"));
    await runner.selectOption("strict");
    await expect(status).toContainText(de("builtin.runner.robot-pool.description"));
    await expect(status.locator(".runner-health")).toContainText(/Robot Framework \d/);
    await expect(form.getByRole("checkbox", { name: de("builtin.param.robot-pool.allow_drift.label") })).toBeVisible();
    await expect(form).toContainText(de("builtin.param.robot-pool.allow_drift.help"));
    // None of that is anybody else's wording: nothing here is marked.
    await expect(page.locator(".run-panel .foreign")).toHaveCount(0);

    // A reason only the runner can give — in its words, said to be English, and marked for the eye.
    await runner.selectOption("offline");
    const reason = status.locator(".runner-health .foreign");
    await expect(reason).toContainText("suite root(s) not found");
    await expect(reason).toHaveAttribute("lang", "en");
    await expect(reason).toHaveAttribute("title", de("foreign.hint"));
    expect(await languageMark(reason)).toMatch(/^"en"/);

    // A value that does not have the declared form: a sentence in German, and the form itself once, as it is written.
    await runner.selectOption("scripted");
    await form.getByRole("combobox", { name: /^Target environment/ }).selectOption("staging");
    await form.getByRole("textbox", { name: /^Build number/ }).fill("build 11");
    await runButton(page, 3, "de").click();
    const field = form.locator('[data-param="build"]');
    await expect(field.getByRole("alert")).toHaveText(de("runner.problem.pattern"));
    await expect(field.locator(".pattern-hint")).toHaveText(`${de("runner.expectedForm")} \\d{4}\\.\\d{1,2}`);
    expect(await field.innerText()).not.toContain("\\\\");

    // A filter the server refuses: the sentence in German, the server's detail marked as the server's.
    await addRule(page, "regex:(", "de");
    const refused = page.getByTestId("filter-error");
    await expect(refused).toContainText(de("error.code.invalid-filter"));
    await expect(refused.locator(".foreign")).toContainText("regex:(");
    await expect(refused.locator(".foreign")).toHaveAttribute("lang", "en");
    expect(await languageMark(refused.locator(".foreign"))).toMatch(/^"en"/);
    await page.getByRole("button", { name: de("filter.removeRule", { pattern: "regex:(" }) }).click();
    await expectPreview(page, 3, 60, "de");

    // A plan: the frame is German, the runner's notes and its own account of the plan are the runner's.
    await runner.selectOption("demo");
    await page.getByRole("group", { name: de("compose.runBar") }).getByRole("button", { name: de("compose.plan"), exact: true }).click();
    const dialog = page.getByRole("dialog", { name: de("plan.title") });
    await expect(dialog).toContainText(de("plan.intro", { count: 3, runner: "demo" }));
    const note = dialog.locator(".plan-notes .foreign").first();
    await expect(note).toContainText("simulated");
    await expect(note).toHaveAttribute("lang", "en");
    expect(await languageMark(note)).toMatch(/^"en"/);
    await dialog.locator(".plan-text summary").click();
    await expect(dialog.locator(".plan-text pre.foreign")).toHaveAttribute("lang", "en");

    // In English the same texts are simply text: said to be English, and not set apart.
    await dialog.getByRole("button", { name: de("action.close") }).click();
    await page.getByRole("combobox", { name: de("prefs.language") }).selectOption("en");
    await runner.selectOption("offline");
    await expect(reason).toHaveAttribute("lang", "en");
    expect(await languageMark(reason)).toBe("none");
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

  // Tab on to the filter and type a tag into it.
  const field = page.getByRole("combobox", { name: msg("filter.addTag"), exact: true });
  await tabTo(page, field);
  await keys.type("Auth");
  await keys.press("Enter");
  await expectPreview(page, 12, 60);
  // Focus stays in the field, ready for the next rule.
  await expect(field).toBeFocused();

  // The picker is two stops on. Open it and tick a tag in the tree.
  const pick = page.getByRole("button", { name: msg("filter.pick"), exact: true });
  await tabTo(page, pick, { within: 3 });
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
  // Tab stays inside the dialog and reaches its buttons.
  await tabTo(page, dialog.getByRole("button", { name: msg("picker.apply") }), { within: 8 });
  await keys.press("Enter");
  await expect(dialog).toBeHidden();
  // Focus goes back to where the dialog was opened from — also after Escape.
  await expect(pick).toBeFocused();
  await keys.press("Enter");
  await expect(dialog.getByRole("searchbox")).toBeFocused();
  await keys.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(pick).toBeFocused();
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND Smoke");
  const expected = await api.post<{ count: number }>("/selection/compile", { tag_filter: { op: "AND", items: ["Auth", "Smoke"] }, limit: 0 });
  await expectPreview(page, expected.count, 60);

  // A rule is three stops in the order it reads: the pattern, NOT, Remove.
  // Walk back to the rule, negate it and take that back.
  const smoke = {
    pattern: page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Smoke" }) }),
    not: page.getByRole("button", { name: msg("filter.negateRule", { pattern: "Smoke" }) }),
    remove: page.getByRole("button", { name: msg("filter.removeRule", { pattern: "Smoke" }) }),
  };
  await tabTo(page, smoke.pattern, { back: true, within: 8 });
  await keys.press("Tab");
  await expect(smoke.not).toBeFocused();
  await keys.press(" ");
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND NOT Smoke");
  await keys.press(" ");
  await expect(page.getByTestId("filter-text")).toHaveText("Auth AND Smoke");
  // Removing a rule does not drop the focus on the floor.
  await keys.press("Tab");
  await expect(smoke.remove).toBeFocused();
  await keys.press("Enter");
  await expect(field).toBeFocused();
  await expectPreview(page, 12, 60);

  // On to the preview. Its rows are ONE stop for Tab: arrow keys move between them, Space ticks.
  const boxes = page.locator(".preview tbody tr[data-item-id]").getByRole("checkbox");
  await tabTo(page, page.getByRole("checkbox", { name: msg("preview.selectAll") }));
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
  const runner = page.getByRole("combobox", { name: msg("runner.label") });
  await expect(runner).toBeFocused();
  await expect(runner).toHaveValue("demo");

  // Tab on to the title, name the run, and start it with Enter.
  const title = page.getByRole("textbox", { name: msg("compose.title") });
  await tabTo(page, title, { within: 8 });
  await keys.type("Lauf per Tastatur");
  // The button that starts the run is a few stops further — reachable, not only clickable…
  const start = page.getByRole("button", { name: msg("compose.run", { count: 11 }), exact: true });
  await tabTo(page, start, { within: 12 });
  // …and so is the way back to the title.
  await tabTo(page, title, { back: true, within: 12 });
  await keys.press("Enter"); // Enter in the form starts the run
  const runId = await currentRunId(page);
  await api.waitFor(runId, ["COMPLETE"]);

  // On the run page, focus is in the content and every part is reachable.
  await expect(page.locator("main")).toBeFocused();
  await expect(page.getByRole("heading", { level: 1, name: "Lauf per Tastatur" })).toBeVisible();
  await tabTo(page, page.getByRole("link", { name: msg("run.tab.selection") }));
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
  await expect(page.locator(".verdict-table thead th[scope=col]")).toHaveCount(7);
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
    // The run bar is in reach without scrolling to it — and does not cover the form's last field.
    const start = runButton(page, 24);
    await expect(start).toBeInViewport({ ratio: 1 });
    const bar = page.getByRole("group", { name: msg("compose.runBar") });
    await expect(bar.getByRole("button", { name: msg("compose.plan"), exact: true })).toBeInViewport({ ratio: 1 });
    await expect(bar.getByRole("button", { name: msg("compose.export") })).toBeInViewport({ ratio: 1 });
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const lastField = page.getByRole("textbox", { name: msg("labels.label") });
    await expect(lastField).toBeInViewport({ ratio: 1 });
    expect((await lastField.boundingBox())!.y + (await lastField.boundingBox())!.height).toBeLessThanOrEqual((await bar.boundingBox())!.y);
    expect((await bar.boundingBox())!.width).toBeLessThanOrEqual(375);
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
