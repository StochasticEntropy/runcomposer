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
    await page.getByRole("combobox", { name: msg("compose.preset") }).selectOption("nightly");

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

  test("presets and quick filters are offered on the empty builder; a quick filter can be a whole expression", async ({ page, open, api }) => {
    await open();
    await page.getByRole("button", { name: msg("filter.clear") }).click();
    await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();

    // The quick filter that is one pattern adds a rule…
    await page.getByRole("button", { name: /^Smoke/ }).first().click();
    await expectPreview(page, 14, 60);
    await page.getByRole("button", { name: msg("filter.clear") }).click();

    // …the one that is an expression becomes the filter, groups and NOT included.
    const expression = { op: "AND", items: [{ op: "OR", items: ["Catalog", "Cart"] }, { not: "prefix:Quarantine-" }] };
    const expected = await api.post<Compiled>("/selection/compile", { tag_filter: expression, limit: 0 });
    await page.getByRole("button", { name: "Shop front without quarantine" }).click();
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
