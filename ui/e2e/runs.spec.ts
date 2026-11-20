// The runs list: what each row says, filtering and paging in the URL, rows
// that update while a run is in flight — and no asking while nobody looks.

import { expect, msg, test } from "./fixtures.ts";

test("rows carry counts, runner, labels and origin; filters and search live in the URL", async ({ page, open, api }) => {
  const nightly = await api.finishedRun("Regression", "b", { title: "Nightly regression", labels: { suite: "nightly", env: "staging" } });
  await api.finishedRun("Payments-Refunds", "a", { title: "Zahlungen prüfen", labels: { suite: "nightly" } });
  await api.compose({ selection: { tag_filter: "Checkout" }, title: "Checkout draft", labels: { suite: "adhoc" } });

  await open("#/runs");
  const rows = page.locator(".runs-table tbody tr");
  await expect(rows).toHaveCount(3);
  const row = page.locator(`tr[data-run-id="${nightly.id}"]`);
  await expect(row.getByRole("link", { name: "Nightly regression" })).toBeVisible();
  await expect(row).toContainText(msg("runState.COMPLETE"));
  await expect(row).toContainText(msg("completion.FAIL"));
  await expect(row).toContainText(`${nightly.verdict_summary.FAIL} ${msg("verdict.FAIL")}`);
  await expect(row).toContainText(`${nightly.verdict_summary.PASS} ${msg("verdict.PASS")}`);
  await expect(row).toContainText("suite=nightly");
  await expect(row).toContainText("env=staging");
  await expect(row).toContainText("demo");
  await expect(row.locator("time")).toHaveAttribute("datetime", /^\d{4}-\d\d-\d\dT/);
  await expect(row.locator("time")).toHaveText(/ago|now/);
  // A run that was only composed says so instead of showing an empty result.
  await expect(page.locator('tr[data-state="COMPOSED"]')).toContainText(msg("runs.notDispatched"));

  // Title search folds case beyond ASCII and follows typing.
  await page.getByRole("searchbox", { name: msg("runs.search") }).fill("PRÜF");
  await expect(rows).toHaveCount(1);
  await expect(page).toHaveURL(/q=PR/);
  await expect(rows.first()).toContainText("Zahlungen prüfen");
  await page.getByRole("button", { name: msg("runs.clearFilters") }).click();
  await expect(rows).toHaveCount(3);

  await page.getByLabel(msg("runs.state")).selectOption("COMPOSED");
  await expect(rows).toHaveCount(1);
  await expect(page).toHaveURL(/state=COMPOSED/);
  await page.getByLabel(msg("runs.state")).selectOption("");

  const label = page.getByRole("textbox", { name: msg("runs.label") });
  await label.fill("suite=nightly");
  await label.press("Enter");
  await expect(rows).toHaveCount(2);
  await label.fill("env=production");
  await label.press("Enter");
  await expect(page.getByText(msg("runs.noMatch"))).toBeVisible();
  // A filtered list that is empty is not "no runs yet".
  await expect(page.getByText(msg("runs.empty"))).toHaveCount(0);

  // The filters are the URL: a reload shows the same list.
  await label.fill("env=staging");
  await label.press("Enter");
  await expect(rows).toHaveCount(1);
  await page.reload();
  await expect(page.getByRole("textbox", { name: msg("runs.label") })).toHaveValue("env=staging");
  await expect(rows).toHaveCount(1);

  // A row opens the run — it is a link, so the keyboard gets there too.
  await rows.first().getByRole("link", { name: "Nightly regression" }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`#/runs/${nightly.id}`));
});

