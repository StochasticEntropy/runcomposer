// Running: choosing a runner and filling in what it asks for, watching a run,
// cancelling it, reading its results, running it again.

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

const runnerSelect = (page: import("@playwright/test").Page) => page.getByRole("combobox", { name: msg("runner.label") });
const status = (page: import("@playwright/test").Page) => page.getByTestId("run-status");

test("the form is rendered from the runner's declared parameters, and its values reach the run", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("scripted");

  const form = page.getByRole("group", { name: msg("runner.parameters") });
  // One control per kind, with labels, help and defaults from the declaration.
  await expect(form.getByRole("combobox", { name: /^Target environment/ })).toHaveValue("");
  await expect(form.getByText("Where the shop under test runs.")).toBeVisible();
  await expect(form.getByRole("textbox", { name: /^Build number/ })).toHaveValue("");
  await expect(form.getByRole("textbox", { name: "Retry count" })).toHaveValue("0");
  await expect(form.getByRole("checkbox", { name: "Run headless" })).toBeChecked();
  const browsers = form.getByRole("group", { name: "Browsers to use" });
  await expect(browsers.getByRole("checkbox", { name: "chromium" })).toBeChecked();
  await expect(browsers.getByRole("checkbox", { name: "firefox" })).not.toBeChecked();

  // Required and ill-typed values are refused on their fields before anything is sent.
  await form.getByRole("textbox", { name: "Retry count" }).fill("two");
  await runButton(page, 3).click();
  // The keyboard is taken to the first field that needs attention.
  await expect(form.getByRole("combobox", { name: /^Target environment/ })).toBeFocused();
  await expect(form.locator('[data-param="environment"]').getByRole("alert")).toHaveText(msg("runner.problem.required"));
  await expect(form.locator('[data-param="build"]').getByRole("alert")).toHaveText(msg("runner.problem.required"));
  await expect(form.locator('[data-param="retries"]').getByRole("alert")).toHaveText(msg("runner.problem.integer"));
  await expect(form.getByRole("textbox", { name: "Retry count" })).toHaveAttribute("aria-invalid", "true");
  expect(await api.runs()).toHaveLength(0);

  await form.getByRole("combobox", { name: /^Target environment/ }).selectOption("staging");
  await form.getByRole("textbox", { name: "Retry count" }).fill("2");

  // A value must have the form the runner declares — said with the form, before anything is sent.
  const build = form.getByRole("textbox", { name: /^Build number/ });
  await build.fill("build 11");
  await runButton(page, 3).click();
  // A sentence says what is wrong; the form itself stands under the field, once, as it is written.
  await expect(form.locator('[data-param="build"]').getByRole("alert")).toHaveText(msg("runner.problem.pattern"));
  await expect(form.locator('[data-param="build"] .pattern-hint')).toHaveText(`${msg("runner.expectedForm")} \\d{4}\\.\\d{1,2}`);
  expect(await form.locator('[data-param="build"]').innerText()).not.toContain("\\\\");
  await expect(build).toBeFocused();
  expect(await api.runs()).toHaveLength(0);
  await build.fill("2026.11");

  // A text is several lines; a mapping is rows of name and value, starting from its default.
  const notes = form.getByRole("textbox", { name: "Release notes" });
  expect(await notes.evaluate((field) => field instanceof HTMLTextAreaElement)).toBe(true);
  await notes.fill("Checkout redesign\nNew wallet provider");
  const variables = form.getByRole("group", { name: "Extra variables" });
  await expect(variables.getByRole("textbox", { name: msg("runner.mappingKeyOf", { label: "Extra variables", number: 1 }) })).toHaveValue("locale");
  await variables.getByRole("textbox", { name: msg("runner.mappingValueOf", { label: "Extra variables", number: 1 }) }).fill("en_US");
  await variables.getByRole("button", { name: msg("runner.mappingAdd") }).click();
  await variables.getByRole("textbox", { name: msg("runner.mappingKeyOf", { label: "Extra variables", number: 2 }) }).fill("currency");
  await variables.getByRole("textbox", { name: msg("runner.mappingValueOf", { label: "Extra variables", number: 2 }) }).fill("eur");
  // Each value of the mapping must match its pattern too.
  await runButton(page, 3).click();
  await expect(form.locator('[data-param="variables"]').getByRole("alert")).toHaveText(msg("runner.problem.pattern", { pattern: "[a-z0-9-]+" }));
  await variables.getByRole("textbox", { name: msg("runner.mappingValueOf", { label: "Extra variables", number: 1 }) }).fill("en");
  // A third row is added and removed again.
  await variables.getByRole("button", { name: msg("runner.mappingAdd") }).click();
  await variables.getByRole("button", { name: msg("runner.mappingRemoveOf", { label: "Extra variables", number: 3 }) }).click();
  await expect(variables.locator(".mapping-row")).toHaveCount(2);
  await form.getByRole("checkbox", { name: "Run headless" }).uncheck();
  await browsers.getByRole("checkbox", { name: "webkit" }).check();
  await page.getByRole("textbox", { name: msg("compose.title") }).fill("Anmeldung prüfen");
  await page.getByRole("textbox", { name: msg("labels.label") }).fill("suite=adhoc");
  await runButton(page, 3).click();

  // Run navigates to the run's own URL.
  const runId = await currentRunId(page);
  await expect(page.getByRole("heading", { level: 1, name: "Anmeldung prüfen" })).toBeVisible();
  const done = await api.waitFor(runId, ["COMPLETE"]);
  expect(done.completion).toBe("PASS");
  const detail = await api.get<{ labels: Record<string, string>; dispatches: { mode: string; parameters: Record<string, unknown> }[] }>(`/runs/${runId}`);
  expect(detail.labels).toEqual({ suite: "adhoc" });
  expect(detail.dispatches[0]!.mode).toBe("scripted");
  expect(detail.dispatches[0]!.parameters).toEqual({
    environment: "staging",
    build: "2026.11",
    retries: 2,
    headless: false,
    browsers: ["chromium", "webkit"],
    notes: "Checkout redesign\nNew wallet provider",
    variables: { locale: "en", currency: "eur" },
  });
  await expect(status(page)).toContainText(msg("completion.PASS"));
  // The parameters are readable on the run.
  await page.getByRole("link", { name: msg("run.tab.selection") }).click();
  await expect(page.getByText("chromium, webkit")).toBeVisible();
});

