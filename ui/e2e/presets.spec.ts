// What a deployment sets up for its testers: a default filter, quick filters
// (also whole expressions), presets — and a runner that says what it checked.

import { currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

interface Compiled {
  count: number;
}

test.describe("a deployment with presets", () => {
  test.use({ config: "presets" });

  const DEFAULT = { op: "AND", items: ["Smoke", { not: "prefix:Quarantine-" }] };

  test("the default filter is where a new selection starts — and only then", async ({ page, open, api }) => {
    const byDefault = await api.post<Compiled>("/selection/compile", { tag_filter: DEFAULT, limit: 0 });
    await open();
    await expect(page.getByTestId("filter-text")).toHaveText("Smoke AND NOT prefix:Quarantine-");
    await expectPreview(page, byDefault.count, 60);

    // A link that carries a selection is that selection, not the default.
    await open("#/compose?f=Payments");
    await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Payments" }) })).toBeVisible();
    await expect(page.getByRole("button", { name: msg("filter.editPattern", { pattern: "Smoke" }) })).toHaveCount(0);
    await expectPreview(page, 17, 60);

    // Coming back within the session returns to what was there, not to the default…
    await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: msg("nav.runs") }).click();
    await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: msg("nav.compose") }).click();
    await expectPreview(page, 17, 60);
    // …and "Start over" is a new selection again.
    await page.getByRole("button", { name: msg("compose.reset") }).click();
    await expect(page.getByTestId("filter-text")).toHaveText("Smoke AND NOT prefix:Quarantine-");
  });

  test("a preset fills in everything: selection, runner, its options, title, labels", async ({ page, open, api }) => {
    await open("#/compose?f=Payments");
    await expectPreview(page, 17, 60);
    await page.getByRole("combobox", { name: msg("compose.preset") }).selectOption({ label: "Nightly regression" });

    const expected = await api.post<Compiled>("/selection/compile", { tag_filter: { op: "AND", items: ["Regression", { not: "prefix:Quarantine-" }] }, limit: 0 });
    await expect(page.locator(".notice").getByText(msg("compose.presetApplied", { label: "Nightly regression" }))).toBeVisible();
    await expect(page.getByTestId("filter-text")).toHaveText("Regression AND NOT prefix:Quarantine-");
    await expectPreview(page, expected.count, 60);
    await expect(page.getByRole("combobox", { name: msg("runner.label") })).toHaveValue("scripted");
    const form = page.getByRole("group", { name: msg("runner.parameters") });
    await expect(form.getByRole("combobox", { name: /^Target environment/ })).toHaveValue("staging");
    await expect(form.getByRole("textbox", { name: /^Build number/ })).toHaveValue("2026.11");
    await expect(form.getByRole("textbox", { name: "Retry count" })).toHaveValue("2");
    await expect(form.getByRole("checkbox", { name: "Run headless" })).not.toBeChecked();
    await expect(form.getByRole("checkbox", { name: "firefox" })).toBeChecked();
    await expect(form.getByRole("textbox", { name: msg("runner.mappingValueOf", { label: "Extra variables", number: 1 }) })).toHaveValue("en");
    await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("Nightly regression");
    await expect(page.getByRole("button", { name: msg("labels.remove", { label: "suite=nightly" }) })).toBeVisible();
    await expect(page.getByRole("button", { name: msg("labels.remove", { label: "env=staging" }) })).toBeVisible();

    // It is everything a run needs: Run, without touching anything else.
    await runButton(page, expected.count).click();
    const runId = await currentRunId(page);
    await api.waitFor(runId, ["COMPLETE"]);
    const detail = await api.get<{ title: string; labels: Record<string, string>; dispatches: { mode: string; parameters: Record<string, unknown> }[] }>(`/runs/${runId}`);
    expect(detail.title).toBe("Nightly regression");
    expect(detail.labels).toEqual({ suite: "nightly", env: "staging" });
    expect(detail.dispatches[0]!.mode).toBe("scripted");
    expect(detail.dispatches[0]!.parameters).toMatchObject({ environment: "staging", build: "2026.11", retries: 2, headless: false, browsers: ["firefox"], variables: { locale: "en" } });
  });

  test("quick filters are always at hand — next to a filter that is there, and on the empty builder", async ({ page, open, api }) => {
    const quick = page.getByRole("group", { name: msg("compose.quickFilters") });
    const expression = { op: "AND", items: [{ op: "OR", items: ["Catalog", "Cart"] }, { not: "prefix:Quarantine-" }] };

    // The builder already holds a filter (here: one from a link). The quick filters are there all the same.
    await open("#/compose?f=Payments");
    await expectPreview(page, 17, 60);
    await expect(quick.getByRole("button")).toHaveText(["Smoke", "Shop front without quarantine"]);
    // One that is a pattern is one more rule of the filter…
    await quick.getByRole("button", { name: "Smoke", exact: true }).click();
    await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
    await expectPreview(page, 4, 60);
    // …and one that is an expression is one more group of it.
    await quick.getByRole("button", { name: "Shop front without quarantine" }).click();
    await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke AND ((Catalog OR Cart) AND NOT prefix:Quarantine-)");
    const narrowed = await api.post<Compiled>("/selection/compile", { tag_filter: { op: "AND", items: ["Payments", "Smoke", expression] }, limit: 0 });
    await expectPreview(page, narrowed.count, 60);

    // They are next to the presets, not hidden while something is typed as text either.
    await page.getByRole("button", { name: msg("filter.editAsText") }).click();
    await expect(quick.getByRole("button")).toHaveCount(2);
    await page.locator(".expr-text").getByRole("button", { name: msg("action.cancel") }).click();

    // On the empty builder: the pattern starts a filter…
    await page.getByRole("button", { name: msg("filter.clear") }).click();
    await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
    await quick.getByRole("button", { name: "Smoke", exact: true }).click();
    await expectPreview(page, 14, 60);
    await page.getByRole("button", { name: msg("filter.clear") }).click();

    // …the one that is an expression becomes the filter, groups and NOT included.
    const expected = await api.post<Compiled>("/selection/compile", { tag_filter: expression, limit: 0 });
    await quick.getByRole("button", { name: "Shop front without quarantine" }).click();
    await expect(page.getByTestId("filter-text")).toHaveText("(Catalog OR Cart) AND NOT prefix:Quarantine-");
    await expect(page.getByRole("group", { name: msg("filter.group", { op: "OR" }) })).toBeVisible();
    await expectPreview(page, expected.count, 60);

    // A preset from its card on the empty builder.
    await page.getByRole("button", { name: msg("filter.clear") }).click();
    const card = page.locator('[data-preset="payments"]');
    await expect(card).toContainText("Payments check");
    await card.getByRole("button", { name: msg("compose.usePresetNamed", { label: "Payments check" }) }).click();
    await expectPreview(page, 17, 60);
    await expect(page.getByRole("combobox", { name: msg("runner.label") })).toHaveValue("demo");
    await expect(page.getByRole("group", { name: msg("runner.parameters") }).getByRole("textbox", { name: "Seed" })).toHaveValue("a");
    await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("Payments check");
  });
});

