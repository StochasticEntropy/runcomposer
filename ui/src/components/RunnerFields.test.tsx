// @vitest-environment jsdom
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { initialValues } from "../lib/params.ts";
import type { ParameterSpec, RunnerInfo } from "../lib/types.ts";
import { renderWithI18n, translator } from "../test/render.tsx";
import type { ApiError } from "../lib/request.ts";
import { LabelsInput, ParameterForm, RunnerSelect, healthChecks, problemTexts, refusedValue } from "./RunnerFields.tsx";

afterEach(cleanup);

const spec = (name: string, kind: ParameterSpec["kind"], extra: Partial<ParameterSpec> = {}): ParameterSpec => ({
  name,
  kind,
  label: name,
  help: "",
  default: null,
  choices: [],
  required: false,
  ...extra,
});

const SPECS = [
  spec("seed", "string", { label: "Seed", default: "demo", help: "Same seed, same verdicts.", required: true }),
  spec("retries", "integer", { label: "Retries" }),
  spec("allow_drift", "boolean", { label: "Allow drift" }),
  spec("browser", "choice", { label: "Target browser", choices: ["chromium", "firefox"] }),
  spec("partitions", "multi_choice", { label: "Partitions", choices: ["chromium", "firefox"], default: ["chromium", "firefox"] }),
];

