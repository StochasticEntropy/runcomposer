// A runner that raises after the hand-off: the run ends failed, and what the
// exception said is on the run page — and the runs list says it failed.

import { EXPLODING_ERROR, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

test.use({ config: "failing" });

test("a runner's exception: the run fails, its message is in the run's error banner, and the list shows it failed", async ({ page, open, api }) => {
  await open("#/compose?f=Auth-SSO");
  await expectPreview(page, 3, 60);
  await runButton(page, 3).click();
  const runId = await currentRunId(page);
  await api.waitFor(runId, ["FAILED"]);

  const runPage = page.locator(".run-page");
  await expect(runPage).toHaveAttribute("data-run-state", "FAILED");
  await expect(runPage.locator(".run-head")).toContainText(msg("runState.FAILED"));
  const banner = page.getByRole("alert").filter({ hasText: msg("run.runnerError") });
  await expect(banner).toContainText(EXPLODING_ERROR);
  // The runner's own words, marked as such.
  await expect(banner.locator(".foreign")).toHaveText(EXPLODING_ERROR);
  await expect(banner.locator(".foreign")).toHaveAttribute("lang", "en");

  await page.getByRole("link", { name: msg("nav.runs"), exact: true }).click();
  const row = page.locator(`tr[data-run-id="${runId}"]`);
  await expect(row).toHaveAttribute("data-state", "FAILED");
  await expect(row).toContainText(msg("runState.FAILED"));
});
