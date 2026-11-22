// The names of a run's items. A run stores item ids; names belong to the
// catalog. So they are asked from the catalog as it is now, once per run —
// and a run page works without them: an id is always there, a name is what
// is shown in front of it when the catalog still has the item.

import { useEffect, useState } from "react";

import type { Api } from "../lib/api.ts";
import { MATCH_ALL } from "../lib/filter.ts";

export type ItemNames = ReadonlyMap<string, string>;

const NONE: ItemNames = new Map();

export async function loadItemNames(api: Pick<Api, "runItems" | "compile">, runId: string, signal?: AbortSignal): Promise<ItemNames> {
  const { item_ids: ids } = await api.runItems(runId, signal);
  if (ids.length === 0) return NONE;
  let items;
  try {
    items = (await api.compile({ item_ids: ids }, signal)).items;
  } catch {
    // An item of the run has left the catalog, so the list as a whole is
    // refused. The others still have their names: take them from the catalog.
    const wanted = new Set(ids);
    items = (await api.compile({ tag_filter: MATCH_ALL }, signal)).items.filter((item) => wanted.has(item.id));
  }
  return new Map(items.filter((item) => item.name && item.name !== item.id).map((item) => [item.id, item.name]));
}

export function useItemNames(api: Pick<Api, "runItems" | "compile">, runId: string | undefined): ItemNames {
  const [names, setNames] = useState<{ runId: string; names: ItemNames } | null>(null);
  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    loadItemNames(api, runId, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) setNames({ runId, names: loaded });
      })
      .catch(() => {
        // Names are an extra: without them the ids are shown, as they are anyway.
      });
    return () => controller.abort();
  }, [api, runId]);
  return names && names.runId === runId ? names.names : NONE;
}
