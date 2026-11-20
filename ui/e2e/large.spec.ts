// Thousands of items: the preview, the picker and a run's results render the
// rows in view, not the rows there are.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

test.use({ config: "large" });

test("reloading the catalog picks up a corpus that changed on disk", async ({ page, open, server }) => {
  await open("#/compose?f=Smoke");
  await expectPreview(page, 750, 3000);
  // Somebody edits the corpus: three tests are left.
  const items = [1, 2, 3].map((number, index) => ({
    id: `Shop.Cart.Core.T000${number}`,
    name: `Cart check ${number}`,
    tags: ["Cart", index === 0 ? "Smoke" : "Regression"],
    hierarchy: ["Shop", "Cart", "Core"],
  }));
  writeFileSync(join(server.directory, "corpus.json"), JSON.stringify({ items }));
  await page.getByRole("button", { name: msg("compose.reloadCatalog") }).click();
  await expect(page.getByText(msg("compose.catalogReloaded", { count: 3 }))).toBeVisible();
  // The preview, and the tags offered, are those of the new corpus.
  await expectPreview(page, 1, 3);
  await page.getByRole("button", { name: msg("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  await expect(dialog.getByRole("treeitem", { name: /^Smoke/ })).toContainText(msg("picker.items", { count: 1 }));
  await expect(dialog.getByRole("treeitem", { name: /^Payments/ })).toHaveCount(0);
});

test("3,000 items: preview, picker and results stay small in the DOM and usable", async ({ page, open, api }) => {
  await open();
  await expect(page.getByText(msg("compose.landing.catalog", { items: 3000, tags: 310 }))).toBeVisible();
  await page.getByRole("button", { name: msg("compose.landing.all", { count: 3000 }) }).click();
  await expectPreview(page, 3000, 3000);

  // All 3,000 match; only a screenful is rendered.
  const rows = page.locator(".preview tbody tr[data-item-id]");
  expect(await rows.count()).toBeLessThan(80);
  await expect(page.locator(".preview-table")).toHaveAttribute("aria-rowcount", "3001");
  await expect(rows.first()).toContainText("Payments check 1");

  // Scrolling to the end shows the last item.
  await page.locator(".preview-scroll").evaluate((element) => element.scrollTo(0, element.scrollHeight));
  await expect(page.locator('.preview tr[data-item-id="Shop.Auth.Suite60.T3000"]')).toBeVisible();
  expect(await rows.count()).toBeLessThan(80);

  // Search and grouping work over the whole set.
  await page.getByRole("searchbox", { name: msg("preview.search") }).fill("check 2999");
  await expect(rows).toHaveCount(1);
  await page.getByRole("searchbox", { name: msg("preview.search") }).fill("");
  await page.getByRole("checkbox", { name: msg("preview.group") }).check();
  await page.locator(".preview-scroll").evaluate((element) => element.scrollTo(0, 0));
  await expect(page.locator(".preview tbody tr.group-row").first()).toContainText(msg("preview.items", { count: 10 }));
  // Untick a whole suite at once.
  await page.locator(".preview tbody tr.group-row").first().getByRole("checkbox").uncheck();
  await expect(runButton(page, 2990)).toBeEnabled();
  await page.getByRole("button", { name: msg("preview.clearExclusions") }).click();

  // The picker: hundreds of tags, a screenful of rows, and search finds the one wanted.
  await page.getByRole("button", { name: msg("filter.pick") }).click();
  const dialog = page.getByRole("dialog", { name: msg("picker.title") });
  await dialog.getByRole("treeitem", { name: new RegExp(`^${msg("picker.unassigned")}`) }).click();
  expect(await dialog.getByRole("treeitem").count()).toBeLessThan(60);
  await dialog.getByRole("searchbox").fill("Checkout-2");
  // Natural order: Checkout-2 before Checkout-20.
  const labels = await dialog.locator(".tree-label").allTextContents();
  expect(labels.indexOf("Checkout-2")).toBeGreaterThan(-1);
  expect(labels.indexOf("Checkout-2")).toBeLessThan(labels.indexOf("Checkout-20"));
  await dialog.getByRole("button", { name: msg("action.cancel") }).click();
  await expect(dialog).toBeHidden();

  // Run all of it; the results table is windowed too.
  await page.getByRole("button", { name: msg("filter.clear") }).click();
  await addRule(page, "Regression");
  await expectPreview(page, 2250, 3000);
  await runButton(page, 2250).click();
  const runId = await currentRunId(page);
  const done = await api.waitFor(runId, ["COMPLETE"], 120_000);
  expect(done.planned_count).toBe(2250);
  await expect(page.getByTestId("run-status")).toContainText(msg("runState.COMPLETE"));
  const verdicts = page.locator(".verdict-table tbody tr[data-item-id]");
  await expect(verdicts.first()).toHaveAttribute("data-status", "FAIL");
  expect(await verdicts.count()).toBeLessThan(80);
  await expect(page.locator(".verdict-table")).toHaveAttribute("aria-rowcount", "2251");
  await page.getByRole("button", { name: new RegExp(`^${msg("run.filter.skipped")}`) }).click();
  await expect(verdicts.first()).toHaveAttribute("data-status", "SKIP");
});
