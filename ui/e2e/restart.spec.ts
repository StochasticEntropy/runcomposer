// The server stops and comes back — a deployment, a crash, a machine that
// was restarted. The pages that were open must say what is true while it is
// away, and be right again by themselves once it is back.

import { addRule, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

test("Compose recovers by itself when the server is back, and never shows the count of another filter", async ({ page, open, server }) => {
  await open("#/compose?f=Payments");
  await expectPreview(page, 17, 60);

  await server.kill();
  await addRule(page, "Smoke");
  const alert = page.locator(".preview").getByRole("alert");
  await expect(alert).toContainText(msg("error.code.network"));
  await expect(page.getByTestId("run-summary")).toHaveText(msg("compose.blocked.network"));
  // "17 of 60" was the answer to "Payments". Next to "Payments AND Smoke" it is not shown — not in the head, not on the button.
  await expect(page.getByTestId("filter-text")).toHaveText("Payments AND Smoke");
  await expect(page.getByTestId("preview-count")).toHaveCount(0);
  await expect(page.getByRole("button", { name: msg("compose.runUncounted"), exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: msg("compose.run", { count: 0 }), exact: true })).toHaveCount(0);

  // Back again. Nobody touches the page.
  await server.comeBack();
  await expectPreview(page, 4, 60);
  await expect(alert).toHaveCount(0);
  await expect(runButton(page, 4)).toBeEnabled();
});

test("a run the server stopped under says so — it is not an error of the runner, and no process id is the headline", async ({ page, open, api, server }) => {
  await open("#/compose?f=Regression");
  await expectPreview(page, 46, 60);
  await page.getByRole("combobox", { name: msg("runner.label") }).selectOption("slow");
  await runButton(page, 46).click();
  const runId = await currentRunId(page);
  await expect(page.getByTestId("run-status")).toContainText(msg("runState.RUNNING"));
  await expect(page.locator(".verdict-table .live-dot").first()).toBeVisible();

  await server.comeBack();
  await api.waitFor(runId, ["STALE"]);
  // The page follows by itself.
  await expect(page.getByTestId("run-status")).toContainText(msg("runState.STALE"), { timeout: 60_000 });
  const stored = await api.get<{ dispatches: { state: string; error: string; error_code: string; error_owner: string | null }[] }>(`/runs/${runId}`);
  expect(stored.dispatches[0]!.state).toBe("STALE");
  expect(stored.dispatches[0]!.error).not.toBe("");
  // The cause is a field of its own; the process it was is kept out of the text.
  expect(stored.dispatches[0]!.error_code).toBe("interrupted");
  expect(stored.dispatches[0]!.error).not.toContain(stored.dispatches[0]!.error_owner ?? "-");

  // What happened, in the UI's words…
  const notice = page.locator(".notice").filter({ hasText: msg("run.cause.interrupted") });
  await expect(notice).toBeVisible();
  // …and nothing claims the runner reported an error.
  await expect(page.getByText(msg("run.cause.runner-raised"))).toHaveCount(0);
  // What the server noted — which process it was — is one step away, for whoever looks into it.
  const technical = notice.locator("details");
  await expect(technical.locator("summary")).toHaveText(msg("run.technicalDetail"));
  await expect(technical.locator(".foreign").first()).toBeHidden();
  await technical.locator("summary").click();
  await expect(technical.locator(".foreign").first()).toHaveText(stored.dispatches[0]!.error);
  await expect(technical.locator(".foreign").nth(1)).toHaveText(stored.dispatches[0]!.error_owner!);
  // The run can be run again.
  await expect(page.getByRole("button", { name: msg("run.rerunAll") })).toBeVisible();
});
