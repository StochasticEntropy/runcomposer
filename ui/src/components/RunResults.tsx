// A run's results: the roll-up per shard — each partition or chunk with its
// own outcome and its own log and report — and every verdict, failures
// first. Live status of shards that have not delivered yet is in the same
// table, marked as live.

import { useMemo, useState } from "react";

import { useI18n } from "../hooks/context.tsx";
import { useMediaQuery } from "../hooks/usePage.ts";
import { useVirtual } from "../hooks/useVirtual.ts";
import { formatDuration, naturalCompare, sumCounts } from "../lib/format.ts";
import type { Artifact, RunDetail, ShardSummary, VerdictRow } from "../lib/types.ts";
import { Dialog, ResultBadge, VerdictBadge, VerdictBar, VerdictCounts } from "./ui.tsx";

const STATUS_RANK: Record<string, number> = { FAIL: 0, ERROR: 1, MISSING: 2, SKIP: 4, PASS: 5 };
const rank = (row: VerdictRow) => STATUS_RANK[row.status] ?? 3;

export type StatusFilter = "all" | "failed" | "missing" | "skipped" | "passed";

const FILTERS: Record<StatusFilter, (row: VerdictRow) => boolean> = {
  all: () => true,
  failed: (row) => row.status === "FAIL" || row.status === "ERROR",
  missing: (row) => row.status === "MISSING",
  skipped: (row) => row.status === "SKIP",
  passed: (row) => row.status === "PASS",
};

export interface ResultRow extends VerdictRow {
  /** Unique within a dispatch: an item reported by two shards, or twice by one, is two rows. */
  key: string;
  /** A later attempt of the same item on the same shard exists. */
  superseded: boolean;
}

/** Failures first, then by item — and every row with a key of its own. */
export function resultRows(verdicts: VerdictRow[]): ResultRow[] {
  const latest = new Map<string, number>();
  for (const row of verdicts) {
    const slot = `${row.shard}\u0000${row.item_id}`;
    latest.set(slot, Math.max(latest.get(slot) ?? 0, row.attempt));
  }
  const seen = new Map<string, number>();
  const rows = verdicts.map((row) => {
    const base = `${row.shard}\u0000${row.item_id}\u0000${row.attempt}\u0000${row.live ? "live" : ""}`;
    const repeat = seen.get(base) ?? 0;
    seen.set(base, repeat + 1);
    return {
      ...row,
      key: repeat === 0 ? base : `${base}\u0000${repeat}`,
      superseded: row.attempt < (latest.get(`${row.shard}\u0000${row.item_id}`) ?? row.attempt),
    };
  });
  return rows.sort(
    (a, b) =>
      Number(a.superseded) - Number(b.superseded) ||
      rank(a) - rank(b) ||
      naturalCompare(a.item_id, b.item_id) ||
      naturalCompare(a.shard, b.shard) ||
      b.attempt - a.attempt,
  );
}

