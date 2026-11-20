// A plan preview for whatever is on screen: the answer belongs to a key made
// of everything the plan depends on, and only the answer to the current key
// is ever shown (lib/keyed.ts).

import { useEffect, useRef, useState } from "react";

import { KeyedRequest, idleKeyed } from "../lib/keyed.ts";
import type { KeyedState } from "../lib/keyed.ts";
import type { PlanResult } from "../lib/types.ts";

export interface PlanQuestion {
  /** Everything the plan depends on, serialized. */
  key: string;
  ask: (signal: AbortSignal) => Promise<PlanResult>;
}

export function usePlan(question: PlanQuestion | null, debounceMs = 250): { state: KeyedState<PlanResult>; retry: () => void } {
  const [state, setState] = useState<KeyedState<PlanResult>>(idleKeyed<PlanResult>());
  const [keyed] = useState(() => new KeyedRequest<PlanResult>(setState, debounceMs));
  const ask = useRef(question?.ask);
  useEffect(() => {
    ask.current = question?.ask;
  });
  const key = question?.key ?? null;
  useEffect(() => {
    // The task is read when the request is made — it belongs to this key.
    keyed.request(key, key === null ? undefined : (signal) => ask.current!(signal));
  }, [keyed, key]);
  useEffect(() => () => keyed.dispose(), [keyed]);
  // The state of a previous question is never returned for the current one.
  const current = state.key === (key ?? "") ? state : { ...idleKeyed<PlanResult>(), key: key ?? "", status: key === null ? ("idle" as const) : ("loading" as const) };
  return { state: current, retry: () => keyed.retry() };
}
