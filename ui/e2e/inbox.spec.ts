// The inbox: deliveries that could not be matched to a run wait for a person.

import type { Page } from "@playwright/test";

import { expect, msg, test } from "./fixtures.ts";
import type { Api, Server } from "./fixtures.ts";

const SSO = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"];

/** Push files to a run's results endpoint with its token. */
async function push(server: Server, runId: string, token: string, files: Record<string, string>): Promise<Response> {
  const form = new FormData();
  for (const [name, content] of Object.entries(files)) form.append("files", new Blob([content], { type: "application/json" }), name);
  return fetch(`${server.api}/runs/${runId}/results`, { method: "POST", body: form, headers: { Authorization: `Bearer ${token}` } });
}

const results = JSON.stringify({ format: "runcomposer-verdicts", verdicts: SSO.map((name) => ({ name, status: "PASS", duration_ms: 20 })) });

/** A run awaiting results, with two deliveries for it in the inbox: one that
 * contradicts the run's document, one that cannot be read. */
async function strandTwo(api: Api, server: Server): Promise<{ runId: string }> {
  const run = await api.compose({ selection: { tag_filter: "Auth-SSO" }, title: "Anmeldung extern" });
  const exported = await api.post<{ ingest_token: string }>(`/runs/${run.id}/dispatch`, { mode: "export" });
  const mismatch = await push(server, run.id, exported.ingest_token, {
    "results.json": results,
    "runcomposer_run.json": JSON.stringify({ run_id: run.id, spec_sha256: "0".repeat(64) }),
  });
  expect(mismatch.status).toBe(409);
  const unreadable = await push(server, run.id, exported.ingest_token, {
    "results.json": JSON.stringify({ format: "runcomposer-verdicts", verdicts: [{ status: "PASS" }] }),
  });
  expect(unreadable.status).toBe(422);
  return { runId: run.id };
}

const badge = (page: Page) => page.getByTestId("inbox-badge");
const row = (page: Page, reason: string) => page.locator(".inbox-table tbody tr").filter({ hasText: msg(`inbox.reason.${reason}`) });

test("the nav shows how many deliveries wait; attach one to its run, discard the other after being asked", async ({ page, open, api, server }) => {
  const { runId } = await strandTwo(api, server);
  await open();
  // The badge is on every view, not only on the inbox.
  await expect(badge(page)).toHaveText(new RegExp(`^2\\s*${msg("nav.inboxWaiting", { count: 2 })}$`));
  await page.getByRole("navigation", { name: msg("nav.label") }).getByRole("link", { name: new RegExp(`^${msg("nav.inbox")}`) }).click();
  await expect(page.getByRole("heading", { level: 1, name: msg("inbox.title") })).toBeVisible();
  await expect(page.locator(".inbox-table tbody tr")).toHaveCount(2);
  // Each entry says, in the viewer's language, why it is here — and the server's detail.
  await expect(row(page, "sha-mismatch")).toContainText("does not match the spec bytes");
  await expect(row(page, "bad-bundle")).toContainText("'name' is required");
  await expect(row(page, "sha-mismatch")).toContainText(msg("inbox.transport.push"));

  // Discard asks first, and Cancel keeps the delivery.
  await row(page, "bad-bundle").getByRole("button", { name: msg("inbox.discard") }).click();
  const confirm = page.getByRole("dialog", { name: msg("inbox.discardTitle") });
  await expect(confirm).toContainText(msg("inbox.discardExplain"));
  await confirm.getByRole("button", { name: msg("action.cancel") }).click();
  await expect(page.locator(".inbox-table tbody tr")).toHaveCount(2);
  await row(page, "bad-bundle").getByRole("button", { name: msg("inbox.discard") }).click();
  await page.getByRole("dialog", { name: msg("inbox.discardTitle") }).getByRole("button", { name: msg("inbox.discardConfirm") }).click();
  await expect(page.getByText(msg("inbox.discarded"))).toBeVisible();
  await expect(page.locator(".inbox-table tbody tr")).toHaveCount(1);
  await expect(badge(page)).toContainText("1");

  // Attach: the run the delivery names is offered; a wrong id is refused in the dialog.
  await row(page, "sha-mismatch").getByRole("button", { name: msg("inbox.attach") }).click();
  const attach = page.getByRole("dialog", { name: msg("inbox.attachTitle") });
  const runField = attach.getByRole("combobox", { name: msg("inbox.runId") });
  await expect(runField).toHaveValue(runId);
  // The run is named by its title, not only by its id.
  await expect(attach).toContainText("Anmeldung extern");
  await expect(row(page, "sha-mismatch").getByRole("link", { name: "Anmeldung extern" })).toHaveAttribute("href", `#/runs/${runId}`);
  await runField.fill("01NOSUCHRUN000000000000000");
  await attach.getByRole("button", { name: msg("inbox.attach") }).click();
  await expect(attach.getByRole("alert")).toContainText(msg("error.code.unknown-run"));
  await runField.fill(runId);
  await attach.getByRole("button", { name: msg("inbox.attach") }).click();
  await expect(attach).toBeHidden();
  await expect(page.getByText(msg("inbox.attached", { count: 3 }))).toBeVisible();
  await expect(page.getByText(msg("inbox.empty"))).toBeVisible();
  await expect(badge(page)).toHaveCount(0);

  // The run it was attached to is complete, and says where its results came from.
  await page.getByRole("link", { name: msg("inbox.openRun") }).click();
  await expect(page).toHaveURL(new RegExp(`#/runs/${runId}`));
  await expect(page.getByTestId("run-status")).toContainText(msg("completion.PASS"));
  await page.getByRole("link", { name: msg("run.tab.activity") }).click();
  await expect(page.locator(".events")).toContainText(msg("event.delivery.attached"));
});