test.describe("selections saved in the browser", () => {
  test.use({ config: "presets" });

  const KEY = "runcomposer.saved-selections.v1";
  const NAME = "Payments before a release";
  const FILTER = "Payments AND NOT prefix:Quarantine-";

  test("save what Compose holds under a name, find it after a reload, use it, rename it, delete it", async ({ page, open, api, browser, server }) => {
    const matched = await api.post<Compiled & { items: { id: string }[] }>("/selection/compile", { tag_filter: { op: "AND", items: ["Payments", { not: "prefix:Quarantine-" }] } });
    await open("#/compose?f=" + encodeURIComponent(FILTER));
    await expectPreview(page, matched.count, 60);

    // Everything a run needs: an item unticked, a runner with its options, a title, a label.
    await page.locator(".preview tbody tr[data-item-id]").first().getByRole("checkbox").uncheck();
    await page.getByRole("combobox", { name: msg("runner.label") }).selectOption("scripted");
    const form = page.getByRole("group", { name: msg("runner.parameters") });
    await form.getByRole("combobox", { name: /^Target environment/ }).selectOption("preview");
    await form.getByRole("textbox", { name: /^Build number/ }).fill("2026.12");
    await form.getByRole("textbox", { name: "Retry count" }).fill("3");
    await page.getByRole("textbox", { name: msg("compose.title") }).fill("Release check");
    const labels = page.getByRole("textbox", { name: msg("labels.label") });
    await labels.fill("suite=release");
    await labels.press("Enter");

    // Save it. The dialog says where it is kept, and what a selection for everybody would be.
    await page.getByRole("button", { name: msg("saved.open") }).click();
    const dialog = page.getByRole("dialog", { name: msg("saved.title") });
    await expect(dialog).toContainText(msg("saved.where"));
    await expect(dialog.getByText(msg("saved.empty"))).toBeVisible();
    const save = dialog.getByRole("button", { name: msg("saved.save") });
    await expect(save).toBeDisabled();
    await dialog.getByRole("textbox", { name: msg("saved.name") }).fill(NAME);
    await save.click();
    const entry = dialog.locator(`[data-saved="${NAME}"]`);
    await expect(entry).toContainText(FILTER);
    await expect(entry).toContainText(msg("compose.withRunner", { runner: "scripted" }));
    await dialog.getByRole("button", { name: msg("action.close") }).click();

    // It is kept in this browser's storage — whole — and nowhere on the server.
    const stored = await page.evaluate((key) => JSON.parse(window.localStorage.getItem(key) ?? "[]") as Record<string, unknown>[], KEY);
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({
      name: NAME,
      filter: FILTER,
      excluded: [matched.items[0]!.id],
      runner: "scripted",
      title: "Release check",
      labels: { suite: "release" },
    });
    expect(stored[0]!.params).toMatchObject({ environment: "preview", build: "2026.12" });
    expect((await api.get<{ presets: { name: string }[] }>("/presets")).presets.map((preset) => preset.name)).toEqual(["nightly", "payments"]);

    // Listed with the deployment's presets, under a heading of its own — also after a reload.
    await page.getByRole("button", { name: msg("compose.reset") }).click();
    await page.reload();
    const select = page.getByRole("combobox", { name: msg("compose.preset") });
    await expect(select.locator(`optgroup[label="${msg("compose.presetsOfDeployment")}"] option`)).toHaveText(["Nightly regression", "Payments check"]);
    await expect(select.locator(`optgroup[label="${msg("saved.inThisBrowser")}"] option`)).toHaveText([NAME]);

    // Using it brings back all of it.
    await select.selectOption({ label: NAME });
    await expect(page.locator(".notice").getByText(msg("saved.applied", { name: NAME }))).toBeVisible();
    await expect(page.getByTestId("filter-text")).toHaveText(FILTER);
    await expectPreview(page, matched.count, 60);
    await expect(page.getByTestId("run-summary")).toHaveText(msg("compose.summary", { selected: matched.count - 1, matched: matched.count, total: 60 }));
    await expect(page.locator(".preview tbody tr[data-item-id]").first().getByRole("checkbox")).not.toBeChecked();
    await expect(page.getByRole("combobox", { name: msg("runner.label") })).toHaveValue("scripted");
    await expect(form.getByRole("combobox", { name: /^Target environment/ })).toHaveValue("preview");
    await expect(form.getByRole("textbox", { name: /^Build number/ })).toHaveValue("2026.12");
    await expect(form.getByRole("textbox", { name: "Retry count" })).toHaveValue("3");
    await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("Release check");
    await expect(page.getByRole("button", { name: msg("labels.remove", { label: "suite=release" }) })).toBeVisible();
    // …and it runs as it was saved.
    await runButton(page, matched.count - 1).click();
    const runId = await currentRunId(page);
    await api.waitFor(runId, ["COMPLETE"]);
    const detail = await api.get<{ title: string; planned_count: number; labels: Record<string, string>; dispatches: { mode: string; parameters: Record<string, unknown> }[] }>(`/runs/${runId}`);
    expect(detail).toMatchObject({ title: "Release check", planned_count: matched.count - 1, labels: { suite: "release" } });
    expect(detail.dispatches[0]).toMatchObject({ mode: "scripted", parameters: { environment: "preview", build: "2026.12", retries: 3 } });

    // A second one; a name can be used once.
    await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: msg("nav.compose") }).click();
    await page.getByRole("button", { name: msg("saved.open") }).click();
    await dialog.getByRole("textbox", { name: msg("saved.name") }).fill("Second look");
    await dialog.getByRole("button", { name: msg("saved.save") }).click();
    await expect(dialog.locator("[data-saved]")).toHaveCount(2);
    // The name of one that exists: saving says it will replace it.
    await dialog.getByRole("textbox", { name: msg("saved.name") }).fill(NAME);
    await expect(dialog.getByRole("button", { name: msg("saved.replace") })).toBeVisible();
    await dialog.getByRole("textbox", { name: msg("saved.name") }).fill("");

    // Rename: not to a name that is taken…
    await dialog.getByRole("button", { name: msg("saved.renameNamed", { name: "Second look" }) }).click();
    const renameField = dialog.getByRole("textbox", { name: msg("saved.newName", { name: "Second look" }) });
    await renameField.fill(NAME);
    await renameField.press("Enter");
    await expect(dialog.getByRole("alert")).toHaveText(msg("saved.nameTaken"));
    // …to a free one.
    await renameField.fill("Release, second look");
    await renameField.press("Enter");
    await expect(dialog.locator('[data-saved="Release, second look"]')).toBeVisible();
    await expect(dialog.locator('[data-saved="Second look"]')).toHaveCount(0);

    // Delete, after being asked — and "Keep" keeps.
    await dialog.getByRole("button", { name: msg("saved.deleteNamed", { name: NAME }) }).click();
    await dialog.getByRole("button", { name: msg("saved.keep") }).click();
    await expect(dialog.locator(`[data-saved="${NAME}"]`)).toBeVisible();
    await dialog.getByRole("button", { name: msg("saved.deleteNamed", { name: NAME }) }).click();
    await expect(dialog.getByText(msg("saved.deleteAsk", { name: NAME }))).toBeVisible();
    await dialog.locator(`[data-saved="${NAME}"]`).getByRole("button", { name: msg("saved.delete"), exact: true }).click();
    await expect(dialog.locator("[data-saved]")).toHaveCount(1);
    await dialog.getByRole("button", { name: msg("action.close") }).click();
    await expect(select.locator(`optgroup[label="${msg("saved.inThisBrowser")}"] option`)).toHaveText(["Release, second look"]);
    expect(await page.evaluate((key) => (JSON.parse(window.localStorage.getItem(key) ?? "[]") as { name: string }[]).map((item) => item.name), KEY)).toEqual(["Release, second look"]);

    // Another browser has none of it: the deployment's presets only.
    const other = await browser.newContext();
    const elsewhere = await other.newPage();
    await elsewhere.goto(`${server.url}/#/compose`);
    const theirs = elsewhere.getByRole("combobox", { name: msg("compose.preset") });
    await expect(theirs.locator(`optgroup[label="${msg("compose.presetsOfDeployment")}"] option`)).toHaveCount(2);
    await expect(theirs.locator(`optgroup[label="${msg("saved.inThisBrowser")}"]`)).toHaveCount(0);
    await other.close();
  });

  test("a browser that keeps nothing says so — and everything else still works", async ({ page, open }) => {
    // Storage that refuses to write: private modes and policies do this.
    await page.addInitScript(() => {
      Storage.prototype.setItem = () => {
        throw new Error("this browser keeps nothing");
      };
    });
    await open("#/compose?f=Payments");
    await expectPreview(page, 17, 60);
    await page.getByRole("button", { name: msg("saved.open") }).click();
    const dialog = page.getByRole("dialog", { name: msg("saved.title") });
    await dialog.getByRole("textbox", { name: msg("saved.name") }).fill(NAME);
    await dialog.getByRole("button", { name: msg("saved.save") }).click();
    await expect(dialog.getByText(msg("saved.failed"))).toBeVisible();
    await expect(dialog.locator("[data-saved]")).toHaveCount(0);
    await dialog.getByRole("button", { name: msg("action.close") }).click();
    await expect(runButton(page, 17)).toBeEnabled();
  });
});

test.describe("a runner that reports what it checked", () => {
  test.use({ config: "ci" });

  test("its health is shown check by check — also for the runner whose job is missing", async ({ page, open }) => {
    await open();
    const select = page.getByRole("combobox", { name: msg("runner.label") });
    await select.selectOption("ci-nightly");
    const checks = page.getByRole("list", { name: msg("runner.checks") });
    await expect(checks.getByRole("listitem")).toHaveCount(2);
    await expect(checks.locator('li[data-ok="true"]')).toHaveCount(2);
    await expect(checks).toContainText("server reachable");
    await expect(checks).toContainText("job 'shop-nightly' readable");
    await expect(page.locator(".runner-health")).toContainText(msg("health.ok"));

    await select.selectOption("ci-missing");
    await expect(page.locator(".runner-health")).toContainText(msg("health.degraded"));
    await expect(checks.locator('li[data-ok="true"]')).toHaveCount(1);
    const failed = checks.locator('li[data-ok="false"]');
    await expect(failed).toContainText(msg("runner.checkFailed"));
    await expect(failed).toContainText("job 'shop-weekly' readable");
    await expect(failed).toContainText("404");
    await expect(page.locator(".runner-status")).toContainText(msg("runner.unhealthyHint"));
  });
});
