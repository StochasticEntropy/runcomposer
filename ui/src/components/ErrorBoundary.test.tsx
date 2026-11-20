// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary.tsx";

afterEach(cleanup);

function Broken({ message }: { message: string }): never {
  throw new Error(message);
}

const texts = { title: "This view ran into an error.", hint: "Reload the page.", reloadLabel: "Reload the page" };

describe("the error boundary", () => {
  it("shows a message and a way out instead of a blank page", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <ErrorBoundary {...texts} resetKey="runs:">
        <Broken message="cannot read the run" />
      </ErrorBoundary>,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("This view ran into an error.");
    expect(alert.textContent).toContain("cannot read the run");
    expect(screen.getByRole("button", { name: "Reload the page" })).toBeTruthy();
    quiet.mockRestore();
  });

  it("gives the next view a clean start", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { rerender } = render(
      <ErrorBoundary {...texts} resetKey="runs:">
        <Broken message="boom" />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    rerender(
      <ErrorBoundary {...texts} resetKey="inbox:">
        <p>the inbox</p>
      </ErrorBoundary>,
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("the inbox")).toBeTruthy();
    quiet.mockRestore();
  });
});
