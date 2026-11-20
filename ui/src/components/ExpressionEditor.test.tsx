// @vitest-environment jsdom
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { fromAst, toAst } from "../lib/editor.ts";
import type { GroupNode } from "../lib/editor.ts";
import { formatFilter } from "../lib/filter.ts";
import type { FilterAst } from "../lib/types.ts";
import { renderWithI18n } from "../test/render.tsx";
import { ExpressionEditor } from "./ExpressionEditor.tsx";

afterEach(cleanup);

let current: GroupNode;
const text = () => formatFilter(toAst(current));

function Harness({ initial, errorPattern }: { initial: FilterAst | null; errorPattern?: string }) {
  const [root, setRoot] = useState(() => fromAst(initial));
  const [textDraft, setTextDraft] = useState<string | null>(null);
  current = root;
  const broken = errorPattern ? root.children.find((child) => child.kind === "tag" && child.pattern === errorPattern) : undefined;
  return (
    <ExpressionEditor
      root={root}
      onChange={setRoot}
      onPick={() => undefined}
      tagListId="tags"
      errorNodeId={broken?.id ?? null}
      labelOf={() => null}
      textDraft={textDraft}
      onTextDraft={setTextDraft}
    />
  );
}

// A text field with a list of suggestions is a combobox to assistive technology.
const addField = (name = "Add a tag or pattern") => screen.getByRole("combobox", { name });

