// @vitest-environment jsdom
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { fromAst, toAst } from "../lib/editor.ts";
import type { GroupNode } from "../lib/editor.ts";
import { formatFilter, quotePattern } from "../lib/filter.ts";
import type { FilterAst } from "../lib/types.ts";
import { renderWithI18n } from "../test/render.tsx";
import { ExpressionEditor, textListId } from "./ExpressionEditor.tsx";

afterEach(cleanup);

let current: GroupNode;
const text = () => formatFilter(toAst(current));

function Harness({ initial, errorPattern, knownTags }: { initial: FilterAst | null; errorPattern?: string; knownTags?: string[] }) {
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
      knownTags={knownTags}
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

describe("the add field speaks the filter's grammar", () => {
  const type = (text: string, name?: string) => {
    fireEvent.change(addField(name), { target: { value: text } });
    fireEvent.submit(addField(name).closest("form")!);
  };

  it("reads an expression as the rules it says — the same text as in the text editor", () => {
    renderWithI18n(<Harness initial={null} />);
    type("Payments AND Smoke");
    expect(toAst(current)).toEqual({ op: "AND", items: ["Payments", "Smoke"] });
    expect(document.querySelectorAll(".chip")).toHaveLength(2);
    // More rules with the group's own operator are more rules of the group…
    type("Regression AND NOT prefix:Quarantine-");
    expect(text()).toBe("Payments AND Smoke AND Regression AND NOT prefix:Quarantine-");
    // …and the other operator is a group of its own.
    type("Cart OR Checkout");
    expect(text()).toBe("Payments AND Smoke AND Regression AND NOT prefix:Quarantine- AND (Cart OR Checkout)");
    expect((addField() as HTMLInputElement).value).toBe("");
  });

  it("lets an expression typed into the empty builder bring its operator along", () => {
    renderWithI18n(<Harness initial={null} />);
    type("Payments OR Cart");
    expect(toAst(current)).toEqual({ op: "OR", items: ["Payments", "Cart"] });
    expect((screen.getByRole("combobox", { name: "How the rules of this group combine" }) as HTMLSelectElement).value).toBe("OR");
  });

  it("reads NOT, the star and a pattern in quotes as the grammar has them", () => {
    renderWithI18n(<Harness initial={"Payments"} />);
    type("NOT Smoke");
    expect(text()).toBe("Payments AND NOT Smoke");
    // In quotes, operator words are letters of one tag.
    type('"Auth OR Cart"');
    expect(toAst(current)).toEqual({ op: "AND", items: ["Payments", { not: "Smoke" }, "Auth OR Cart"] });
    type('"regex:^Cart(V2)?$"');
    expect(text()).toBe('Payments AND NOT Smoke AND "Auth OR Cart" AND "regex:^Cart(V2)?$"');
  });

  it("refuses text that is no expression in place: nothing is added, the text stays, the place is named", () => {
    renderWithI18n(<Harness initial={"Payments"} />);
    type("Smoke AND (Cart OR");
    expect(text()).toBe("Payments");
    const field = addField() as HTMLInputElement;
    expect(field.value).toBe("Smoke AND (Cart OR");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("The expression ends where a tag or pattern is expected.");
    expect(field.getAttribute("aria-describedby")).toBe(alert.id);
    // Two words with nothing between them are not one tag with a space…
    type("Cart Smoke");
    expect(screen.getByRole("alert").textContent).toBe("Position 6: an operator is missing before “Smoke”.");
    // …the caret is where reading stopped…
    expect(field.selectionStart).toBe(5);
    // …and typing on takes the message away.
    fireEvent.change(field, { target: { value: "Cart AND Smoke" } });
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.submit(field.closest("form")!);
    expect(text()).toBe("Payments AND Cart AND Smoke");
  });

  it("says that operators are capitals when one is typed small", () => {
    renderWithI18n(<Harness initial={null} />);
    type("Payments and Smoke");
    expect(toAst(current)).toBeNull();
    expect(screen.getByRole("alert").textContent).toBe("Position 10: operators are written in capitals — AND, not “and”.");
  });

  it("reads a catalog tag spelled like an expression as the grammar does: unquoted two tags, quoted one", () => {
    // One grammar, one answer: the add field has no exception for a catalog
    // tag. The text editor, a link, the API and the CLI read the same.
    const catalog = ["Payments", "Smoke", "Cart AND Checkout"];
    renderWithI18n(<Harness initial={null} knownTags={catalog} />);
    type("Cart AND Checkout");
    expect(toAst(current)).toEqual({ op: "AND", items: ["Cart", "Checkout"] });
  });

  it("offers a catalog tag in its list as the field reads it: quoted where the grammar needs it", () => {
    const catalog = ["Payments", "Smoke", "Cart AND Checkout"];
    renderWithI18n(
      <>
        <datalist id="tags-text">
          {catalog.map((tag) => (
            <option key={tag} value={quotePattern(tag)} />
          ))}
        </datalist>
        <Harness initial={null} knownTags={catalog} />
      </>,
    );
    expect(addField().getAttribute("list")).toBe(textListId("tags"));
    const offered = [...document.querySelectorAll(`#${textListId("tags")} option`)].map((option) => option.getAttribute("value"));
    expect(offered).toEqual(["Payments", "Smoke", '"Cart AND Checkout"']);
    type(offered[2]!);
    expect(toAst(current)).toBe("Cart AND Checkout");
    expect(document.querySelector(".chip.is-unmatched")).toBeNull();
  });
});

describe("a rule no tag of the catalog matches", () => {
  const catalog = ["Payments", "Payments-Cards", "Checkout", "Smoke", "Sprint-12", "Sprint-13"];
  // A slip of the fingers: two neighbouring letters of a tag swapped.
  const slip = (tag: string) => tag.slice(0, 4) + tag[5] + tag[4] + tag.slice(6);
  const typo = slip("Payments");
  const NO_SUCH_PREFIX = `prefix:${slip("Sprint-")}`;

  it("is flagged on its chip, and the tag that was probably meant is one click away", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: [typo, "Smoke"] }} knownTags={catalog} />);
    const chip = document.querySelector(`.chip[data-pattern="${typo}"]`)!;
    expect(chip.className).toContain("is-unmatched");
    expect(chip.querySelector(".chip-flag")!.textContent).toBe("no tag");
    expect(document.querySelector('.chip[data-pattern="Smoke"]')!.className).not.toContain("is-unmatched");
    // The flag describes the rule to assistive technology too.
    const edit = screen.getByRole("button", { name: `Edit ${typo}` });
    expect(document.getElementById(edit.getAttribute("aria-describedby")!)!.textContent).toBe("no tag");

    const note = screen.getByRole("status", { name: "Rules that match no tag" });
    expect(note.textContent).toContain(`“${typo}” matches no tag of the catalog.`);
    fireEvent.click(within(note).getByRole("button", { name: `Replace ${typo} with Payments` }));
    expect(text()).toBe("Payments AND Smoke");
    expect(document.querySelector(".chip.is-unmatched")).toBeNull();
    expect(screen.queryByRole("status", { name: "Rules that match no tag" })).toBeNull();
  });

  it("is flagged for a pattern too — without guessing what was meant — and never for everything", () => {
    renderWithI18n(<Harness initial={{ op: "AND", items: ["prefix:Sprint-", NO_SUCH_PREFIX, { not: "regex:^$" }, "regex:(?i)smoke"] }} knownTags={catalog} />);
    const flagged = [...document.querySelectorAll(".chip.is-unmatched")].map((chip) => chip.getAttribute("data-pattern"));
    // The prefix nothing begins with is flagged. What the browser cannot read is left to the server.
    expect(flagged).toEqual([NO_SUCH_PREFIX]);
    const note = screen.getByRole("status", { name: "Rules that match no tag" });
    expect(within(note).queryAllByRole("button")).toHaveLength(0);
  });

  it("is not claimed while the catalog is not known", () => {
    renderWithI18n(<Harness initial={typo} />);
    expect(document.querySelector(".chip.is-unmatched")).toBeNull();
  });

  it("a typed expression is flagged rule by rule as soon as it is added", () => {
    renderWithI18n(<Harness initial={null} knownTags={catalog} />);
    fireEvent.change(addField(), { target: { value: `${typo} AND Smoke` } });
    fireEvent.submit(addField().closest("form")!);
    expect([...document.querySelectorAll(".chip")].map((chip) => [chip.getAttribute("data-pattern"), chip.classList.contains("is-unmatched")])).toEqual([
      [typo, true],
      ["Smoke", false],
    ]);
  });
});