test("paging keeps the total and the place", async ({ page, open, api }) => {
  for (let index = 1; index <= 27; index += 1) {
    await api.compose({ selection: { tag_filter: "Auth-SSO" }, title: `Draft ${String(index).padStart(2, "0")}` });
  }
  await open("#/runs");
  const rows = page.locator(".runs-table tbody tr");
  await expect(rows).toHaveCount(25);
  await expect(page.getByText(msg("runs.range", { first: 1, last: 25, total: 27 }))).toBeVisible();
  await expect(page.getByRole("button", { name: msg("runs.previous") })).toBeDisabled();
  await page.getByRole("button", { name: msg("runs.next") }).click();
  await expect(rows).toHaveCount(2);
  await expect(page).toHaveURL(/offset=25/);
  await expect(page.getByText(msg("runs.range", { first: 26, last: 27, total: 27 }))).toBeVisible();
  await expect(rows.last()).toContainText("Draft 01");
  await expect(page.getByRole("button", { name: msg("runs.next") })).toBeDisabled();
  await page.reload();
  await expect(rows).toHaveCount(2);
  await page.getByRole("button", { name: msg("runs.previous") }).click();
  await expect(rows).toHaveCount(25);
});

test("a row follows its run while it is in flight", async ({ page, open, api }) => {
  const run = await api.compose({ selection: { tag_filter: "Auth-SSO" }, title: "Live row", dispatch: { runner: "slow", parameters: { seed: "a" } } });
  await open("#/runs");
  const row = page.locator(`tr[data-run-id="${run.id}"]`);
  await expect(row).toBeVisible();
  // Nobody reloads; the row arrives at its result on its own.
  await expect(row).toHaveAttribute("data-state", "COMPLETE");
  await expect(row).toContainText(msg("completion.PASS"));
  await expect(row).toContainText(`3 ${msg("verdict.PASS")}`);
});

test("nothing is asked of the server while the tab is hidden", async ({ page, open, api }) => {
  await api.finishedRun("Auth-SSO", "a", { title: "Anmeldung prüfen" });
  await page.clock.install();
  await open("#/runs");
  await expect(page.locator(".runs-table tbody tr")).toHaveCount(1);

  const listRequests: string[] = [];
  page.on("request", (request) => {
    if (/\/api\/v1\/runs(\?|$)/.test(request.url())) listRequests.push(request.url());
  });
  // Visible: the idle interval passes and the list is asked for again.
  const visiblePoll = page.waitForRequest(/\/api\/v1\/runs(\?|$)/);
  await page.clock.fastForward(21_000);
  await visiblePoll;
  await expect.poll(() => listRequests.length).toBe(1);

  // Hidden: minutes pass and nothing is asked.
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.fastForward(300_000);
  // A round trip to the page, so anything it was going to send has been sent.
  await page.evaluate(() => fetch("/api/v1/health").then((response) => response.status));
  expect(listRequests).toHaveLength(1);

  // Shown again: it looks at once, without waiting for the interval.
  const back = page.waitForRequest(/\/api\/v1\/runs(\?|$)/);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await back;
});

test("when the list cannot be loaded it says so — it does not claim there are no runs", async ({ page, open, api }) => {
  await api.finishedRun("Auth-SSO", "a", { title: "Anmeldung prüfen" });
  await page.route(/\/api\/v1\/runs(\?|$)/, (route) => route.abort("connectionrefused"));
  await open("#/runs");
  const alert = page.getByRole("alert");
  await expect(alert).toContainText(msg("error.code.network"));
  await expect(page.getByText(msg("runs.empty"))).toHaveCount(0);
  await page.unroute(/\/api\/v1\/runs(\?|$)/);
  await alert.getByRole("button", { name: msg("action.retry") }).click();
  await expect(page.locator(".runs-table tbody tr")).toHaveCount(1);
  await expect(alert).toHaveCount(0);
});

test("the first screen of an empty server points to Compose", async ({ page, open }) => {
  await open("#/runs");
  await expect(page.getByText(msg("runs.empty"))).toBeVisible();
  await page.getByRole("link", { name: msg("runs.composeFirst") }).click();
  await expect(page.getByText(msg("compose.landing.title"))).toBeVisible();
});
