// The page as the browser sees it: is it on screen, what time is it, and how
// wide is it.

import { useEffect, useState, useSyncExternalStore } from "react";

const subscribeVisibility = (notify: () => void) => {
  document.addEventListener("visibilitychange", notify);
  return () => document.removeEventListener("visibilitychange", notify);
};

/** False while the tab is in the background — nothing should poll then. */
export const usePageVisible = (): boolean =>
  useSyncExternalStore(subscribeVisibility, () => document.visibilityState !== "hidden");

/** A clock for relative times; stands still while the tab is hidden. */
export function useNow(intervalMs = 30_000): number {
  const visible = usePageVisible();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!visible) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [visible, intervalMs]);
  return now;
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    () => window.matchMedia(query).matches,
  );
}

/** Resolves when the page is (or becomes) visible; rejects when aborted. */
export function whenVisible(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("aborted", "AbortError"));
    if (document.visibilityState !== "hidden") return resolve();
    const done = () => {
      document.removeEventListener("visibilitychange", check);
      signal.removeEventListener("abort", abort);
    };
    const check = () => {
      if (document.visibilityState === "hidden") return;
      done();
      resolve();
    };
    const abort = () => {
      done();
      reject(new DOMException("aborted", "AbortError"));
    };
    document.addEventListener("visibilitychange", check);
    signal.addEventListener("abort", abort, { once: true });
  });
}

export function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("aborted", "AbortError"));
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    const abort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}
