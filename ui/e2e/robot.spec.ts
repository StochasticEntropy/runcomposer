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
  // It renders: the framework's log is an application of its own, and it runs.
  await expect(log).toHaveTitle(/Log$/);
  await expect(log.locator("#s1")).toBeVisible();
  await expect(log.getByRole("heading", { name: /Log$/ }).first()).toBeVisible();
  const response = await page.request.get(log.url());
  expect(response.status()).toBe(200);
  expect(await response.text()).toContain("Slow Warehouse Sync Finishes");
  // …inside a sandbox that is not this server's origin.
  expect(response.headers()["content-security-policy"]).toContain("sandbox allow-scripts");
  expect(response.headers()["content-security-policy"]).not.toContain("allow-same-origin");
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

test("a run in lanes: the plan, and each region has its own results, and so has each shard", async ({ page, open, api }) => {
  await open("#/compose?f=Checkout");
  await expectPreview(page, 13, 58);
  await page.getByRole("combobox", { name: msg("runner.label") }).selectOption("robot-lanes");
  // A mapping the runner declares, handed to the tests as variables.
  const variables = page.getByRole("group", { name: "Extra variables" });
  await variables.getByRole("button", { name: msg("runner.mappingAdd") }).click();
  await variables.getByRole("textbox", { name: msg("runner.mappingKeyOf", { label: "Extra variables", number: 1 }) }).fill("locale");
  await variables.getByRole("textbox", { name: msg("runner.mappingValueOf", { label: "Extra variables", number: 1 }) }).fill("de");

  // The plan: 13 tests distributed over two lanes, two tests per shard.
  await page.getByRole("button", { name: msg("compose.plan"), exact: true }).click();
  const planDialog = page.getByRole("dialog", { name: msg("plan.title") });
  const plan = planDialog.getByTestId("plan");
  await expect(plan).toContainText(
    `${msg("plan.summary", { count: 13 })} ${msg("plan.inShards", { count: 7 })} ${msg("plan.inGroups", { count: 2 })}`,
  );
  await expect(plan.locator("[data-plan-group]")).toHaveCount(2);
  await expect(plan.locator('[data-plan-group="eu"]')).toContainText("eu");
  await expect(plan.locator('[data-plan-shard="eu-01"] td').first()).toHaveText("eu");
  await expect(plan.locator("[data-plan-shard]")).toHaveCount(7);
  await expect(plan.locator(".plan-notes")).toContainText("cold start");
  expect(await api.runs()).toHaveLength(0);
  await planDialog.getByRole("button", { name: msg("compose.run", { count: 13 }) }).click();
  const runId = await currentRunId(page);

  // In flight: a roll-up per group, each with its shards; the slow tests are RUNNING.
  const groups = page.locator(".group-card");
  await expect(groups).toHaveCount(2);
  await expect(page.locator('.verdict-table tbody tr[data-status="RUNNING"]').first()).toBeVisible();
  await expect(page.getByTestId("run-running")).toBeVisible();
  const eu = page.locator('.group-card[data-group="eu"]');
  await expect(eu).toContainText(msg("run.shardRunning"));
  // What is running is never in the count of results.
  const live = await api.get<{ state: string; live_summary: Record<string, Record<string, number>>; verdict_summary: Record<string, number> }>(`/runs/${runId}`);
  if (live.state === "RUNNING") expect(Object.keys(live.verdict_summary)).not.toContain("RUNNING");

  await expect(status(page)).toContainText(msg("runState.COMPLETE"), { timeout: 90_000 });
  // The mapping reached the dispatch as declared.
  const detail = await api.get<{
    groups: { group: string; declared_shards: number; delivered_shards: number; count: number }[];
    shard_groups: Record<string, string>;
    verdicts: { item_id: string; status: string; shard: string }[];
    dispatches: { parameters: Record<string, unknown> }[];
    artifacts: { kind: string; name: string }[];
  }>(`/runs/${runId}`);
  expect(detail.dispatches[0]!.parameters.variables).toEqual({ locale: "de" });
  for (const group of detail.groups) {
    const card = page.locator(`.group-card[data-group="${group.group}"]`);
    await expect(card).toContainText(msg("run.groupShards", { done: group.declared_shards, total: group.declared_shards }));
    await expect(card.locator(".shard-card")).toHaveCount(group.declared_shards);
  }

  // One test of this selection fails. What failed is where the eye lands:
  // its group is the first and open, with the failing shard first in it…
  const failed = detail.verdicts.filter((verdict) => verdict.status === "FAIL");
  expect(failed).toHaveLength(1);
  const troubled = detail.shard_groups[failed[0]!.shard]!;
  const passed = detail.groups.find((group) => group.group !== troubled)!;
  await expect(groups.first()).toHaveAttribute("data-group", troubled);
  const bad = page.locator(`.group-card[data-group="${troubled}"]`);
  await expect(bad.locator("details.group-shards")).toHaveJSProperty("open", true);
  await expect(bad.locator(".shard-card").first()).toHaveAttribute("data-shard", failed[0]!.shard);
  // Each shard keeps its own log, inside its group.
  await expect(bad.locator(".shard-card").first().getByRole("link", { name: /^log\.html/ })).toBeVisible();
  // …and the group that passed is one line: folded, not gone.
  const good = page.locator(`.group-card[data-group="${passed.group}"]`);
  await expect(good).toContainText(msg("completion.PASS"));
  await expect(good.locator("details.group-shards")).toHaveJSProperty("open", false);
  await expect(good.locator(".shard-card").first()).toBeHidden();
  await good.locator("details.group-shards > summary").click();
  await expect(good.locator(".shard-card").first().getByRole("link", { name: /^log\.html/ })).toBeVisible();

  // From the top of the results, one step leads to what failed — past all the groups and shards.
  const jump = page.getByTestId("jump-to-results");
  await expect(jump).toHaveText(msg("run.jumpFailed", { count: 1 }));
  await jump.click();
  await expect(page).toHaveURL(/status=failed/);
  await expect(page.getByRole("heading", { name: msg("run.verdicts") })).toBeFocused();
  await expect(page.getByRole("heading", { name: msg("run.verdicts") })).toBeInViewport();
  await expect(page.locator(".verdict-table tbody tr[data-item-id]")).toHaveCount(1);
  await page.getByRole("button", { name: new RegExp(`^${msg("run.filter.all")}`) }).click();

  // The failed item is the first row, and the row itself leads to the log and the report of its shard.
  const first = page.locator(".verdict-table tbody tr[data-item-id]").first();
  await expect(first).toHaveAttribute("data-status", "FAIL");
  await expect(first).toHaveAttribute("data-item-id", failed[0]!.item_id);
  const log = first.getByRole("link", { name: msg("run.fileOf", { file: "log", item: failed[0]!.item_id }) });
  await expect(log).toHaveText("log");
  await expect(log).toHaveAttribute("href", new RegExp(`/${failed[0]!.shard}/log\\.html$`));
  await expect(first.getByRole("link", { name: msg("run.fileOf", { file: "report", item: failed[0]!.item_id }) })).toHaveAttribute("href", new RegExp(`/${failed[0]!.shard}/report\\.html$`));
  const opened = await page.request.get(new URL((await log.getAttribute("href"))!, page.url()).href);
  expect(opened.status()).toBe(200);
  expect(await opened.text()).toContain("Stage Variable Reaches The Test");
  // A row of another shard links to that shard's files, not to these.
  const last = page.locator('.verdict-table tbody tr[data-item-id][data-status="PASS"]').first();
  const lastShard = (await last.locator(".col-shard").innerText()).trim();
  await expect(last.locator(".col-files a").first()).toHaveAttribute("href", new RegExp(`/${lastShard}/log\\.html$`));
  await expect(page.locator(".verdict-table .live-dot")).toHaveCount(0);
  await expect(page.locator('.verdict-table tbody tr[data-status="RUNNING"]')).toHaveCount(0);

  // A recorded directory is listed, but is nothing to open: no link.
  expect(detail.artifacts.some((artifact) => artifact.kind === "directory")).toBe(true);
  await expect(page.locator('[data-artifact-kind="directory"]').first()).toBeVisible();
  await expect(page.getByRole("link", { name: /^files \(/ })).toHaveCount(0);

  // The results can be read one group at a time; the choice is in the URL.
  const us = detail.groups.find((group) => group.group === "us")!;
  await page.getByRole("combobox", { name: msg("run.group"), exact: true }).selectOption("us");
  await expect(page).toHaveURL(/group=us/);
  await expect(page.getByRole("button", { name: new RegExp(`^${msg("run.filter.all")}\\s+${us.count}$`) })).toBeVisible();
  const shardOptions = page.getByRole("combobox", { name: msg("run.shard"), exact: true }).getByRole("option");
  await expect(shardOptions).toHaveCount(us.declared_shards + 1);
});

test("after a cancel a lane and its shards say the same about what was reported", async ({ page, open, api }) => {
  // Thirteen tests in two lanes, two per shard; the slow ones keep their shards busy long enough to cancel.
  await open("#/compose?f=Checkout");
  await expectPreview(page, 13, 58);
  await page.getByRole("combobox", { name: msg("runner.label") }).selectOption("robot-lanes");
  await runButton(page, 13).click();
  const runId = await currentRunId(page);
  // Cancel once a shard has delivered and others are still at work: a run that is partly reported.
  await expect
    .poll(async () => (await api.get<{ shards: { delivered: boolean }[]; state: string }>(`/runs/${runId}`)).shards.filter((shard) => shard.delivered).length, { timeout: 60_000 })
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: msg("run.cancel") }).click();
  await page.getByRole("dialog", { name: msg("run.cancelTitle") }).getByRole("button", { name: msg("run.cancelConfirm") }).click();
  await expect(status(page)).toContainText(msg("runState.CANCELLED"), { timeout: 60_000 });

  interface Rolled {
    count: number;
    planned: number;
    summary: Record<string, number>;
  }
  const detail = await api.get<{ groups: (Rolled & { group: string; shards: string[] })[]; shards: (Rolled & { shard: string; delivered: boolean })[] }>(`/runs/${runId}`);
  const missing = detail.shards.filter((shard) => !shard.delivered);
  expect(missing.length, "the run was cancelled before every shard delivered").toBeGreaterThan(0);
  for (const group of detail.groups) {
    const members = detail.shards.filter((shard) => group.shards.includes(shard.shard));
    // The server counts reported items one way: a group's count is its shards' counts, and holds nothing MISSING.
    expect(group.count).toBe(members.reduce((sum, shard) => sum + shard.count, 0));
    expect(group.count).toBe(Object.values(group.summary).reduce((sum, value) => sum + value, 0) - (group.summary.MISSING ?? 0));

    // The page says that number for the group — and its shard cards add up to it.
    const card = page.locator(`.group-card[data-group="${group.group}"]`);
    await expect(card.getByTestId("group-reported")).toContainText(msg("run.shardProgress", { done: group.count, total: group.planned }));
    const shardLines = await card.locator(".shard-card .shard-numbers").evaluateAll((lines) => lines.map((line) => Number.parseInt(line.textContent ?? "", 10)));
    expect(shardLines).toHaveLength(members.length);
    expect(shardLines.reduce((sum, value) => sum + value, 0)).toBe(group.count);
  }
  // Somewhere a lane is short of its plan: that is what "cancelled" looks like, and no line says otherwise.
  expect(detail.groups.some((group) => group.count < group.planned)).toBe(true);
  // The head of the run says the same total.
  const reported = detail.groups.reduce((sum, group) => sum + group.count, 0);
  await expect(page.getByTestId("run-progress")).toHaveText(msg("progress.label", { done: reported, total: 13 }));
});