test("a value the server refuses lands on its field, in the viewer's words plus the server's", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("demo");
  // What the form sends is always well-typed, so the request is altered on its
  // way out: the refusal that comes back is the server's own.
  await page.route("**/api/v1/runs", async (route) => {
    const body = route.request().postDataJSON() as { dispatch: { parameters: Record<string, unknown> } };
    body.dispatch.parameters.seed = 7;
    await route.continue({ postData: JSON.stringify(body) });
  });
  await runButton(page, 3).click();
  const field = page.locator('[data-param="seed"]');
  await expect(field.getByRole("alert")).toContainText(msg("error.code.invalid-parameter"));
  await expect(field.getByRole("alert")).toContainText("parameter 'seed' must be a string");
  await expect(field.getByRole("textbox")).toHaveAttribute("aria-invalid", "true");
  await expect(page).toHaveURL(/#\/compose/);
  expect(await api.runs()).toHaveLength(0);

  // Changing the value clears the message; the next attempt goes through.
  await page.unroute("**/api/v1/runs");
  await field.getByRole("textbox").fill("a");
  await expect(field.getByRole("alert")).toHaveCount(0);
  await runButton(page, 3).click();
  await api.waitFor(await currentRunId(page), ["COMPLETE"]);
});

test("the names of a mapping have a form too: shown, checked before anything is sent, worded by the UI", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("scripted");
  const form = page.getByRole("group", { name: msg("runner.parameters") });
  await form.getByRole("combobox", { name: /^Target environment/ }).selectOption("staging");
  await form.getByRole("textbox", { name: /^Build number/ }).fill("2026.11");

  // The form says which form a name must have, next to the form of a value — each once, as it is written.
  const switches = page.getByRole("group", { name: "Feature switches" });
  const field = page.locator('[data-param="switches"]');
  await expect(field.locator('.pattern-hint[data-pattern-of="name"]')).toHaveText(`${msg("runner.expectedNameForm")} [A-Z][A-Za-z-]+`);
  await expect(field.locator('.pattern-hint[data-pattern-of="value"]')).toHaveText(`${msg("runner.expectedValueForm")} on|off`);

  const name = (row: number) => switches.getByRole("textbox", { name: msg("runner.mappingKeyOf", { label: "Feature switches", number: row }) });
  const value = (row: number) => switches.getByRole("textbox", { name: msg("runner.mappingValueOf", { label: "Feature switches", number: row }) });
  await switches.getByRole("button", { name: msg("runner.mappingAdd") }).click();
  await name(1).fill("Checkout-Express");
  await value(1).fill("on");
  await switches.getByRole("button", { name: msg("runner.mappingAdd") }).click();
  await name(2).fill("payments wallets");
  await value(2).fill("off");

  // A name of the wrong form is refused here, in the UI's words — no request is made.
  let sent = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/v1/runs")) sent += 1;
  });
  await runButton(page, 3).click();
  await expect(field.getByRole("alert")).toHaveText(msg("runner.problem.key"));
  await expect(name(2)).toHaveAttribute("aria-invalid", "true");
  await expect(name(1)).not.toHaveAttribute("aria-invalid", "true");
  await expect(value(2)).not.toHaveAttribute("aria-invalid", "true");
  expect(sent).toBe(0);
  expect(await api.runs()).toHaveLength(0);
  // The same in German: the sentence is the UI's own.
  await page.getByRole("combobox", { name: msg("prefs.language") }).selectOption("de");
  await page.getByRole("button", { name: msg("compose.run", { count: 3 }, "de"), exact: true }).click();
  await expect(field.getByRole("alert")).toHaveText(msg("runner.problem.key", {}, "de"));
  await expect(field.locator('.pattern-hint[data-pattern-of="name"]')).toHaveText(`${msg("runner.expectedNameForm", {}, "de")} [A-Z][A-Za-z-]+`);
  await expect(field.locator(".foreign")).toHaveCount(0);
  await page.getByRole("combobox", { name: msg("prefs.language", {}, "de") }).selectOption("en");

  // Corrected, the mapping reaches the run as typed.
  await name(2).fill("Payments-Wallets");
  await expect(field.getByRole("alert")).toHaveCount(0);
  await runButton(page, 3).click();
  const runId = await currentRunId(page);
  await api.waitFor(runId, ["COMPLETE"]);
  const detail = await api.get<{ dispatches: { parameters: Record<string, unknown> }[] }>(`/runs/${runId}`);
  expect(detail.dispatches[0]!.parameters.switches).toEqual({ "Checkout-Express": "on", "Payments-Wallets": "off" });
});

