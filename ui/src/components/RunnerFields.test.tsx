// @vitest-environment jsdom
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { initialValues } from "../lib/params.ts";
import type { ParameterSpec, RunnerInfo } from "../lib/types.ts";
import { renderWithI18n } from "../test/render.tsx";
import { LabelsInput, ParameterForm, RunnerSelect } from "./RunnerFields.tsx";

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
        problems={{ retries: "Enter a whole number." }}
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
      "Demo (simulated) (demo)",
      "Local pool (pool) — down",
      "ghost — unavailable",
    ]);
    expect(options.map((option) => option.disabled)).toEqual([false, false, true]);
    expect(screen.getByText("cannot import the plugin", { exact: false })).toBeTruthy();
    expect(screen.getByText("always available")).toBeTruthy();
  });

  it("shows the health and its reason for an unhealthy runner, and can probe again", () => {
    renderWithI18n(<RunnerSelect value="pool" onChange={() => undefined} />, app);
    expect(screen.getByText("the framework is not installed")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(app.refreshRunners).toHaveBeenCalledWith(true);
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
