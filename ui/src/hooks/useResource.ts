// Loading something from the server, as a view needs it: the data, whether a
// request is under way, the error of the last attempt, and a way to ask
// again. A reload keeps showing what was there (no flash back to a spinner);
// an answer that arrives after a newer request was started is dropped.

import { useCallback, useEffect, useRef, useState } from "react";

import { Latest, asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";

export interface Resource<T> {
  data: T | undefined;
  /** True while a request is under way — the first one or a reload. */
  loading: boolean;
  error: ApiError | null;
  reload: () => Promise<void>;
  setData: (value: T) => void;
}

export function useResource<T>(load: (signal: AbortSignal) => Promise<T>, key: string): Resource<T> {
  const [state, setState] = useState<{ key: string; data: T | undefined; loading: boolean; error: ApiError | null }>({
    key,
    data: undefined,
    loading: true,
    error: null,
  });
  const latest = useRef(new Latest());
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  const run = useCallback(async (forKey: string) => {
    setState((current) =>
      current.key === forKey
        ? { ...current, loading: true }
        : { key: forKey, data: undefined, loading: true, error: null },
    );
    try {
      const outcome = await latest.current.run((signal) => loadRef.current(signal));
      if (!outcome.current) return;
      setState({ key: forKey, data: outcome.value, loading: false, error: null });
    } catch (error) {
      setState((current) => (current.key === forKey ? { ...current, loading: false, error: asApiError(error) } : current));
    }
  }, []);

  useEffect(() => {
    const controller = latest.current;
    void run(key);
    return () => controller.cancel();
  }, [key, run]);

  const reload = useCallback(() => run(key), [key, run]);
  const setData = useCallback((value: T) => setState((current) => ({ ...current, data: value, error: null })), []);
  // A key change is visible one render before the effect runs: never show the
  // previous key's data under the new key.
  const current = state.key === key ? state : { key, data: undefined, loading: true, error: null };
  return { data: current.data, loading: current.loading, error: current.error, reload, setData };
}