test("when the server refuses a value of a declared form, the field says so in the UI's words — not in the server's", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("scripted");
  const form = page.getByRole("group", { name: msg("runner.parameters") });
  await form.getByRole("combobox", { name: /^Target environment/ }).selectOption("staging");
  await form.getByRole("textbox", { name: /^Build number/ }).fill("2026.11");
  // What the form sends has the declared form, so the request is altered on its way out:
  // the refusal that comes back is the server's own, for a value that does not have it.
  let alter = "build";
  await page.route("**/api/v1/runs", async (route) => {
    const body = route.request().postDataJSON() as { dispatch: { parameters: Record<string, unknown> } };
    if (alter === "build") body.dispatch.parameters.build = "build 11";
    else body.dispatch.parameters.switches = { "payments wallets": "off" };
    await route.continue({ postData: JSON.stringify(body) });
  });
  await runButton(page, 3).click();
  const build = form.locator('[data-param="build"]');
  await expect(build.getByRole("alert")).toHaveText(msg("runner.problem.refusedForm"));
  // The form stands under the field as it is written; the server's sentence — with its doubled backslashes — is not shown.
  await expect(build.locator(".pattern-hint")).toHaveText(`${msg("runner.expectedForm")} \\d{4}\\.\\d{1,2}`);
  await expect(build.locator(".foreign")).toHaveCount(0);
  expect(await build.innerText()).not.toContain("\\\\");
  expect(await build.innerText()).not.toContain("does not match");
  expect(await api.runs()).toHaveLength(0);

  // The same for the names of a mapping.
  alter = "switches";
  await runButton(page, 3).click();
  const switches = form.locator('[data-param="switches"]');
  await expect(switches.getByRole("alert")).toHaveText(msg("runner.problem.refusedForm"));
  await expect(switches.locator(".foreign")).toHaveCount(0);
  expect(await switches.innerText()).not.toContain("key pattern");
  expect(await api.runs()).toHaveLength(0);
});

test("a refused dispatch is explained where the run was started and leaves no run behind", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("strict");
  await runButton(page, 3).click();
  const alert = page.locator(".run-bar").getByRole("alert");
  await expect(alert).toContainText(msg("error.code.dispatch-refused"));
  await expect(runButton(page, 3)).toBeFocused();
  await expect(alert).toContainText("do not belong to the configured suite roots");
  await expect(page).toHaveURL(/#\/compose/);
  expect(await api.runs()).toHaveLength(0);
  // The selection is untouched and can go to another runner at once.
  await runnerSelect(page).selectOption("demo");
  await expect(alert).toHaveCount(0);
  await runButton(page, 3).click();
  await api.waitFor(await currentRunId(page), ["COMPLETE"]);
});

