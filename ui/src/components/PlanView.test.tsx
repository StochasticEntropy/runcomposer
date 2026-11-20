// @vitest-environment jsdom
import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { KeyedState } from "../lib/keyed.ts";
import type { PlanResult } from "../lib/types.ts";
import { renderWithI18n } from "../test/render.tsx";
import { PlanView } from "./PlanView.tsx";

afterEach(cleanup);

const answer = (result: Partial<PlanResult>): KeyedState<PlanResult> => ({
  key: "k",
  status: "ready",
  resultKey: "k",
  error: null,
  result: { runner: "demo", run_id: null, item_count: 4, parameters: {}, refused: false, refusal: null, plan: null, warnings: [], ...result },
});

describe("the plan view", () => {
  it("shows shards, groups, estimates and the runner's notes", () => {
    renderWithI18n(
      <PlanView
        onRetry={() => undefined}
        state={answer({
          plan: {
            text: "two lanes",
            shards: [
              { shard: "eu-01", group: "eu", count: 2, item_ids: ["Shop.Cart.Core.T001", "Shop.Cart.Core.T002"], estimate_ms: 1500 },
              { shard: "us-01", group: "us", count: 2, item_ids: ["Shop.Cart.Core.T003", "Shop.Cart.Core.T004"], estimate_ms: null },
            ],
            groups: [
              { group: "eu", shards: ["eu-01"], count: 2, estimate_ms: 1500 },
              { group: "us", shards: ["us-01"], count: 2, estimate_ms: null },
            ],
            notes: ["cold start: no duration history"],
            data: {},
          },
        })}
      />,
    );
    expect(screen.getByTestId("plan").getAttribute("data-refused")).toBe("false");
    expect(screen.getByText("4 items in 2 shards of 2 groups")).toBeTruthy();
    expect(screen.getByText("cold start: no duration history")).toBeTruthy();
    const shard = document.querySelector('[data-plan-shard="eu-01"]')!;
    expect(shard.textContent).toContain("Shop.Cart.Core.T002");
    expect([...shard.querySelectorAll("td")].map((cell) => cell.textContent)).toEqual(["eu", "2", "1.5 s"]);
    // No estimate is "–", never 0.
    expect(document.querySelector('[data-plan-group="us"]')!.textContent).toContain("–");
  });

  it("shows a refusal as a refusal", () => {
    renderWithI18n(<PlanView onRetry={() => undefined} state={answer({ refused: true, refusal: "working directory not found" })} />);
    expect(screen.getByTestId("plan").getAttribute("data-refused")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("The runner would refuse this run: working directory not found");
    expect(document.querySelector(".plan-shards")).toBeNull();
  });

  it("shows nothing of an answer that belongs to another question", () => {
    const stale = { ...answer({ refused: true, refusal: "old" }), key: "another question", status: "loading" as const };
    renderWithI18n(<PlanView onRetry={() => undefined} state={stale} />);
    expect(screen.queryByTestId("plan")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Asking the runner for its plan");
  });
});
