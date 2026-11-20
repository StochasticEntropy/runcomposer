// A piece of view state that should still be there after a trip to another
// view or a reload — a sort order, a toggle. Kept in session storage.

import { useCallback, useState } from "react";

import { readSession, writeSession } from "../lib/prefs.ts";

export function usePersisted<T>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => readSession<T>(key) ?? initial);
  const set = useCallback(
    (next: T) => {
      setValue(next);
      writeSession(key, next);
    },
    [key],
  );
  return [value, set];
}