test("an unhealthy runner is listed with its state and the reason", async ({ page, open }) => {
  await open();
  const select = runnerSelect(page);
  await expect(select.getByRole("option", { name: new RegExp(`offline.*${msg("health.degraded")}`) })).toBeAttached();
  await select.selectOption("offline");
  const health = page.locator(".runner-health");
  await expect(health).toContainText(msg("health.degraded"));
  await expect(health).toContainText("suite root(s) not found");
  await expect(page.locator(".runner-status")).toContainText(msg("runner.unhealthyHint"));
  // Asking again probes the runner now.
  const probe = page.waitForRequest((request) => request.url().endsWith("/api/v1/runners?refresh=true"));
  await page.getByRole("button", { name: msg("runner.recheck") }).click();
  await probe;
  await expect(health).toContainText("suite root(s) not found");
});

test("watch a run progress live, then cancel it", async ({ page, open, api }) => {
  await open("#/compose?f=Regression");
  await expectPreview(page, 46, 60);
  await runnerSelect(page).selectOption("slow");
  await runButton(page, 46).click();
  const runId = await currentRunId(page);

  // The run reports where it is without anybody reloading.
  await expect(status(page)).toContainText(msg("runState.RUNNING"));
  const progress = page.getByTestId("run-progress");
  await expect(progress).toHaveText(/^[1-9]\d* of 46 /);
  const earlier = Number.parseInt((await progress.textContent())!, 10);
  await expect.poll(async () => Number.parseInt((await progress.textContent())!, 10)).toBeGreaterThan(earlier);
  // The item being executed right now is RUNNING: listed, counted as running — and not as a result.
  await expect(page.locator('.verdict-table tbody tr[data-status="RUNNING"]')).toHaveCount(1);
  await expect(page.getByTestId("run-running")).toHaveText(msg("run.nowRunning", { count: 1 }));
  await expect(page.locator(".shard-card")).toContainText(msg("run.nowRunning", { count: 1 }));
  const reported = Number.parseInt((await progress.textContent())!, 10);
  const liveRows = await api.get<{ verdicts: { status: string; live: boolean }[] }>(`/runs/${runId}`);
  expect(liveRows.verdicts.some((row) => row.status === "RUNNING" && row.live)).toBe(true);
  expect(reported).toBeLessThan(46);
  await page.getByRole("button", { name: new RegExp(`^${msg("run.filter.running")}\\s+1$`) }).click();
  await expect(page.locator(".verdict-table tbody tr[data-item-id]")).toHaveCount(1);
  await expect(page.locator(".verdict-table tbody tr[data-item-id]").first().locator(".col-duration")).toHaveText("–");
  await page.getByRole("button", { name: new RegExp(`^${msg("run.filter.all")}`) }).click();
  // A run without groups shows shards as before — no group roll-up.
  await expect(page.locator(".group-card")).toHaveCount(0);
  // Items that finished are listed while their shard is still running — marked live.
  await expect(page.locator(".verdict-table tbody tr[data-item-id]").first()).toBeVisible();
  await expect(page.locator(".verdict-table .live-dot").first()).toBeVisible();
  await expect(page.locator(".shard-card")).toContainText(msg("run.shardRunning"));

  await page.getByRole("button", { name: msg("run.cancel") }).click();
  const confirm = page.getByRole("dialog", { name: msg("run.cancelTitle") });
  // The dialog says what will be true afterwards — every sentence of it is checked below, on this very run:
  // delivered results are kept; what was only reported live is no result, and shows as missing with its last live status.
  await expect(confirm.locator("p").first()).toHaveText(msg("run.cancelExplain"));
  await expect(confirm).not.toContainText("keeps its result");
  const before = await api.get<{ verdicts: { status: string; live: boolean }[] }>(`/runs/${runId}`);
  expect(before.verdicts.filter((row) => row.live && row.status !== "RUNNING").length).toBeGreaterThan(0);
  expect(before.verdicts.filter((row) => !row.live)).toHaveLength(0);
  await confirm.getByRole("button", { name: msg("run.cancelConfirm") }).click();
  await expect(confirm).toBeHidden();
  await expect(status(page)).toContainText(msg("runState.CANCELLED"));
  expect((await api.run(runId)).state).toBe("CANCELLED");
  // The keyboard is not left on nothing: the button that opened the dialog is gone, the view has the focus.
  await expect(page.locator("main")).toBeFocused();
  // Cancelling is not an error of the runner, and nothing is counted twice:
  // every item is one row — missing, with what it had reported live as a note.
  await expect(page.getByText(msg("run.cancelledHint"))).toBeVisible();
  await expect(page.getByText(msg("run.runnerError"))).toHaveCount(0);
  await expect(page.getByRole("button", { name: new RegExp(`^${msg("run.filter.all")}\\s+46$`) })).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(`^${msg("run.filter.missing")}\\s+46$`) })).toBeVisible();
  await expect(page.locator(".verdict-table .live-dot")).toHaveCount(0);
  await expect(progress).toHaveText(msg("progress.label", { done: 0, total: 46 }));
  // Each missing item says what it was doing when the run stopped — the server's word, not a guess:
  // those that had finished, and the one that was in the middle of it.
  await expect(page.locator(".verdict-table .col-message").filter({ hasText: msg("run.seenLive", { status: msg("verdict.PASS") }) }).first()).toBeVisible();
  await page.getByRole("searchbox", { name: msg("run.searchVerdicts") }).fill(msg("run.lastLiveRunning"));
  await expect(page.locator(".verdict-table tbody tr[data-item-id]")).toHaveCount(1);
  await expect(page.locator(".verdict-table tbody tr[data-item-id]").first()).toHaveAttribute("data-status", "MISSING");
  await page.getByRole("searchbox", { name: msg("run.searchVerdicts") }).fill("");
  await expect(page.locator(".shard-card")).toContainText(msg("run.shardNothing"));
  await expect(page.getByRole("button", { name: msg("run.rerunFailed", { count: 46 }) })).toBeVisible();
  // A cancelled run is over: it can be run again, and no longer cancelled.
  await expect(page.getByRole("button", { name: msg("run.cancel") })).toHaveCount(0);
  await expect(page.getByRole("button", { name: msg("run.rerunAll") })).toBeVisible();
  // What happened is on record.
  await page.getByRole("link", { name: msg("run.tab.activity") }).click();
  await expect(page.locator(".events")).toContainText(msg("event.run.cancel_requested"));
  await expect(page.locator(".events")).toContainText(msg("event.run.cancelled"));
});

