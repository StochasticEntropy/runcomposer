// Export: the document goes out byte for byte as the server hashed it, the
// ingest token is shown once, and results sent back complete the run.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import type { Page } from "@playwright/test";

import { currentRunId, expect, expectPreview, msg, test } from "./fixtures.ts";

const sha256 = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");

async function exportFromCompose(page: Page, format: "JSON" | "YAML" = "JSON") {
  await page.getByRole("button", { name: msg("compose.export") }).click();
  const dialog = page.getByRole("dialog", { name: msg("export.title") });
  if (format === "YAML") await dialog.getByRole("radio", { name: "YAML" }).check();
  await dialog.getByRole("button", { name: msg("export.confirm") }).click();
  const done = page.getByRole("dialog", { name: msg("export.doneTitle") });
  await expect(done).toBeVisible();
  return done;
}

test("round trip with a title outside ASCII: export → execute elsewhere → results complete the run", async ({ page, open, api, server }, testInfo) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await page.getByRole("textbox", { name: msg("compose.title") }).fill("Zahlungen prüfen — Größe ✓");

  // Opening the dialog creates nothing; it says what it is about to do.
  await page.getByRole("button", { name: msg("compose.export") }).click();
  const ask = page.getByRole("dialog", { name: msg("export.title") });
  await expect(ask).toContainText(msg("export.introCompose", { count: 3 }));
  expect(await api.runs()).toHaveLength(0);
  await ask.getByRole("button", { name: msg("export.confirm") }).click();

  const done = page.getByRole("dialog", { name: msg("export.doneTitle") });
  await expect(done).toContainText(msg("export.tokenOnce"));
  const token = (await done.getByTestId("export-token").textContent())!;
  expect(token).toMatch(/^rct_/);
  const shown = (await done.getByTestId("export-sha").textContent())!;

  // The download is the server's own bytes: its hash is the hash the server computed.
  const [download] = await Promise.all([page.waitForEvent("download"), done.getByTestId("export-download").click()]);
  const specFile = testInfo.outputPath("spec.json");
  await download.saveAs(specFile);
  expect(download.suggestedFilename()).toMatch(/^runspec-[0-9A-Z]+\.json$/);
  expect(sha256(specFile)).toBe(shown);
  expect(readFileSync(specFile, "utf-8")).toContain("Zahlungen prüfen — Größe ✓");
  // What a client that re-serialised the document would have produced is a different document.
  const reserialised = createHash("sha256").update(JSON.stringify(JSON.parse(readFileSync(specFile, "utf-8")))).digest("hex");
  expect(reserialised).not.toBe(shown);

  await done.getByRole("link", { name: msg("export.openRun") }).click();
  const runId = await currentRunId(page);
  await expect(page.getByTestId("run-status")).toContainText(msg("runState.AWAITING_RESULTS"));
  await expect(page.getByText(msg("run.awaitingHint"))).toBeVisible();
  // Exactly one run exists — the export did not compose another on the way.
  const runs = await api.runs();
  expect(runs.map((run) => run.id)).toEqual([runId]);

  // Somewhere else: execute the downloaded document, send the bundle back with the token.
  const detail = await api.get<{ dispatches: { dispatch_id: string; spec_sha256: string }[] }>(`/runs/${runId}`);
  expect(detail.dispatches[0]!.spec_sha256).toBe(shown);
  const bundle = await api.simulate(specFile, detail.dispatches[0]!.dispatch_id);
  expect(bundle.map((file) => file.name)).toContain("runcomposer_run.json");
  const form = new FormData();
  for (const file of bundle) form.append("files", new Blob([new Uint8Array(file.buffer)], { type: file.mimeType }), file.name);
  const pushed = await fetch(`${server.api}/runs/${runId}/results`, { method: "POST", body: form, headers: { Authorization: `Bearer ${token}` } });
  expect(pushed.status, await pushed.clone().text()).toBe(200);

  // The open page notices on its own.
  await expect(page.getByTestId("run-status")).toContainText(msg("runState.COMPLETE"), { timeout: 40_000 });
  await expect(page.getByTestId("run-status")).toContainText(msg("completion.PASS"));

  // Later: the same bytes can be downloaded again, and the token is nowhere on the page.
  await page.getByRole("link", { name: msg("run.tab.activity") }).click();
  const [again] = await Promise.all([page.waitForEvent("download"), page.getByTestId("dispatch-spec").click()]);
  const againFile = testInfo.outputPath("spec-again.json");
  await again.saveAs(againFile);
  expect(sha256(againFile)).toBe(shown);
  await expect(page.locator("body")).not.toContainText(token);
});

