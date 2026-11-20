import { useSyncExternalStore } from "react";

import { parseHash } from "../lib/router.ts";
import type { Route } from "../lib/router.ts";

const subscribe = (notify: () => void) => {
  window.addEventListener("hashchange", notify);
  return () => window.removeEventListener("hashchange", notify);
};
const snapshot = () => window.location.hash;

let cachedHash: string | null = null;
let cachedRoute: Route | null = null;

/** The current route. Re-renders on every navigation, including back/forward. */
export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, snapshot);
  if (hash !== cachedHash || cachedRoute === null) {
    cachedHash = hash;
    cachedRoute = parseHash(hash);
  }
  return cachedRoute;
}

/** Go somewhere. `replace` rewrites the current history entry — for state
 * that follows typing, where every keystroke must not become a Back step. */
export function navigate(hash: string, options: { replace?: boolean } = {}): void {
  if (window.location.hash === hash) return;
  if (options.replace) {
    window.history.replaceState(window.history.state, "", hash);
    // replaceState fires no hashchange; subscribers are told by hand.
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = hash;
  }
}