test("a run that finishes on its own is seen finishing", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runnerSelect(page).selectOption("slow");
  await page.getByRole("group", { name: msg("runner.parameters") }).getByRole("textbox", { name: "Seed" }).fill("a");
  await runButton(page, 3).click();
  const runId = await currentRunId(page);
  await expect(status(page)).toContainText(msg("runState.COMPLETE"));
  await expect(status(page)).toContainText(msg("completion.PASS"));
  await expect(page.getByTestId("run-progress")).toHaveText(msg("progress.label", { done: 3, total: 3 }));
  expect((await api.run(runId)).completion).toBe("PASS");
  await expect(page.locator(".verdict-table .live-dot")).toHaveCount(0);
});

test("results are failures first, filterable, and a message opens in full", async ({ page, open, api }) => {
  const run = await api.finishedRun("Regression", "b", { title: "Nightly regression" });
  const failed = (run.verdict_summary.FAIL ?? 0) + (run.verdict_summary.ERROR ?? 0);
  expect(failed).toBeGreaterThan(0);
  await open(`#/runs/${run.id}`);
  await expect(status(page)).toContainText(msg("completion.FAIL"));

  const rows = page.locator(".verdict-table tbody tr[data-item-id]");
  // An item is shown as in the preview: by its name, with the id it is stored under beneath.
  const catalog = await api.post<{ items: { id: string; name: string }[] }>("/selection/compile", { tag_filter: "Regression" });
  const firstId = (await rows.first().getAttribute("data-item-id"))!;
  const known = catalog.items.find((entry) => entry.id === firstId)!;
  expect(known.name).not.toBe(known.id);
  await expect(rows.first().locator(".item-name")).toHaveText(known.name);
  await expect(rows.first().locator(".item-id")).toHaveText(known.id);
  // The columns are as wide as what they hold: a badge is narrow, the item has the room — and stays on one line.
  const columns = Object.fromEntries(
    await page.locator(".verdict-table thead th").evaluateAll((cells) => cells.map((cell) => [cell.className, cell.getBoundingClientRect().width] as const)),
  ) as Record<string, number>;
  expect(columns["col-status"]).toBeLessThan(150);
  expect(columns["col-item"]).toBeGreaterThan(2.5 * columns["col-status"]!);
  const item = rows.first().locator(".col-item .item-name");
  expect(await item.evaluate((element) => element.getClientRects().length)).toBe(1);
  // Every failure comes before the first row that is not one.
  const statuses = await rows.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-status")));
  expect(statuses.slice(0, failed).every((value) => value === "FAIL" || value === "ERROR")).toBe(true);
  expect(statuses[failed]).not.toBe("FAIL");

  const failedFilter = page.getByRole("button", { name: new RegExp(`^${msg("run.filter.failed")}\\s+${failed}$`) });
  await failedFilter.click();
  await expect(failedFilter).toHaveAttribute("aria-pressed", "true");
  await expect(rows).toHaveCount(failed);
  // Durations are shown in units a person reads.
  await expect(rows.first().locator(".col-duration")).toHaveText(/\d (ms|s)$/);

  // Which results are shown is in the URL: a reload shows the same rows.
  await expect(page).toHaveURL(/status=failed/);
  await page.reload();
  await expect(rows).toHaveCount(failed);
  await expect(failedFilter).toHaveAttribute("aria-pressed", "true");

  await rows.first().locator(".message-btn").click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.locator(".message-full")).toContainText("simulated failure");
  // A test's message is data — shown as written, and not marked as anybody's wording.
  await expect(dialog.locator(".foreign")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // The search finds an item by its name as well as by its id.
  const search = page.getByRole("searchbox", { name: msg("run.searchVerdicts") });
  await page.getByRole("button", { name: new RegExp(`^${msg("run.filter.all")}`) }).click();
  await search.fill(known.name);
  await expect(page.locator(`.verdict-table tbody tr[data-item-id="${known.id}"]`)).toBeVisible();
  expect(await rows.count()).toBeLessThan(catalog.items.length);
  await search.fill(known.id);
  await expect(rows).toHaveCount(1);

  await search.fill("no item is called this");
  await expect(page.getByText(msg("run.noRowsMatch"))).toBeVisible();
});

