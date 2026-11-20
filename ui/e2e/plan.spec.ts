// Plan previews: what a run would do, read before it is started — and never
// the plan of something other than what is on screen.

import { currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

const planButton = (page: import("@playwright/test").Page) => page.getByRole("button", { name: msg("compose.plan"), exact: true });
const runnerSelect = (page: import("@playwright/test").Page) => page.getByRole("combobox", { name: msg("runner.label") });

test("plan a selection before running it: shards, estimates, the runner's notes — then run from the plan", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("demo");
  await page.getByRole("group", { name: msg("runner.parameters") }).getByRole("textbox", { name: "Seed" }).fill("a");
  await planButton(page).click();

  const dialog = page.getByRole("dialog", { name: msg("plan.title") });
  await expect(dialog).toContainText(msg("plan.intro", { count: 3, runner: "demo" }));
  const plan = dialog.getByTestId("plan");
  await expect(plan).toHaveAttribute("data-refused", "false");
  await expect(plan).toContainText(`${msg("plan.summary", { count: 3 })} ${msg("plan.inShards", { count: 1 })}`);
  await expect(plan).toContainText("simulated: no test would be executed");
  // The shard, what it would run, and how long the runner expects it to take.
  const shard = plan.locator('[data-plan-shard="1"]');
  await expect(shard.locator("td").first()).toHaveText("3");
  await expect(shard.locator("td").last()).toHaveText(/\d (ms|s)$/);
  await shard.locator("summary").click();
  await expect(shard).toContainText("Shop.Auth.SSO.T002");
  await dialog.locator(".plan-text summary").click();
  await expect(dialog.locator(".plan-text")).toContainText("with seed 'a'");
  // Looking at a plan starts nothing.
  expect(await api.runs()).toHaveLength(0);

  await dialog.getByRole("button", { name: msg("compose.run", { count: 3 }) }).click();
  const run = await api.waitFor(await currentRunId(page), ["COMPLETE"]);
  expect(run.planned_count).toBe(3);
});

test("a run the runner would refuse is shown as refused — by the plan, before anything is started", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("closed");
  await planButton(page).click();
  const dialog = page.getByRole("dialog", { name: msg("plan.title") });
  const plan = dialog.getByTestId("plan");
  await expect(plan).toHaveAttribute("data-refused", "true");
  await expect(plan.getByRole("alert")).toContainText(msg("plan.refused"));
  await expect(plan.getByRole("alert")).toContainText("working directory not found");
  await expect(plan.locator(".plan-shards")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: msg("compose.run", { count: 3 }) })).toBeDisabled();
  expect(await api.runs()).toHaveLength(0);
});

test("a plan is only offered by a runner that can plan", async ({ page, open }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("demo");
  await expect(planButton(page)).toBeEnabled();
  await runnerSelect(page).selectOption("plain");
  await expect(runButton(page, 3)).toBeEnabled();
  await expect(planButton(page)).toHaveCount(0);
});

test("an option the server refuses for a plan is said in the plan, naming the option", async ({ page, open }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("demo");
  // The form sends well-typed values; the request is altered on its way out.
  await page.route("**/api/v1/plan", async (route) => {
    const body = route.request().postDataJSON() as { parameters: Record<string, unknown> };
    body.parameters.seed = 7;
    await route.continue({ postData: JSON.stringify(body) });
  });
  await planButton(page).click();
  const dialog = page.getByRole("dialog", { name: msg("plan.title") });
  await expect(dialog.getByRole("alert")).toContainText(msg("error.code.invalid-parameter"));
  await expect(dialog.getByRole("alert")).toContainText("parameter 'seed' must be a string");
  await page.unroute("**/api/v1/plan");
  await dialog.getByRole("button", { name: msg("action.retry") }).click();
  await expect(dialog.getByTestId("plan")).toHaveAttribute("data-refused", "false");
});

test("the plan of a stored run follows its options — a late answer for other options is never shown", async ({ page, open, api }) => {
  const run = await api.finishedRun("Auth-SSO", "b", { title: "Anmeldung prüfen" });
  await open(`#/runs/${run.id}`);
  await page.getByRole("button", { name: msg("run.rerunAll") }).click();
  const dialog = page.getByRole("dialog", { name: msg("rerun.titleAll") });

  // The answer to the FIRST question (seed b) is held back until a later one was answered.
  let release: (() => void) | null = null;
  const asked: string[] = [];
  await page.route(`**/api/v1/runs/${run.id}/plan`, async (route) => {
    const seed = String((route.request().postDataJSON() as { parameters: { seed: string } }).parameters.seed);
    asked.push(seed);
    if (seed === "b") await new Promise<void>((done) => (release = done));
    await route.continue().catch(() => undefined);
  });
  await dialog.getByRole("button", { name: msg("plan.show") }).click();
  await expect.poll(() => asked).toEqual(["b"]);
  await expect(dialog.getByRole("status").filter({ hasText: msg("plan.loading") })).toBeVisible();

  await dialog.getByRole("textbox", { name: "Seed" }).fill("a");
  const plan = dialog.getByTestId("plan");
  await expect(plan).toBeVisible();
  await plan.locator(".plan-text summary").click();
  await expect(plan.locator(".plan-text")).toContainText("with seed 'a'");
  expect(asked[asked.length - 1]).toBe("a");
  // Now the old answer is let through. It belongs to options that are no longer on screen.
  expect(release).not.toBeNull();
  release!();
  await page.unroute(`**/api/v1/runs/${run.id}/plan`);
  await expect(plan.locator(".plan-text")).toContainText("with seed 'a'");
  await expect(plan.locator(".plan-text")).not.toContainText("with seed 'b'");

  // An option that is not acceptable: no plan is shown for it, and none of the old one stays.
  await dialog.getByRole("textbox", { name: "Seed" }).fill("line one");
  await expect(plan.locator(".plan-text")).toContainText("with seed 'line one'");
  await dialog.getByRole("button", { name: msg("plan.hide") }).click();
  await expect(plan).toHaveCount(0);

  // The plan of the failures of this run: a plan for exactly what a rerun would compose.
  await dialog.getByRole("button", { name: msg("action.cancel") }).click();
  const failed = (run.verdict_summary.FAIL ?? 0) + (run.verdict_summary.ERROR ?? 0);
  await page.getByRole("button", { name: msg("run.rerunFailed", { count: failed }) }).click();
  const failures = page.getByRole("dialog", { name: msg("rerun.titleFailed") });
  await failures.getByRole("button", { name: msg("plan.show") }).click();
  await expect(failures.getByTestId("plan")).toContainText(`${msg("plan.summary", { count: failed })} ${msg("plan.inShards", { count: 1 })}`);
});
