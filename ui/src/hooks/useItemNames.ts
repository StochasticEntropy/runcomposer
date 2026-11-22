// The names of a run's items, as the run was composed: the server keeps
// them with the run and serves them with its items (`/runs/{id}/items`), so
// a renamed or removed catalog item still shows the name the run had. A run
// page works without them: an id is always there, and a name is what is
// shown in front of it when it differs.

import { useEffect, useState } from "react";

import type { Api } from "../lib/api.ts";

export type ItemNames = ReadonlyMap<string, string>;

const NONE: ItemNames = new Map();

export async function loadItemNames(api: Pick<Api, "runItems">, runId: string, signal?: AbortSignal): Promise<ItemNames> {
  const { items } = await api.runItems(runId, signal);
  return new Map((items ?? []).filter((item) => item.name && item.name !== item.id).map((item) => [item.id, item.name]));
}

export function useItemNames(api: Pick<Api, "runItems">, runId: string | undefined): ItemNames {
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