test("re-run only the failures of THIS run, with other parameters", async ({ page, open, api }) => {
  // Two finished runs: the rerun must follow the one that is open, not "the latest".
  const nightly = await api.finishedRun("Regression", "b", { title: "Nightly regression", labels: { suite: "nightly" } });
  await api.finishedRun("Auth-SSO", "b", { title: "A one-off check" });
  const failed = (nightly.verdict_summary.FAIL ?? 0) + (nightly.verdict_summary.ERROR ?? 0) + (nightly.verdict_summary.MISSING ?? 0);

  await open(`#/runs/${nightly.id}`);
  await page.getByRole("button", { name: msg("run.rerunFailed", { count: failed }) }).click();
  const dialog = page.getByRole("dialog", { name: msg("rerun.titleFailed") });
  await expect(dialog).toContainText(msg("rerun.explainFailed", { count: failed, title: "Nightly regression" }));
  // The runner and its parameters start out as they were for this run.
  await expect(dialog.getByRole("combobox", { name: msg("runner.label") })).toHaveValue("demo");
  const seed = dialog.getByRole("textbox", { name: "Seed" });
  await expect(seed).toHaveValue("b");
  await seed.fill("a");
  // The new run may have a title of its own; left empty it keeps this run's.
  const newTitle = dialog.getByRole("textbox", { name: msg("rerun.title") });
  await expect(newTitle).toHaveAttribute("placeholder", "Nightly regression");
  await newTitle.fill("Nightly regression, second attempt");
  await dialog.getByRole("button", { name: msg("compose.run", { count: failed }) }).click();

  // A NEW run, opened at its own URL, composed from exactly those failures.
  await expect(page).not.toHaveURL(new RegExp(nightly.id));
  const rerunId = await currentRunId(page);
  const rerun = await api.waitFor(rerunId, ["COMPLETE"]);
  expect(rerun.planned_count).toBe(failed);
  await expect(status(page)).toContainText(msg("runState.COMPLETE"));
  await expect(page.getByRole("heading", { level: 1, name: "Nightly regression, second attempt" })).toBeVisible();
  // The header says what this run is a re-run of — the relation the server records — with a link back.
  expect((await api.get<{ rerun_of: { run_id: string; scope: string } }>(`/runs/${rerunId}`)).rerun_of).toEqual({ run_id: nightly.id, scope: "failed" });
  await expect(page.locator(".run-facts")).toContainText(msg("origin.rerun"));
  const back = page.getByTestId("rerun-of");
  await expect(back).toContainText(msg("run.rerunOf"));
  await expect(back.getByRole("link", { name: "Nightly regression" })).toHaveAttribute("href", `#/runs/${nightly.id}`);
  // Where it came from is said, and linked.
  await page.getByRole("link", { name: msg("run.tab.selection") }).click();
  await page.locator(".selection-tab").getByRole("link", { name: "Nightly regression" }).click();
  await expect(page).toHaveURL(new RegExp(`#/runs/${nightly.id}`));
});

