import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KeyedRequest, isCurrent } from "./keyed.ts";
import type { KeyedState } from "./keyed.ts";
import { ApiError } from "./request.ts";

describe("KeyedRequest", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function harness(debounceMs = 100) {
    const calls: { key: string; resolve: (value: string) => void; reject: (error: unknown) => void; signal: AbortSignal }[] = [];
    const states: KeyedState<string>[] = [];
    const keyed = new KeyedRequest<string>((state) => states.push(state), debounceMs);
    const ask = (key: string | null) =>
      keyed.request(key, key === null ? undefined : (signal) => new Promise<string>((resolve, reject) => calls.push({ key, resolve, reject, signal })));
    return { calls, states, keyed, ask };
  }

  it("never shows the answer to an earlier question, however late it arrives", async () => {
    const { calls, keyed, ask } = harness(0);
    ask("Payments|demo|seed=a");
    ask("Payments|demo|seed=b");
    expect(calls[0]!.signal.aborted).toBe(true);
    calls[1]!.resolve("plan for seed b");
    await vi.runAllTimersAsync();
    expect(keyed.current.result).toBe("plan for seed b");
    calls[0]!.resolve("plan for seed a");
    await vi.runAllTimersAsync();
    expect(keyed.current.result).toBe("plan for seed b");
    expect(isCurrent(keyed.current)).toBe(true);
  });

  it("drops the old answer the moment the question changes", async () => {
    const { calls, keyed, ask } = harness(0);
    ask("Payments|demo");
    calls[0]!.resolve("first");
    await vi.runAllTimersAsync();
    expect(isCurrent(keyed.current)).toBe(true);
    ask("Checkout|demo");
    expect(keyed.current).toMatchObject({ status: "loading", result: null });
    expect(isCurrent(keyed.current)).toBe(false);
  });

  it("debounces a burst of changes into one request and goes idle without a question", async () => {
    const { calls, keyed, ask } = harness(100);
    for (const key of ["a", "ab", "abc"]) {
      ask(key);
      await vi.advanceTimersByTimeAsync(30);
    }
    await vi.advanceTimersByTimeAsync(100);
    expect(calls.map((call) => call.key)).toEqual(["abc"]);
    ask(null);
    expect(keyed.current).toMatchObject({ status: "idle", key: "", result: null });
    expect(calls[0]!.signal.aborted).toBe(true);
  });

  it("reports the failure of the current question and can ask again", async () => {
    const { calls, keyed, ask } = harness(0);
    ask("Payments|ghost");
    calls[0]!.reject(new ApiError(400, "unknown-runner", "no runner is configured under the id 'ghost'"));
    await vi.runAllTimersAsync();
    expect(keyed.current).toMatchObject({ status: "error", result: null });
    expect(keyed.current.error?.code).toBe("unknown-runner");
    keyed.retry();
    calls[1]!.resolve("plan");
    await vi.runAllTimersAsync();
    expect(isCurrent(keyed.current)).toBe(true);
  });

  it("asks once for the same question, and again after dispose", async () => {
    const { calls, keyed, ask } = harness(0);
    ask("Payments|demo");
    ask("Payments|demo");
    expect(calls).toHaveLength(1);
    keyed.dispose();
    ask("Payments|demo");
    expect(calls).toHaveLength(2);
  });
});