function ArtifactLinks({ artifacts }: { artifacts: Artifact[] }) {
  const { t } = useI18n();
  if (artifacts.length === 0) return null;
  return (
    <ul className="artifact-links">
      {artifacts.map((artifact, index) => (
        <li key={`${artifact.name}-${index}`}>
          {artifact.href ? (
            <a href={artifact.href} target="_blank" rel="noreferrer noopener">
              {artifact.name}
              <span className="sr-only"> ({t("run.opensNewTab")})</span>
            </a>
          ) : (
            <span title={t("run.artifactNoLink")}>
              {artifact.name}: <code>{artifact.url_or_path}</code>
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function ShardCard({ shard, artifacts, live, running }: { shard: ShardSummary; artifacts: Artifact[]; live: number; running: boolean }) {
  const { t } = useI18n();
  const reported = shard.delivered ? shard.count : live;
  const counts = shard.delivered ? shard.summary : {};
  return (
    <li className="shard-card" data-shard={shard.shard}>
      <div className="shard-head">
        <h3>{shard.shard || t("run.unnamedShard")}</h3>
        {shard.delivered ? (
          <ResultBadge result={shard.completion} />
        ) : (
          <span className={`badge badge-${running ? "running" : "neutral"}`}>{running ? t("run.shardRunning") : t("run.shardWaiting")}</span>
        )}
      </div>
      <VerdictBar counts={shard.delivered ? counts : live > 0 ? { RUNNING: live } : {}} planned={shard.planned ?? reported} active={!shard.delivered && running} />
      <p className="shard-numbers">
        {shard.planned !== null
          ? t("run.shardProgress", { done: reported, total: shard.planned })
          : t("run.shardReported", { count: reported })}
        {!shard.declared && <span className="muted"> · {t("run.shardUndeclared")}</span>}
      </p>
      <VerdictCounts counts={counts} />
      <ArtifactLinks artifacts={artifacts} />
    </li>
  );
}

export function RunResults({ run }: { run: RunDetail }) {
  const { t, tOr, locale } = useI18n();
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [shard, setShard] = useState("");
  const [search, setSearch] = useState("");
  const [opened, setOpened] = useState<ResultRow | null>(null);
  const narrow = useMediaQuery("(max-width: 700px)");
  const rowHeight = narrow ? 84 : 40;

  const dispatch = run.dispatches.find((entry) => entry.dispatch_id === run.dispatch_id) ?? null;
  const all = useMemo(() => resultRows(run.verdicts), [run.verdicts]);
  const artifacts = useMemo(
    () => run.artifacts.filter((artifact) => artifact.dispatch_id === null || artifact.dispatch_id === run.dispatch_id),
    [run.artifacts, run.dispatch_id],
  );
  const shardNames = run.shards.map((entry) => entry.shard);
  const multiShard = shardNames.length > 1;

  const counts = useMemo(() => {
    const scoped = shard ? all.filter((row) => row.shard === shard) : all;
    return Object.fromEntries(
      (Object.keys(FILTERS) as StatusFilter[]).map((name) => [name, scoped.filter(FILTERS[name]).length]),
    ) as Record<StatusFilter, number>;
  }, [all, shard]);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return all.filter(
      (row) =>
        FILTERS[filter](row) &&
        (!shard || row.shard === shard) &&
        (!needle || row.item_id.toLowerCase().includes(needle) || row.message.toLowerCase().includes(needle)),
    );
  }, [all, filter, shard, search]);
  const virtual = useVirtual<HTMLDivElement>(rows.length, rowHeight, 12, 37);

  const liveByShard = useMemo(() => {
    const live = new Map<string, number>();
    for (const row of run.verdicts) if (row.live) live.set(row.shard, (live.get(row.shard) ?? 0) + 1);
    return live;
  }, [run.verdicts]);
  const running = dispatch?.state === "RUNNING" || dispatch?.state === "DISPATCHED";
  const runLevel = artifacts.filter((artifact) => !artifact.shard || !shardNames.includes(artifact.shard));
  const links = Object.entries(dispatch?.links ?? {});

  if (!dispatch) return <p className="state state-empty">{t("run.neverDispatched")}</p>;

  return (
    <div className="results">
      {run.shards.length > 0 && (
        <section aria-labelledby="shards-title">
          <h2 id="shards-title">{t("run.shards", { count: run.shards.length })}</h2>
          <ul className="shard-grid">
            {run.shards.map((entry) => (
              <ShardCard
                key={entry.shard}
                shard={entry}
                artifacts={artifacts.filter((artifact) => artifact.shard === entry.shard)}
                live={liveByShard.get(entry.shard) ?? 0}
                running={running}
              />
            ))}
          </ul>
        </section>
      )}

      {(runLevel.length > 0 || links.length > 0) && (
        <section aria-labelledby="links-title" className="run-links">
          <h2 id="links-title">{t("run.links")}</h2>
          <ul className="artifact-links">
            {links.map(([name, href]) => (
              <li key={name}>
                {/^https?:\/\//.test(href) ? (
                  <a href={href} target="_blank" rel="noreferrer noopener">
                    {name}
                  </a>
                ) : (
                  <span>
                    {name}: <code>{href}</code>
                  </span>
                )}
              </li>
            ))}
          </ul>
          <ArtifactLinks artifacts={runLevel} />
        </section>
      )}

      <section aria-labelledby="verdicts-title">
        <h2 id="verdicts-title">{t("run.verdicts")}</h2>
        {all.length === 0 ? (
          <p className="state state-empty">{running ? t("run.noVerdictsYet") : t("run.noVerdicts")}</p>
        ) : (
          <>
            <div className="toolbar">
              <div className="chip-filter" role="group" aria-label={t("run.filterByStatus")}>
                {(Object.keys(FILTERS) as StatusFilter[]).map((name) => (
                  <button
                    key={name}
                    type="button"
                    className={`btn btn-small${filter === name ? " is-on" : ""}`}
                    aria-pressed={filter === name}
                    disabled={name !== "all" && counts[name] === 0}
                    onClick={() => setFilter(name)}
                  >
                    {t(`run.filter.${name}`)} <span className="count-pill">{counts[name]}</span>
                  </button>
                ))}
              </div>
              {multiShard && (
                <label className="field field-inline">
                  <span>{t("run.shard")}</span>
                  <select className="input" value={shard} onChange={(event) => setShard(event.target.value)}>
                    <option value="">{t("run.allShards")}</option>
                    {shardNames.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="field field-grow">
                <span className="sr-only">{t("run.searchVerdicts")}</span>
                <input
                  type="search"
                  className="input"
                  value={search}
                  placeholder={t("run.searchVerdicts")}
                  aria-label={t("run.searchVerdicts")}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
            </div>

            {rows.length === 0 ? (
              <p className="state state-empty">{t("run.noRowsMatch")}</p>
            ) : (
              <div
                className="table-scroll results-scroll"
                ref={virtual.containerRef}
                onScroll={virtual.onScroll}
                tabIndex={0}
                role="group"
                aria-label={t("run.verdicts")}
              >
                <table className="table verdict-table" aria-rowcount={rows.length + 1}>
                  <thead>
                    <tr aria-rowindex={1}>
                      <th scope="col">{t("run.column.status")}</th>
                      <th scope="col">{t("run.column.item")}</th>
                      <th scope="col" className="col-shard">
                        {t("run.column.shard")}
                      </th>
                      <th scope="col" className="num col-attempt">
                        {t("run.column.attempt")}
                      </th>
                      <th scope="col" className="num col-duration">
                        {t("run.column.duration")}
                      </th>
                      <th scope="col" className="col-message">
                        {t("run.column.message")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {virtual.padTop > 0 && (
                      <tr aria-hidden="true" className="spacer">
                        <td colSpan={6} style={{ height: virtual.padTop }} />
                      </tr>
                    )}
                    {rows.slice(virtual.start, virtual.end).map((row, offset) => (
                      <tr
                        key={row.key}
                        style={{ height: rowHeight }}
                        aria-rowindex={virtual.start + offset + 2}
                        className={row.superseded ? "is-superseded" : undefined}
                        data-status={row.status}
                        data-item-id={row.item_id}
                      >
                        <td className="col-status">
                          <VerdictBadge status={row.status} live={row.live} />
                          {row.live && <span className="live-dot" aria-hidden="true" title={t("run.live")} />}
                        </td>
                        <td className="col-item mono">{row.item_id}</td>
                        <td className="col-shard" data-label={t("run.column.shard")}>
                          {row.shard || "–"}
                        </td>
                        <td className="num col-attempt" data-label={t("run.column.attempt")}>
                          {row.attempt}
                          {row.superseded && <span className="sr-only"> ({t("run.superseded")})</span>}
                        </td>
                        <td className="num col-duration" data-label={t("run.column.duration")}>
                          {row.status === "MISSING" ? "–" : formatDuration(row.duration_ms, locale)}
                        </td>
                        <td className="col-message">
                          {row.message ? (
                            <button type="button" className="message-btn" onClick={() => setOpened(row)} title={t("run.showMessage")}>
                              {row.status === "MISSING" ? tOr(`run.reason.${row.message}`, row.message) : row.message}
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                    {virtual.padBottom > 0 && (
                      <tr aria-hidden="true" className="spacer">
                        <td colSpan={6} style={{ height: virtual.padBottom }} />
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      {opened && (
        <Dialog title={opened.item_id} onClose={() => setOpened(null)} size="large">
          <dl className="facts">
            <dt>{t("run.column.status")}</dt>
            <dd>
              <VerdictBadge status={opened.status} live={opened.live} />
            </dd>
            <dt>{t("run.column.shard")}</dt>
            <dd>{opened.shard || "–"}</dd>
            <dt>{t("run.column.attempt")}</dt>
            <dd>{opened.attempt}</dd>
            <dt>{t("run.column.duration")}</dt>
            <dd>{formatDuration(opened.duration_ms, locale)}</dd>
          </dl>
          <pre className="message-full">{opened.message}</pre>
          <ArtifactLinks artifacts={artifacts.filter((artifact) => artifact.shard === opened.shard)} />
        </Dialog>
      )}
    </div>
  );
}

/** The headline progress of a dispatch: what is in so far against what is owed. */
export function runProgress(run: RunDetail): { counts: Record<string, number>; done: number; planned: number } {
  const dispatch = run.dispatches.find((entry) => entry.dispatch_id === run.dispatch_id);
  const delivered = new Set(run.shards.filter((entry) => entry.delivered).map((entry) => entry.shard));
  const counts: Record<string, number> = {};
  for (const entry of run.shards) {
    if (!entry.delivered) continue;
    for (const [status, value] of Object.entries(entry.summary)) counts[status] = (counts[status] ?? 0) + value;
  }
  let live = 0;
  for (const row of run.verdicts) if (row.live && !delivered.has(row.shard)) live += 1;
  if (live > 0) counts.RUNNING = live;
  const done = sumCounts(counts);
  const planned = dispatch?.planned_verdicts ?? Math.max(run.planned_count, done);
  return { counts, done, planned: Math.max(planned, done) };
}
