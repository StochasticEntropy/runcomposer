// @vitest-environment jsdom
// Text the server or a runner wrote, in the deployment's `text_language`:
// the language it carries, and when it is marked as not translated. The
// API reference link, which depends on what the server serves. And the
// server's filter-syntax refusal, worded by the UI.
import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiLink } from "../App.tsx";
import { createTranslator, marksUntranslated, primaryLanguage } from "../lib/i18n.ts";
import { ApiError } from "../lib/request.ts";
import de from "../locales/de.json";
import { renderWithI18n } from "../test/render.tsx";
import { ErrorText, Foreign } from "./ui.tsx";

afterEach(cleanup);

const german = createTranslator(de, "de");

describe("the untranslated mark", () => {
  it("compares languages by their primary subtag", () => {
    expect(primaryLanguage("de-CH")).toBe("de");
    expect(primaryLanguage("EN")).toBe("en");
    expect(marksUntranslated("de", "en")).toBe(true);
    expect(marksUntranslated("en", "de")).toBe(true);
    expect(marksUntranslated("de", "de")).toBe(false);
    expect(marksUntranslated("de", "de-CH")).toBe(false);
    expect(marksUntranslated("en", "en-GB")).toBe(false);
    expect(marksUntranslated("en", "pt-BR")).toBe(true);
  });

  it("is shown when the page speaks another language than the server's text", () => {
    renderWithI18n(<Foreign>runner went away</Foreign>, undefined, { ...german, textLanguage: "en" });
    const text = screen.getByText("runner went away");
    expect(text.getAttribute("lang")).toBe("en");
    expect(text.classList.contains("is-marked")).toBe(true);
    expect(text.getAttribute("title")).toBe(german.t("foreign.hint"));
  });

  it("is not shown when they speak the same language, and the text says its own language", () => {
    renderWithI18n(<Foreign>Runner nicht erreichbar</Foreign>, undefined, { ...german, textLanguage: "de-CH" });
    const text = screen.getByText("Runner nicht erreichbar");
    expect(text.getAttribute("lang")).toBe("de-CH");
    expect(text.classList.contains("is-marked")).toBe(false);
    expect(text.getAttribute("title")).toBeNull();
  });

  it("is shown on an English page for a deployment whose server text is German", () => {
    renderWithI18n(<Foreign>Runner nicht erreichbar</Foreign>, undefined, { textLanguage: "de" });
    const text = screen.getByText("Runner nicht erreichbar");
    expect(text.getAttribute("lang")).toBe("de");
    expect(text.classList.contains("is-marked")).toBe(true);
  });

  it("applies to the detail of an error", () => {
    renderWithI18n(<ErrorText error={new ApiError(409, "dispatch-refused", "no agent is free")} />, undefined, { textLanguage: "de" });
    expect(screen.getByText("no agent is free").getAttribute("lang")).toBe("de");
  });
});

describe("the API reference link", () => {
  it("opens the documentation viewer where the server serves it", () => {
    renderWithI18n(<ApiLink docs />);
    const link = screen.getByRole("link", { name: "API reference" });
    expect(link.getAttribute("href")).toBe("/docs");
  });

  it("is the OpenAPI description otherwise, in both languages", () => {
    renderWithI18n(<ApiLink docs={false} />);
    expect(screen.getByRole("link", { name: "API description (OpenAPI)" }).getAttribute("href")).toBe("/openapi.json");
    cleanup();
    renderWithI18n(<ApiLink docs={false} />, undefined, german);
    expect(screen.getByRole("link", { name: "API-Beschreibung (OpenAPI)" }).getAttribute("href")).toBe("/openapi.json");
  });
});

describe("a filter-syntax refusal", () => {
  it("is worded by the UI: the problem, at the column the server counted", () => {
    const error = new ApiError(400, "filter-syntax", "cannot read the filter text at column 9: …", {
      problem: "missing-operator",
      position: 8,
      token: "Smoke",
    });
    renderWithI18n(<p data-testid="error"><ErrorText error={error} /></p>);
    const shown = screen.getByTestId("error");
    expect(shown.textContent).toContain("The filter text cannot be read.");
    expect(shown.textContent).toContain("Position 9: an operator is missing before “Smoke”.");
    expect(shown.querySelector(".foreign")).toBeNull();
  });

  it("falls back to the server's words for a problem it does not know", () => {
    const error = new ApiError(400, "filter-syntax", "cannot read the filter text", { problem: "something-new", position: 0 });
    renderWithI18n(<p data-testid="error"><ErrorText error={error} /></p>);
    expect(screen.getByTestId("error").querySelector(".foreign")!.textContent).toBe("cannot read the filter text");
  });
});
