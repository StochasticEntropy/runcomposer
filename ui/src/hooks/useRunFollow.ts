// Following one run: load it, and while it is in flight keep it current.
//
// One loop per run page, and only one request of it at a time:
//
//   load the run → still in flight? → wait on the run's event stream (a long
//   poll that returns the moment something happens, or after a short while so
//   live per-item status keeps moving) → load again → …
//
// The loop belongs to (run id, dispatch): leaving the page or opening another
// run aborts it, so an answer for a run that is no longer on screen has
// nowhere to land. It stands still while the tab is hidden and looks again
// the moment it is shown. A run that is over is not polled; `refresh` looks
// once more.

import { useCallback, useEffect, useState } from "react";

import type { Api } from "../lib/api.ts";
import { asApiError, isAbort } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { isActive } from "../lib/types.ts";
import type { RunDetail } from "../lib/types.ts";
import { sleep, whenVisible } from "./usePage.ts";

/** Seconds a look at the event stream may be held open. */
const WAIT_RUNNING_S = 2;
const WAIT_AWAITING_S = 20;

export interface RunFollow {
  run: RunDetail | undefined;
  error: ApiError | null;
  /** True until the first answer for this run arrived. */
  loading: boolean;
  refresh: () => void;
}

export function useRunFollow(api: Api, runId: string, dispatchId: string | null): RunFollow {
  const key = `${runId}|${dispatchId ?? ""}`;
  const [state, setState] = useState<{ key: string; run: RunDetail | undefined; error: ApiError | null }>({
    key,
    run: undefined,
    error: null,
  });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void (async () => {
      let failures = 0;
      while (!signal.aborted) {
        let run: RunDetail;
        try {
          await whenVisible(signal);
          run = await api.run(runId, dispatchId, signal);
          if (signal.aborted) return;
          failures = 0;
          setState({ key, run, error: null });
        } catch (caught) {
          if (isAbort(caught) || signal.aborted) return;
          const error = asApiError(caught);
          setState((current) => ({ key, run: current.key === key ? current.run : undefined, error }));
          if (error.status === 404) return; // it is gone; asking again will not bring it back
          failures += 1;
          try {
            await sleep(Math.min(30_000, 2000 * failures), signal);
          } catch {
            return;
          }
          continue;
        }
        if (!isActive(run.state)) return;
        try {
          const wait = run.state === "AWAITING_RESULTS" ? WAIT_AWAITING_S : WAIT_RUNNING_S;
          await api.events(runId, run.last_event_seq, wait, signal);
        } catch (caught) {
          if (isAbort(caught) || signal.aborted) return;
          try {
            await sleep(2000, signal);
          } catch {
            return;
          }
        }
      }
    })();
    return () => controller.abort();
  }, [api, runId, dispatchId, key, generation]);

  const refresh = useCallback(() => setGeneration((value) => value + 1), []);
  const current = state.key === key ? state : { key, run: undefined, error: null };
  return { run: current.run, error: current.error, loading: current.run === undefined && current.error === null, refresh };
}