describe("the parameter form", () => {
  it("renders one labelled control per declared parameter, with its default", () => {
    renderWithI18n(<ParameterForm specs={SPECS} values={initialValues(SPECS)} onChange={() => undefined} problems={{}} />);
    const seed = screen.getByRole("textbox", { name: /Seed/ }) as HTMLInputElement;
    expect(seed.value).toBe("demo");
    expect(seed.getAttribute("aria-required")).toBe("true");
    expect(seed.getAttribute("aria-describedby")).toBeTruthy();
    expect(screen.getByText("Same seed, same verdicts.")).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "Retries" }) as HTMLInputElement).inputMode).toBe("numeric");
    expect((screen.getByRole("checkbox", { name: "Allow drift" }) as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole("combobox", { name: "Target browser" })).toBeTruthy();
    const group = screen.getByRole("group", { name: "Partitions" });
    expect(group.querySelectorAll("input[type=checkbox]:checked")).toHaveLength(2);
  });

  it("reports every kind of change with the right type", () => {
    const onChange = vi.fn();
    const values = initialValues(SPECS);
    renderWithI18n(<ParameterForm specs={SPECS} values={values} onChange={onChange} problems={{}} />);
    fireEvent.change(screen.getByRole("textbox", { name: /Seed/ }), { target: { value: "b" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...values, seed: "b" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Allow drift" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...values, allow_drift: true });
    fireEvent.change(screen.getByRole("combobox", { name: "Target browser" }), { target: { value: "firefox" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...values, browser: "firefox" });
    fireEvent.click(screen.getByRole("checkbox", { name: "chromium" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...values, partitions: ["firefox"] });
  });

  it("puts a problem on its field: text, alert, aria-invalid", () => {
    renderWithI18n(
      <ParameterForm
        specs={SPECS}
        values={initialValues(SPECS)}
        onChange={() => undefined}
        problems={{ retries: { text: "Enter a whole number." } }}
      />,
    );
    const retries = screen.getByRole("textbox", { name: "Retries" });
    expect(retries.getAttribute("aria-invalid")).toBe("true");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("Enter a whole number.");
    expect(retries.getAttribute("aria-describedby")).toContain(alert.id);
    expect(screen.getByRole("textbox", { name: /Seed/ }).getAttribute("aria-invalid")).toBeNull();
  });

  it("renders nothing for a runner without parameters", () => {
    const { container } = renderWithI18n(<ParameterForm specs={[]} values={{}} onChange={() => undefined} problems={{}} />);
    expect(container.innerHTML).toBe("");
  });
});

describe("text and mapping parameters", () => {
  const specs = [
    spec("notes", "text", { label: "Release notes" }),
    spec("variables", "mapping", { label: "Extra variables", default: { locale: "de" } }),
  ];

  it("a text is a multi-line field; a mapping is rows of name and value that can be added and removed", () => {
    const onChange = vi.fn();
    const values = initialValues(specs);
    renderWithI18n(<ParameterForm specs={specs} values={values} onChange={onChange} problems={{}} />);
    expect(screen.getByRole("textbox", { name: "Release notes" })).toBeInstanceOf(HTMLTextAreaElement);
    const group = screen.getByRole("group", { name: "Extra variables" });
    expect((screen.getByRole("textbox", { name: "Extra variables, name 1" }) as HTMLInputElement).value).toBe("locale");
    fireEvent.change(screen.getByRole("textbox", { name: "Extra variables, value 1" }), { target: { value: "en" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...values, variables: [{ key: "locale", value: "en" }] });
    fireEvent.click(screen.getByRole("button", { name: "Add a row" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...values, variables: [{ key: "locale", value: "de" }, { key: "", value: "" }] });
    fireEvent.click(screen.getByRole("button", { name: "Remove row 1 of Extra variables" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...values, variables: [] });
    expect(group.querySelectorAll(".mapping-row")).toHaveLength(1);
  });

  it("says which form a value must have", () => {
    // The message is a sentence; the pattern is shown once, as the form the field expects — not inside the message.
    expect(problemTexts(translator.t, { build: "pattern" })).toEqual({ build: { text: "This does not have the form the field expects." } });
    const patterned = [spec("build", "string", { label: "Build number", pattern: "\\d{4}\\.\\d{1,2}" })];
    renderWithI18n(
      <ParameterForm
        specs={patterned}
        values={{ build: "x" }}
        onChange={() => undefined}
        problems={{ build: { text: "This value was not accepted.", foreign: "parameter 'build': 'x' does not match the pattern" } }}
      />,
    );
    expect(document.querySelector(".pattern-hint code")!.textContent).toBe("\\d{4}\\.\\d{1,2}");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("This value was not accepted.");
    // The server's own words are marked as text the UI did not write.
    expect(alert.querySelector('[lang="en"]')!.textContent).toContain("does not match the pattern");
  });
});

describe("the names of a mapping", () => {
  const switches = [spec("switches", "mapping", { label: "Feature switches", key_pattern: "[A-Z][A-Za-z-]+", pattern: "on|off" })];
  const rows = [
    { key: "Checkout-Express", value: "on" },
    { key: "payments wallets", value: "off" },
  ];

  it("shows the form a name must have next to the form of a value, each once and as written", () => {
    renderWithI18n(<ParameterForm specs={switches} values={{ switches: rows }} onChange={() => undefined} problems={{}} />);
    const hints = [...document.querySelectorAll(".pattern-hint")].map((hint) => hint.textContent);
    expect(hints).toEqual(["Expected form of a name: [A-Z][A-Za-z-]+", "Expected form of a value: on|off"]);
    // Both are part of what describes the field to assistive technology.
    const described = screen.getByRole("group", { name: "Feature switches" }).getAttribute("aria-describedby")!.split(" ");
    expect(described.map((id) => document.getElementById(id)?.getAttribute("data-pattern-of"))).toEqual(["name", "value"]);
  });

  it("words a name of the wrong form itself, and marks that name — not the whole field", () => {
    expect(problemTexts(translator.t, { switches: "key" })).toEqual({ switches: { text: "A name does not have the form names must have here." } });
    renderWithI18n(<ParameterForm specs={switches} values={{ switches: rows }} onChange={() => undefined} problems={problemTexts(translator.t, { switches: "key" })} />);
    expect(screen.getByRole("alert").textContent).toBe("A name does not have the form names must have here.");
    const invalid = (name: string) => screen.getByRole("textbox", { name }).getAttribute("aria-invalid");
    expect(invalid("Feature switches, name 2")).toBe("true");
    expect(invalid("Feature switches, name 1")).toBeNull();
    expect(invalid("Feature switches, value 2")).toBeNull();
  });

  it("words a refusal by the kind the server names — and shows the server's sentence only for the runner's own reason", () => {
    const refusal = (problem: string, key?: string) =>
      ({ code: "invalid-parameter", status: 400, detail: "parameter 'build': 'x' does not match the pattern '\\\\d{4}'", extra: { parameter: "build", problem, ...(key ? { key } : {}) } }) as unknown as ApiError;
    const patterned = spec("build", "string", { pattern: "\\d{4}" });
    expect(refusedValue(translator, patterned, refusal("pattern"))).toEqual({ text: "This does not have the form the field expects." });
    expect(refusedValue(translator, switches[0], refusal("key-pattern", "x-bad name"))).toEqual({ text: "A name does not have the form names must have here. (x-bad name)" });
    expect(refusedValue(translator, patterned, refusal("choice"))).toEqual({ text: "Choose one of the listed values." });
    // The runner's own reason is something only the runner knows: its sentence is shown, as the runner's.
    const runner = refusedValue(translator, spec("seed", "string"), refusal("runner"));
    expect(runner.foreign).toBe(refusal("runner").detail);
    expect(runner.text).toBe("The runner did not accept this value:");
  });
});

describe("the runner select", () => {
  const runners: RunnerInfo[] = [
    { id: "demo", title: "Demo (simulated)", description: "Simulates.", parameters: [], health: { status: "ok", detail: "always available", data: {} } },
    { id: "pool", title: "Local pool", parameters: [], health: { status: "down", detail: "the framework is not installed", data: {} } },
    { id: "ghost", error: "cannot import the plugin" },
  ];
  const app = { runners, runnersChecking: false, refreshRunners: vi.fn() };

  it("lists every runner — a broken one disabled, with its reason on the page", () => {
    renderWithI18n(<RunnerSelect value="demo" onChange={() => undefined} />, app);
    const options = screen.getAllByRole("option") as HTMLOptionElement[];
    expect(options.map((option) => option.textContent)).toEqual([
      "demo — Demo (simulated)",
      "pool — Local pool — down",
      "ghost — unavailable",
    ]);
    expect(options.map((option) => option.disabled)).toEqual([false, false, true]);
    expect(screen.getByText("cannot import the plugin", { exact: false })).toBeTruthy();
    expect(screen.getByText("always available")).toBeTruthy();
  });

  it("shows the health and its reason for an unhealthy runner, and can probe again", () => {
    renderWithI18n(<RunnerSelect value="pool" onChange={() => undefined} />, app);
    expect(screen.getByText("the framework is not installed")).toBeTruthy();
    expect(screen.getByText(/reports a problem/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(app.refreshRunners).toHaveBeenCalledWith(true);
  });

  it("shows what a runner checked about itself, check by check", () => {
    const checked: RunnerInfo[] = [
      {
        id: "nightly-ci",
        title: "Nightly job",
        parameters: [],
        health: {
          status: "degraded",
          detail: "job not readable",
          data: {
            checks: [
              { check: "server reachable", ok: true, detail: "version 2" },
              { check: "job readable", ok: false, detail: "404 Not Found" },
            ],
          },
        },
      },
    ];
    renderWithI18n(<RunnerSelect value="nightly-ci" onChange={() => undefined} />, { ...app, runners: checked });
    const items = screen.getByRole("list", { name: "What this runner checked" }).querySelectorAll("li");
    expect([...items].map((item) => [item.getAttribute("data-ok"), item.textContent])).toEqual([
      ["true", "ok server reachable — version 2"],
      ["false", "failed job readable — 404 Not Found"],
    ]);
    expect(healthChecks(checked[0])).toHaveLength(2);
    expect(healthChecks({ id: "demo", health: { status: "ok", detail: "", data: {} } })).toEqual([]);
  });

  it("says so when no runner is configured", () => {
    renderWithI18n(<RunnerSelect value={null} onChange={() => undefined} />, { ...app, runners: [] });
    expect(screen.getByText(/No runner is configured/)).toBeTruthy();
  });
});

describe("the labels input", () => {
  it("adds key=value on Enter", () => {
    const onChange = vi.fn();
    const onDraft = vi.fn();
    renderWithI18n(<LabelsInput labels={{ env: "staging" }} onChange={onChange} draft="suite=nightly" onDraftChange={onDraft} problem={null} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Labels" }), { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({ env: "staging", suite: "nightly" });
    expect(onDraft).toHaveBeenCalledWith("");
    fireEvent.click(screen.getByRole("button", { name: "Remove label env=staging" }));
    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("refuses what is not key=value, on the field", () => {
    const onChange = vi.fn();
    renderWithI18n(<LabelsInput labels={{}} onChange={onChange} draft="nightly" onDraftChange={() => undefined} problem={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Add label" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toBe("A label is written key=value.");
    expect(screen.getByRole("textbox", { name: "Labels" }).getAttribute("aria-invalid")).toBe("true");
  });
});