test("run a whole run again: a second dispatch of the same run", async ({ page, open, api }) => {
  const first = await api.finishedRun("Auth-SSO", "b", { title: "Anmeldung prüfen" });
  await open(`#/runs/${first.id}`);
  await expect(status(page)).toContainText(msg("completion.FAIL"));
  await page.getByRole("button", { name: msg("run.rerunAll") }).click();
  const dialog = page.getByRole("dialog", { name: msg("rerun.titleAll") });
  await dialog.getByRole("textbox", { name: "Seed" }).fill("a");
  await dialog.getByRole("button", { name: msg("compose.run", { count: 3 }) }).click();
  await expect(dialog).toBeHidden();
  // Same URL, same run — now passed, and the earlier dispatch still readable.
  await expect(page).toHaveURL(new RegExp(`#/runs/${first.id}`));
  await expect(status(page)).toContainText(msg("completion.PASS"));
  const picker = page.getByRole("combobox", { name: msg("run.showDispatch") });
  await expect(picker.getByRole("option")).toHaveCount(2);
  await picker.selectOption({ index: 0 });
  await expect(page).toHaveURL(/[?&]d=/);
  await expect(page.locator('.verdict-table tbody tr[data-status="FAIL"]').first()).toBeVisible();
  expect((await api.get<{ dispatch_count: number }>(`/runs/${first.id}`)).dispatch_count).toBe(2);
});

test("a run that was only composed is dispatched from its own page", async ({ page, open, api }) => {
  const composed = await api.compose({ selection: { tag_filter: "Auth-SSO" }, title: "Anmeldung, noch nicht gestartet" });
  await open(`#/runs/${composed.id}`);
  await expect(status(page)).toContainText(msg("runState.COMPOSED"));
  await expect(page.getByText(msg("run.composedHint"))).toBeVisible();
  await expect(page.getByText(msg("run.neverDispatched"))).toBeVisible();
  await page.getByRole("button", { name: msg("run.dispatch") }).click();
  const dialog = page.getByRole("dialog", { name: msg("rerun.titleFirst") });
  await dialog.getByRole("combobox", { name: msg("runner.label") }).selectOption("demo");
  await dialog.getByRole("textbox", { name: "Seed" }).fill("a");
  await dialog.getByRole("button", { name: msg("compose.run", { count: 3 }) }).click();
  await expect(status(page)).toContainText(msg("completion.PASS"));
  expect((await api.run(composed.id)).state).toBe("COMPLETE");
});

