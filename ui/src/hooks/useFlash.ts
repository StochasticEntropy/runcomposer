// The message a view was handed on arrival ("run started", "run deleted"),
// and messages it posts itself. A confirmation that something worked goes
// away on its own after a while; a warning stays until it is dismissed.

import { useCallback, useEffect, useState } from "react";

import { takeFlash } from "../lib/flash.ts";
import type { Flash } from "../lib/flash.ts";

const OK_VISIBLE_MS = 8000;

export function useFlash(): { flash: Flash | null; show: (flash: Flash) => void; dismiss: () => void } {
  const [flash, setFlash] = useState<Flash | null>(() => takeFlash());
  useEffect(() => {
    if (flash?.tone !== "ok") return;
    const timer = window.setTimeout(() => setFlash(null), OK_VISIBLE_MS);
    return () => window.clearTimeout(timer);
  }, [flash]);
  const dismiss = useCallback(() => setFlash(null), []);
  return { flash, show: setFlash, dismiss };
}