test("YAML export, and a new token replaces the one shown once", async ({ page, open, api, server }, testInfo) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  const done = await exportFromCompose(page, "YAML");
  const first = (await done.getByTestId("export-token").textContent())!;
  const [download] = await Promise.all([page.waitForEvent("download"), done.getByTestId("export-download").click()]);
  expect(download.suggestedFilename()).toMatch(/\.yaml$/);
  const specFile = testInfo.outputPath("spec.yaml");
  await download.saveAs(specFile);
  expect(sha256(specFile)).toBe(await done.getByTestId("export-sha").textContent());
  await done.getByRole("link", { name: msg("export.openRun") }).click();
  const runId = await currentRunId(page);

  await page.getByRole("link", { name: msg("run.tab.activity") }).click();
  await page.getByRole("button", { name: msg("run.activity.newToken") }).click();
  const confirm = page.getByRole("dialog", { name: msg("run.rotateTitle") });
  await expect(confirm).toContainText(msg("run.rotateExplain"));
  await confirm.getByRole("button", { name: msg("run.rotateConfirm") }).click();
  const second = (await page.getByTestId("rotated-token").textContent())!;
  expect(second).toMatch(/^rct_/);
  expect(second).not.toBe(first);

  const results = { format: "runcomposer-verdicts", verdicts: [{ name: "Shop.Auth.SSO.T001", status: "PASS", duration_ms: 20 }] };
  const push = (token: string) => {
    const form = new FormData();
    form.append("files", new Blob([JSON.stringify(results)], { type: "application/json" }), "results.json");
    return fetch(`${server.api}/runs/${runId}/results`, { method: "POST", body: form, headers: { Authorization: `Bearer ${token}` } });
  };
  expect((await push(first)).status).toBe(403);
  expect((await push(second)).status).toBe(200);
  // One of three items came back: the run is complete, and honestly incomplete.
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("run-status")).toContainText(msg("completion.INCOMPLETE"), { timeout: 40_000 });
  expect((await api.run(runId)).verdict_summary).toEqual({ PASS: 1, MISSING: 2 });
});

test.describe("a server without runners or tokens", () => {
  test.use({ config: "open" });

  test("a selection can still be exported, finalized as it stands, and exported again from the run", async ({ page, open, api }) => {
    await open("#/compose?f=Auth-SSO");
    await expectPreview(page, 3, 60);
    // No runner: the page says so, Run stays off, Export works.
    await expect(page.locator(".run-panel")).toContainText(msg("runner.none"));
    await expect(page.getByRole("button", { name: msg("compose.run", { count: 3 }) })).toBeDisabled();
    const done = await exportFromCompose(page);
    await expect(done).toContainText(msg("export.noToken"));
    await expect(done.getByTestId("export-token")).toHaveCount(0);
    await done.getByRole("link", { name: msg("export.openRun") }).click();
    const runId = await currentRunId(page);

    // Nobody answers: close it as it stands. Every item counts as missing.
    await page.getByRole("button", { name: msg("run.finalize") }).click();
    const confirm = page.getByRole("dialog", { name: msg("run.finalizeTitle") });
    await expect(confirm).toContainText(msg("run.finalizeExplain"));
    await confirm.getByRole("button", { name: msg("run.finalize") }).click();
    await expect(page.getByTestId("run-status")).toContainText(msg("completion.INCOMPLETE"));
    await expect(page.locator('.verdict-table tbody tr[data-status="MISSING"]')).toHaveCount(3);
    await expect(page.locator(".verdict-table tbody tr").first()).toContainText(msg("run.reason.not-reported"));

    // The run's own Export hands the same run out again — it does not compose a new one.
    await page.getByRole("button", { name: msg("run.export") }).click();
    const again = page.getByRole("dialog", { name: msg("export.title") });
    await again.getByRole("button", { name: msg("export.confirm") }).click();
    await expect(page.getByRole("dialog", { name: msg("export.doneTitle") })).toBeVisible();
    await page.getByRole("dialog", { name: msg("export.doneTitle") }).getByRole("button", { name: msg("action.done") }).click();
    await expect(page.getByTestId("run-status")).toContainText(msg("runState.AWAITING_RESULTS"));
    const runs = await api.runs();
    expect(runs.map((run) => run.id)).toEqual([runId]);
    expect((await api.get<{ dispatch_count: number }>(`/runs/${runId}`)).dispatch_count).toBe(2);
  });
});