test("attaching a delivery that matches nothing in the run is not reported as a success", async ({ page, open, api, server }) => {
  const run = await api.compose({ selection: { tag_filter: "Auth-SSO" }, title: "Anmeldung extern" });
  const exported = await api.post<{ ingest_token: string }>(`/runs/${run.id}/dispatch`, { mode: "export" });
  // Results for an item the run does not have, with a marker from another document.
  const stray = await push(server, run.id, exported.ingest_token, {
    "results.json": JSON.stringify({ format: "runcomposer-verdicts", verdicts: [{ name: "Shop.Cart.Core.T999", status: "PASS", duration_ms: 20 }] }),
    "runcomposer_run.json": JSON.stringify({ run_id: run.id, spec_sha256: "0".repeat(64) }),
  });
  expect(stray.status).toBe(409);
  await open("#/inbox");
  await row(page, "sha-mismatch").getByRole("button", { name: msg("inbox.attach") }).click();
  const attach = page.getByRole("dialog", { name: msg("inbox.attachTitle") });
  await attach.getByRole("button", { name: msg("inbox.attach") }).click();
  const outcome = page.locator(".notice-warn");
  await expect(outcome).toContainText(msg("inbox.nothingRecorded"));
  await expect(page.locator(".notice-ok")).toHaveCount(0);
  await outcome.getByRole("link", { name: msg("inbox.openRun") }).click();
  await expect(page).toHaveURL(new RegExp(`#/runs/${run.id}`));
});

test("a delivery can become a run of its own", async ({ page, open, api, server }) => {
  await strandTwo(api, server);
  await open("#/inbox");
  await row(page, "sha-mismatch").getByRole("button", { name: msg("inbox.promote") }).click();
  const dialog = page.getByRole("dialog", { name: msg("inbox.promoteTitle") });
  await expect(dialog.getByRole("combobox", { name: msg("inbox.format") })).toHaveValue("runcomposer-verdicts");
  await dialog.getByRole("button", { name: msg("inbox.promote") }).click();
  await expect(page.getByText(msg("inbox.promoted", { count: 3 }))).toBeVisible();
  await page.getByRole("link", { name: msg("inbox.openRun") }).click();
  await expect(page.getByTestId("run-status")).toContainText(msg("completion.PASS"));
  await expect(page.locator(".run-facts").getByText(msg("origin.ingested"), { exact: true })).toBeVisible();
  expect(await api.runs()).toHaveLength(2);
});

test("when the inbox cannot be loaded it says so instead of claiming to be empty", async ({ page, open }) => {
  await page.route("**/api/v1/quarantine", (route) => route.abort("connectionrefused"));
  await open("#/inbox");
  await expect(page.getByRole("alert")).toContainText(msg("error.code.network"));
  await expect(page.getByText(msg("inbox.empty"))).toHaveCount(0);
  await page.unroute("**/api/v1/quarantine");
  await page.getByRole("button", { name: msg("action.retry") }).click();
  await expect(page.getByText(msg("inbox.empty"))).toBeVisible();
});