describe("the expression editor", () => {
  it("adds rules by typing and Enter, into the root's AND", () => {
    renderWithI18n(<Harness initial={null} />);
    for (const pattern of ["Payments", "prefix:Checkout-"]) {
      fireEvent.change(addField(), { target: { value: pattern } });
      fireEvent.submit(addField().closest("form")!);
    }
    expect(text()).toBe("Payments AND prefix:Checkout-");
    expect((addField() as HTMLInputElement).value).toBe("");
  });

  it("negates and removes a rule with named buttons", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: ["Payments", "Smoke"] }} />);
    const negate = screen.getByRole("button", { name: "NOT: negate Smoke" });
    expect(negate.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(negate);
    expect(text()).toBe("Payments AND NOT Smoke");
    expect(screen.getByRole("button", { name: "NOT: negate Smoke" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Remove Payments" }));
    expect(text()).toBe("NOT Smoke");
  });

  it("switches a group between AND and OR", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: ["Payments", "Smoke"] }} />);
    fireEvent.change(screen.getByRole("combobox", { name: "How the rules of this group combine" }), { target: { value: "OR" } });
    expect(text()).toBe("Payments OR Smoke");
  });

  it("adds a subgroup with the other operator, and fills it", () => {
    renderWithI18n(<Harness initial={"Payments"} />);
    fireEvent.click(screen.getByRole("button", { name: "Add group" }));
    const group = screen.getByRole("group", { name: "Group, combined with OR" });
    for (const pattern of ["Smoke", "Regression"]) {
      const field = within(group).getByRole("combobox", { name: "Add a tag or pattern to this group" });
      fireEvent.change(field, { target: { value: pattern } });
      fireEvent.submit(field.closest("form")!);
    }
    expect(text()).toBe("Payments AND (Smoke OR Regression)");
    fireEvent.click(within(group).getByRole("button", { name: "NOT: negate this group" }));
    expect(screen.getByRole("button", { name: "NOT: negate the whole filter" })).toBeTruthy();
    expect(within(group).getByRole("button", { name: "Pick tags for this group…" })).toBeTruthy();
    expect(text()).toBe("Payments AND NOT (Smoke OR Regression)");
  });

  it("edits a pattern in place", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: ["Payments", "Smoke"] }} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit Smoke" }));
    const field = screen.getByRole("combobox", { name: "Edit Smoke" });
    fireEvent.change(field, { target: { value: "regex:^Sprint-1[23]$" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(text()).toBe("Payments AND regex:^Sprint-1[23]$");
  });

  it("marks the rule the server refused", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: ["Payments", "regex:("] }} errorPattern="regex:(" />);
    expect(screen.getByRole("button", { name: "Edit regex:(" }).getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("button", { name: "Edit Payments" }).getAttribute("aria-invalid")).toBeNull();
  });

  it("reads the whole expression as text, and says what is wrong with a bad one", () => {
    renderWithI18n(<Harness initial={"Payments"} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit as text" }));
    const area = screen.getByRole("textbox", { name: "Filter expression" }) as HTMLTextAreaElement;
    expect(area.value).toBe("Payments");
    fireEvent.change(area, { target: { value: "Payments AND (Smoke OR" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert").textContent).toContain("ends where a tag or pattern is expected");
    expect(text()).toBe("Payments");
    fireEvent.change(area, { target: { value: "Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(text()).toBe("Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-");
    expect(screen.getByRole("button", { name: "NOT: negate prefix:Quarantine-" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("shows the match-everything filter as what it means", () => {
    renderWithI18n(<Harness initial={{ not: "regex:^$" }} />);
    expect(screen.getByRole("button", { name: "Edit All items" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(toAst(current)).toBeNull();
  });

  it("draws everything as one plain rule — not as a negation, with nothing to negate — and writes it *", () => {
    const { container } = renderWithI18n(<Harness initial={{ not: "regex:^$" }} />);
    const chip = container.querySelector(".chip")!;
    expect(chip.getAttribute("data-pattern")).toBe("*");
    expect(chip.className).toContain("is-everything");
    expect(chip.className).not.toContain("is-negated");
    expect(chip.querySelector("[aria-pressed]")).toBeNull();
    expect(chip.textContent).not.toContain("regex");
    expect(text()).toBe("*");
    // As text it is the star, and the star typed is everything.
    fireEvent.click(screen.getByRole("button", { name: "Edit as text" }));
    const area = screen.getByRole("textbox", { name: "Filter expression" }) as HTMLTextAreaElement;
    expect(area.value).toBe("*");
    fireEvent.change(area, { target: { value: "* AND NOT prefix:Quarantine-" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(toAst(current)).toEqual({ op: "AND", items: [{ not: "regex:^$" }, { not: "prefix:Quarantine-" }] });
    expect(text()).toBe("* AND NOT prefix:Quarantine-");
  });

  it("keeps the filter in effect in view while another one is being typed", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: ["Payments", "Smoke"] }} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit as text" }));
    const area = screen.getByRole("textbox", { name: "Filter expression" });
    fireEvent.change(area, { target: { value: "Checkout" } });
    // Typed is not applied: the filter is what it was, and the screen says so.
    expect(text()).toBe("Payments AND Smoke");
    expect(screen.getByTestId("applied-filter").textContent).toBe("In effect now: Payments AND Smoke");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(text()).toBe("Payments AND Smoke");
    expect(screen.queryByTestId("applied-filter")).toBeNull();
  });

  it("says the operator once per group, however many rules it has, and counts a long group", () => {
    const tags = [
      ...["Payments", "Payments-Cards", "Checkout", "Checkout-Express", "Checkout-Gift", "Checkout-Standard", "Cart", "Cart-Core", "Catalog", "Catalog-Browse"],
      ...["Catalog-Search", "Auth", "Auth-Login", "Auth-SSO", "Auth-Signup", "Smoke", "Regression", "Sprint-12", "Sprint-13", "Sprint-14"],
    ];
    const { container } = renderWithI18n(<Harness initial={{ op: "OR", items: tags }} />);
    expect(container.querySelectorAll(".chip")).toHaveLength(20);
    expect(screen.getAllByRole("combobox", { name: "How the rules of this group combine" })).toHaveLength(1);
    // Between the rules there is nothing: no word, no element.
    const rules = container.querySelector(".expr-rules")!;
    expect([...rules.children].every((child) => child.children.length === 1 && child.firstElementChild!.classList.contains("chip"))).toBe(true);
    expect(rules.className).toContain("is-long");
    expect(screen.getByText("20 rules in this group — they scroll.")).toBeTruthy();
    // Every rule still has its three controls, in the order they read.
    const controls = [...rules.children[19]!.querySelectorAll("button")].map((button) => button.getAttribute("aria-label"));
    expect(controls).toEqual(["Edit Sprint-14", "NOT: negate Sprint-14", "Remove Sprint-14"]);
  });
});
