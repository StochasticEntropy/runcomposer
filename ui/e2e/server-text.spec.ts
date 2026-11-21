// What the server says about itself at boot: the language its free text is
// in (`core.text_language`), which decides when the UI marks such text as
// not translated, and whether it serves the API documentation viewers.

import type { Locator } from "@playwright/test";

import { EXPLODING_ERROR, currentRunId, expect, expectPreview, msg, runButton, test } from "./fixtures.ts";

const languageMark = (text: Locator) => text.evaluate((element) => getComputedStyle(element, "::before").content);

test.describe("a deployment whose server writes German", () => {
  test.use({ config: "germanText", locale: "de-DE" });

  test("its text is not marked on a German page, and marked as German on an English one", async ({ page, open, api }) => {
    await open("#/compose?f=Auth-SSO");
    await expectPreview(page, 3, 60, "de");
    await runButton(page, 3, "de").click();
    const runId = await currentRunId(page);
    await api.waitFor(runId, ["FAILED"]);

    const error = page.getByRole("alert").filter({ hasText: msg("run.runnerError", {}, "de") }).locator(".foreign");
    await expect(error).toHaveText(EXPLODING_ERROR);
    await expect(error).toHaveAttribute("lang", "de");
    await expect(error).not.toHaveClass(/is-marked/);
    expect(await languageMark(error)).toBe("none");

    await page.getByRole("combobox", { name: msg("prefs.language", {}, "de") }).selectOption("en");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    const shown = page.getByRole("alert").filter({ hasText: msg("run.runnerError") }).locator(".foreign");
    await expect(shown).toHaveText(EXPLODING_ERROR);
    await expect(shown).toHaveAttribute("lang", "de");
    await expect(shown).toHaveClass(/is-marked/);
    await expect(shown).toHaveAttribute("title", msg("foreign.hint"));
    expect(await languageMark(shown)).toMatch(/^"de"/);
  });

  test("the API link opens the documentation viewer the server serves", async ({ page, open, server }) => {
    await open("#/compose");
    const link = page.getByTestId("api-link");
    await expect(link).toHaveText(msg("nav.apiDocs", {}, "de"));
    await expect(link).toHaveAttribute("href", "/docs");
    expect((await fetch(`${server.url}/docs`)).status).toBe(200);
  });
});

test.describe("a deployment with the defaults", () => {
  test.use({ config: "failing" });

  test("the API link is the OpenAPI description, since /docs is not served", async ({ page, open, server }) => {
    await open("#/compose");
    const link = page.getByTestId("api-link");
    await expect(link).toHaveText(msg("nav.apiSchema"));
    await expect(link).toHaveAttribute("href", "/openapi.json");
    expect((await fetch(`${server.url}/openapi.json`)).status).toBe(200);
    expect((await fetch(`${server.url}/docs`)).status).toBe(404);
  });
});
