// A real runner: Robot Framework executes the example suite in worker
// processes, fanned out over two partitions. Live per-item status, per-shard
// results with their own log and report, and cancelling a run in flight.

import type { Page } from "@playwright/test";

import { currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

test.use({ config: "robot" });

const status = (page: Page) => page.getByTestId("run-status");

/** Select the `Checkout` tests, then keep only those whose name contains one of `names`. */
async function selectCheckoutTests(page: Page, names: string[]): Promise<void> {
  await expectPreview(page, 13, 58);
  await page.getByRole("checkbox", { name: msg("preview.selectAll") }).uncheck();
  const search = page.getByRole("searchbox", { name: msg("preview.search") });
  for (const name of names) {
    await search.fill(name);
    const row = page.locator(".preview tbody tr[data-item-id]");
    await expect(row).toHaveCount(1);
    await row.getByRole("checkbox").check();
  }
  await search.fill("");
}

test("run on two partitions: live progress, per-shard results, logs and reports", async ({ page, open, api, context }) => {
  await open("#/compose?f=Checkout");
  // One deliberately slow test (8 s) keeps the run in flight long enough to watch.
  await selectCheckoutTests(page, ["Slow Warehouse Sync", "Guest Checkout Completes", "Pickup Point Can Be Chosen"]);
  const form = page.getByRole("group", { name: msg("runner.parameters") });
  const partitions = form.getByRole("group", { name: "Partitions" });
  await expect(partitions.getByRole("checkbox", { name: "chromium" })).toBeChecked();
  await expect(partitions.getByRole("checkbox", { name: "firefox" })).toBeChecked();
  await runButton(page, 3).click();
  const runId = await currentRunId(page);

  // In flight: both shards announced, with what each owes.
  await expect(status(page)).toContainText(msg("runState.RUNNING"));
  const shards = page.locator(".shard-card");
  await expect(shards).toHaveCount(2);
  await expect(page.locator('.shard-card[data-shard="chromium-1"]')).toContainText(msg("run.shardRunning"));
  // The quick tests are reported live while the slow one still runs.
  const progress = page.getByTestId("run-progress");
  await expect(progress).toHaveText(/^[1-6] of 6 /);
  await expect(page.locator(".verdict-table .live-dot").first()).toBeVisible();
  expect((await api.run(runId)).state).toBe("RUNNING");

  // Over: each partition has its own verdict, numbers and files.
  await expect(status(page)).toContainText(msg("runState.COMPLETE"), { timeout: 60_000 });
  await expect(status(page)).toContainText(msg("completion.PASS"));
  await expect(progress).toHaveText(msg("progress.label", { done: 6, total: 6 }));
  for (const shard of ["chromium-1", "firefox-1"]) {
    const card = page.locator(`.shard-card[data-shard="${shard}"]`);
    await expect(card).toContainText(msg("completion.PASS"));
    await expect(card).toContainText(msg("run.shardProgress", { done: 3, total: 3 }));
    await expect(card.getByRole("link", { name: /^log\.html/ })).toBeVisible();
    await expect(card.getByRole("link", { name: /^report\.html/ })).toBeVisible();
  }
  // Every item is there once per partition, with shard, attempt and duration.
  const rows = page.locator(".verdict-table tbody tr[data-item-id]");
  await expect(rows).toHaveCount(6);
  await expect(page.locator(".verdict-table .live-dot")).toHaveCount(0);
  await page.getByRole("combobox", { name: msg("run.shard"), exact: true }).selectOption("firefox-1");
  await expect(rows).toHaveCount(3);
  await expect(rows.first().locator(".col-shard")).toHaveText("firefox-1");
  await expect(rows.first().locator(".col-attempt")).toHaveText("1");
  const slow = page.locator('.verdict-table tr[data-item-id$="Slow Warehouse Sync Finishes"]');
  await expect(slow.locator(".col-duration")).toHaveText(/^\d+(\.\d)? s$/);

  // The log opens — the real file Robot Framework wrote for that partition.
  const [log] = await Promise.all([
    context.waitForEvent("page"),
    page.locator('.shard-card[data-shard="firefox-1"]').getByRole("link", { name: /^log\.html/ }).click(),
  ]);
  await log.waitForLoadState();
  expect(log.url()).toContain(`/artifacts/${runId}/`);
  expect(log.url()).toContain("/firefox-1/log.html");
  const response = await page.request.get(log.url());
  expect(response.status()).toBe(200);
  expect(await response.text()).toContain("Slow Warehouse Sync Finishes");
  await log.close();
});

test("one partition only, and a failing test is reported red on it", async ({ page, open, api }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 13, 58);
  const partitions = page.getByRole("group", { name: "Partitions" });
  await partitions.getByRole("checkbox", { name: "firefox" }).uncheck();
  await runButton(page, 13).click();
  const runId = await currentRunId(page);
  await expect(status(page)).toContainText(msg("completion.FAIL"), { timeout: 60_000 });
  // One partition, cut into two chunks for the two workers: two shards, none of them firefox.
  await expect(page.locator(".shard-card")).toHaveCount(2);
  await expect(page.locator('.shard-card[data-shard^="chromium-"]')).toHaveCount(2);
  const done = await api.run(runId);
  expect(done.verdict_summary).toEqual({ PASS: 12, FAIL: 1 });

  // The failure is the first row, with the framework's own message.
  const first = page.locator(".verdict-table tbody tr[data-item-id]").first();
  await expect(first).toHaveAttribute("data-status", "FAIL");
  await expect(first).toContainText("Expired Card Is Rejected Loudly");
  await expect(first.locator(".col-message")).toContainText("simulated defect");
  // And it is what "re-run the failures" offers.
  await expect(page.getByRole("button", { name: msg("run.rerunFailed", { count: 1 }) })).toBeVisible();
});

test("cancel a real run in flight", async ({ page, open, api }) => {
  await open("#/compose?f=Checkout");
  // The 14-second test: plenty of time to cancel.
  await selectCheckoutTests(page, ["Slow Invoice Batch", "Guest Checkout Completes"]);
  await runButton(page, 2).click();
  const runId = await currentRunId(page);
  await expect(status(page)).toContainText(msg("runState.RUNNING"));
  // Wait until the workers are really executing: the quick test has reported.
  await expect(page.locator(".verdict-table .live-dot").first()).toBeVisible();

  await page.getByRole("button", { name: msg("run.cancel") }).click();
  await page.getByRole("dialog", { name: msg("run.cancelTitle") }).getByRole("button", { name: msg("run.cancelConfirm") }).click();
  await expect(status(page)).toContainText(msg("runState.CANCELLED"), { timeout: 60_000 });
  const cancelled = await api.run(runId);
  expect(cancelled.state).toBe("CANCELLED");
  expect(cancelled.completion).toBeNull();
});