test("load a run's selection back into the builder — with its runner, options and labels", async ({ page, open, api }) => {
  const ast = { op: "AND", items: ["Payments", { not: "prefix:Quarantine-" }] };
  const composed = await api.compose({
    selection: { tag_filter: ast },
    title: "Zahlungen ohne Quarantäne",
    labels: { suite: "nightly", env: "staging" },
    dispatch: { runner: "scripted", parameters: { environment: "preview", build: "2026.11", retries: 3, headless: false, browsers: ["firefox"] } },
  });
  const plain = await api.waitFor(composed.id, ["COMPLETE"]);
  // Something else is in the builder from earlier work.
  await open("#/compose?f=Smoke");
  await page.getByRole("textbox", { name: msg("compose.title") }).fill("Etwas ganz anderes");
  await open(`#/runs/${plain.id}`);
  await page.getByRole("link", { name: msg("run.tab.selection") }).click();
  await expect(page.getByTestId("run-filter")).toHaveText("Payments AND NOT prefix:Quarantine-");
  await page.getByRole("link", { name: msg("run.load") }).first().click();
  await expect(page).toHaveURL(/#\/compose\?f=/);
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND NOT prefix:Quarantine-");
  await expectPreview(page, plain.planned_count, 60);
  await expect(page.getByRole("button", { name: msg("filter.negateRule", { pattern: "prefix:Quarantine-" }) })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("pinned")).toHaveCount(0);
  // How it was run comes along; the title of the other draft does not.
  await expect(page.getByText(msg("compose.loadedFrom", { title: "Zahlungen ohne Quarantäne" }))).toBeVisible();
  await expect(page.getByRole("combobox", { name: msg("runner.label") })).toHaveValue("scripted");
  const form = page.getByRole("group", { name: msg("runner.parameters") });
  await expect(form.getByRole("combobox", { name: /^Target environment/ })).toHaveValue("preview");
  await expect(form.getByRole("textbox", { name: "Retry count" })).toHaveValue("3");
  await expect(form.getByRole("checkbox", { name: "Run headless" })).not.toBeChecked();
  await expect(form.getByRole("checkbox", { name: "firefox" })).toBeChecked();
  await expect(form.getByRole("checkbox", { name: "chromium" })).not.toBeChecked();
  await expect(page.getByRole("button", { name: msg("labels.remove", { label: "env=staging" }) })).toBeVisible();
  await expect(page.getByRole("button", { name: msg("labels.remove", { label: "suite=nightly" }) })).toBeVisible();
  await expect(page.getByRole("textbox", { name: msg("compose.title") })).toHaveValue("");
});

test("a run that named its items is loaded back as that list — and the list can be dropped", async ({ page, open, api }) => {
  // The failures of a run, rerun: a selection with no filter at all.
  const nightly = await api.finishedRun("Regression", "b", { title: "Nightly regression" });
  const failed = (nightly.verdict_summary.FAIL ?? 0) + (nightly.verdict_summary.ERROR ?? 0);
  const rerun = await api.post<{ run: { id: string } }>(`/runs/${nightly.id}/rerun`, { scope: "failed", parameters: { seed: "a" } });
  await api.waitFor(rerun.run.id, ["COMPLETE"]);

  await open(`#/runs/${rerun.run.id}`);
  await page.getByRole("link", { name: msg("run.load") }).first().click();
  await expect(page).toHaveURL(new RegExp(`from=${rerun.run.id}`));
  const pinned = page.getByTestId("pinned");
  await expect(pinned).toContainText(msg("compose.pinned", { count: failed }));
  await expectPreview(page, failed, 60);
  // The list combines with a filter typed on top of it…
  await addRule(page, "Payments");
  const narrowed = await api.post<{ count: number }>("/selection/compile", {
    tag_filter: "Payments",
    item_ids: (await api.get<{ item_ids: string[] }>(`/runs/${rerun.run.id}/items`)).item_ids,
    limit: 0,
  });
  await expectPreview(page, narrowed.count, 60);
  // …and removing it leaves the filter alone.
  await pinned.getByRole("button", { name: msg("compose.unpin") }).click();
  await expect(pinned).toHaveCount(0);
  await expectPreview(page, 17, 60);
});

test("a run has its own URL: deep link, reload, and an honest answer for an id that does not exist", async ({ page, open, api }) => {
  const run = await api.finishedRun("Auth-SSO", "a", { title: "Anmeldung prüfen" });
  await open(`#/runs/${run.id}?tab=activity`);
  await expect(page.getByRole("heading", { level: 1, name: "Anmeldung prüfen" })).toBeVisible();
  await expect(page.getByRole("link", { name: msg("run.tab.activity") })).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".events")).toContainText(msg("event.run.completed"));
  await page.reload();
  await expect(page.locator(".events")).toContainText(msg("event.run.completed"));
  await expect(page).toHaveTitle(/Anmeldung prüfen/);

  await open("#/runs/01NOSUCHRUN000000000000000");
  await expect(page.getByText(msg("run.notFound"))).toBeVisible();
  await page.getByRole("link", { name: msg("run.backToRuns") }).click();
  await expect(page.getByRole("heading", { name: msg("runs.title"), level: 1 })).toBeVisible();

  await open("#/no/such/view");
  await expect(page.getByText(msg("error.notFoundTitle"))).toBeVisible();
});

test("delete a run, after being asked", async ({ page, open, api }) => {
  const run = await api.finishedRun("Auth-SSO", "a", { title: "Lauf zum Wegwerfen" });
  await open(`#/runs/${run.id}`);
  await page.getByRole("button", { name: msg("run.delete") }).click();
  const confirm = page.getByRole("dialog", { name: msg("run.deleteTitle") });
  await expect(confirm).toContainText(msg("run.deleteExplain", { title: "Lauf zum Wegwerfen" }));
  // Cancel keeps it.
  await confirm.getByRole("button", { name: msg("action.cancel") }).click();
  expect(await api.runs()).toHaveLength(1);
  await page.getByRole("button", { name: msg("run.delete") }).click();
  await page.getByRole("dialog", { name: msg("run.deleteTitle") }).getByRole("button", { name: msg("run.deleteConfirm") }).click();
  await expect(page).toHaveURL(/#\/runs$/);
  await expect(page.getByText(msg("run.deleted", { title: "Lauf zum Wegwerfen" }))).toBeVisible();
  await expect(page.getByText(msg("runs.empty"))).toBeVisible();
  expect(await api.runs()).toHaveLength(0);
});
