// A run's results: the roll-up per shard — each partition or chunk with its
// own outcome and its own log and report — and every verdict, failures
// first. Live status of shards that have not delivered yet is in the same
// table, marked as live.

import { useCallback, useMemo, useRef, useState } from "react";

import { useI18n } from "../hooks/context.tsx";
import { useMediaQuery } from "../hooks/usePage.ts";
import { useVirtual } from "../hooks/useVirtual.ts";
import { formatDuration, naturalCompare, sumCounts } from "../lib/format.ts";
import { isActive } from "../lib/types.ts";
import type { Artifact, GroupSummary, RunDetail, ShardSummary, VerdictRow } from "../lib/types.ts";
import { Dialog, ResultBadge, VerdictBadge, VerdictBar, VerdictCounts } from "./ui.tsx";

const STATUS_RANK: Record<string, number> = { FAIL: 0, ERROR: 1, MISSING: 2, RUNNING: 3, SKIP: 5, PASS: 6 };
const rank = (row: VerdictRow) => STATUS_RANK[row.status] ?? 4;

export type StatusFilter = "all" | "failed" | "missing" | "running" | "skipped" | "passed";

const FILTERS: Record<StatusFilter, (row: VerdictRow) => boolean> = {
  all: () => true,
  failed: (row) => row.status === "FAIL" || row.status === "ERROR",
  missing: (row) => row.status === "MISSING",
  running: (row) => row.status === "RUNNING",
  skipped: (row) => row.status === "SKIP",
  passed: (row) => row.status === "PASS",
};

export interface ResultRow extends VerdictRow {
  /** Unique within a dispatch: an item reported by two shards, or twice by one, is two rows. */
  key: string;
  /** A later attempt of the same item on the same shard exists. */
  superseded: boolean;
  /** On a row without a delivered result: what the item reported live before the dispatch ended. */
  seenLive: string | null;
}

const TROUBLE = ["FAIL", "ERROR", "MISSING"];
/** A list of more shards than this folds the ones without a failure into one line. */
const FOLD_SHARDS_ABOVE = 8;

/** Where the eye should go first: 0 something did not pass (or never came), 1 still at work, 2 all well. */
export function attention(part: { summary: Record<string, number>; done: boolean }, over: boolean): 0 | 1 | 2 {
  if (TROUBLE.some((status) => (part.summary[status] ?? 0) > 0)) return 0;
  if (!part.done) return over ? 0 : 1;
  return 2;
}

/** Failures first, then what is still running, then the rest — each in natural order of its name. */
export function byAttention<T>(parts: T[], rankOf: (part: T) => number, nameOf: (part: T) => string): T[] {
  return [...parts].sort((a, b) => rankOf(a) - rankOf(b) || naturalCompare(nameOf(a), nameOf(b)));
}

/** The files to offer next to a result: its shard's log and report, by their short names. */
export function rowFiles(artifacts: Artifact[], shard: string): { label: string; href: string }[] {
  const linked = artifacts.filter((artifact) => artifact.shard === shard && artifact.href);
  const readable = linked.filter((artifact) => artifact.media_type === "text/html");
  return (readable.length > 0 ? readable : linked).slice(0, 2).map((artifact) => ({
    // "log.html (eu-01)" → "log": the row already says which shard it is.
    label: artifact.name.replace(/\s*\(.*\)\s*$/, "").replace(/\.html?$/i, ""),
    href: artifact.href!,
  }));
}

/** Counts without RUNNING: an item that is running has no result yet. */
export function reportedOnly(counts: Record<string, number>): Record<string, number> {
  const { RUNNING: _running, ...reported } = counts;
  void _running;
  return reported;
}

/** Failures first, then by item — and every row with a key of its own.
 *
 * `over` says the dispatch has ended. An item whose shard never delivered
 * then counts as MISSING, whatever it reported live — live status is not a
 * result — and the server puts what it last reported on the MISSING row
 * (`last_live`). The live rows themselves are dropped: one row per item, the
 * verdict that counts, with what was seen as a note. */
export function resultRows(all: VerdictRow[], over = false): ResultRow[] {
  const verdicts = (over ? all.filter((row) => !row.live) : all).map((row) => ({ ...row, seenLive: row.last_live?.status ?? null }));
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
          ) : artifact.kind === "directory" ? (
            // A recorded directory makes its files servable; it is not itself a page to open.
            <span className="muted" data-artifact-kind="directory" title={t("run.artifactDirectory")}>
              {artifact.name}
            </span>
          ) : (
            // Not served: the server gives a path relative to the artifact
            // directory, or for a file outside it the bare file name — never
            // where it lies on the server's machine.
            <span title={t("run.artifactNoLink")} data-artifact-kind={artifact.kind}>
              {artifact.name}
              {artifact.url_or_path && artifact.url_or_path !== artifact.name && (
                <>
                  : <code>{artifact.url_or_path}</code>
                </>
              )}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function ShardCard({
  shard,
  artifacts,
  live,
  running,
  over,
}: {
  shard: ShardSummary;
  artifacts: Artifact[];
  /** Counts by status of what the shard has reported live so far. */
  live: Record<string, number>;
  running: boolean;
  /** The dispatch has ended: a shard that did not deliver never will. */
  over: boolean;
}) {
  const { t } = useI18n();
  // Live status counts while the shard is still expected to deliver, not after.
  const counts = shard.delivered ? shard.summary : over ? {} : reportedOnly(live);
  const reported = shard.delivered ? shard.count : sumCounts(counts);
  const nowRunning = shard.delivered || over ? 0 : (live.RUNNING ?? 0);
  return (
    <li className="shard-card" data-shard={shard.shard}>
      <div className="shard-head">
        <h3>{shard.shard || t("run.unnamedShard")}</h3>
        {shard.delivered ? (
          <ResultBadge result={shard.completion} />
        ) : (
          <span className={`badge badge-${running ? "running" : over ? "warn" : "neutral"}`}>
            {running ? t("run.shardRunning") : over ? t("run.shardNothing") : t("run.shardWaiting")}
          </span>
        )}
      </div>
      <VerdictBar counts={counts} planned={shard.planned ?? reported} active={!shard.delivered && running} />
      <p className="shard-numbers">
        {shard.planned !== null
          ? t("run.shardProgress", { done: reported, total: shard.planned })
          : t("run.shardReported", { count: reported })}
        {nowRunning > 0 && <span className="count-running"> · {t("run.nowRunning", { count: nowRunning })}</span>}
        {!shard.declared && <span className="muted"> · {t("run.shardUndeclared")}</span>}
      </p>
      <VerdictCounts counts={counts} />
      <ArtifactLinks artifacts={artifacts} />
    </li>
  );
}

export interface ResultsQuery {
  status?: string;
  group?: string;
  shard?: string;
  q?: string;
}

interface Props {
  run: RunDetail;
  /** Item id → the name the catalog has for it now; ids without one are shown as ids. */
  names?: ReadonlyMap<string, string>;
  /** Which results are shown — kept by the caller in the URL, so a reload or a shared link shows the same rows. */
  query: ResultsQuery;
  onQuery: (changes: ResultsQuery) => void;
}

export function RunResults({ run, names, query, onQuery }: Props) {
  const { t, tOr, locale } = useI18n();
  const filter: StatusFilter = query.status && query.status in FILTERS ? (query.status as StatusFilter) : "all";
  const shard = query.shard ?? "";
  const group = query.group ?? "";
  const search = query.q ?? "";
  const setFilter = (status: StatusFilter) => onQuery({ status: status === "all" ? "" : status });
  const setShard = (name: string) => onQuery({ shard: name });
  const setGroup = (name: string) => onQuery({ group: name, shard: "" });
  const setSearch = (q: string) => onQuery({ q });
  const [opened, setOpened] = useState<ResultRow | null>(null);
  const verdictsTitle = useRef<HTMLHeadingElement | null>(null);
  const narrow = useMediaQuery("(max-width: 700px)");
  const rowHeight = narrow ? 84 : 44;

  const dispatch = run.dispatches.find((entry) => entry.dispatch_id === run.dispatch_id) ?? null;
  const over = dispatch !== null && !isActive(dispatch.state);
  const all = useMemo(() => resultRows(run.verdicts, over), [run.verdicts, over]);
  const artifacts = useMemo(
    () => run.artifacts.filter((artifact) => artifact.dispatch_id === null || artifact.dispatch_id === run.dispatch_id),
    [run.artifacts, run.dispatch_id],
  );
  // Items nobody reported, of a dispatch whose shards never said what they
  // would run, come back under no shard at all. That is not a shard to show.
  const shards = run.shards.filter(
    (entry) => entry.declared || entry.shard !== "" || Object.keys(entry.summary).some((status) => status !== "MISSING"),
  );
  // Groups: the shards a runner declared as belonging together (a partition, a lane).
  const groups = useMemo(() => run.groups ?? [], [run.groups]);
  const groupOf = useMemo(() => run.shard_groups ?? {}, [run.shard_groups]);
  const inGroup = useMemo(() => (name: string) => !group || groupOf[name] === group, [group, groupOf]);
  const shardNames = shards.map((entry) => entry.shard).filter(inGroup);
  const multiShard = shards.length > 1;

  const counts = useMemo(() => {
    const scoped = all.filter((row) => (!shard || row.shard === shard) && inGroup(row.shard));
    return Object.fromEntries(
      (Object.keys(FILTERS) as StatusFilter[]).map((name) => [name, scoped.filter(FILTERS[name]).length]),
    ) as Record<StatusFilter, number>;
  }, [all, shard, inGroup]);

  /** A row's message in the viewer's words where the server sent a code for it. */
  const messageOf = useCallback(
    (row: ResultRow): string => {
      const text = row.status === "MISSING" ? tOr(`run.reason.${row.message}`, row.message) : row.message;
      if (!row.seenLive) return text;
      // What the item was doing when the dispatch stopped.
      if (row.seenLive === "RUNNING") return `${text} — ${t("run.lastLiveRunning")}`;
      return `${text} — ${t("run.seenLive", { status: tOr(`verdict.${row.seenLive}`, row.seenLive) })}`;
    },
    [t, tOr],
  );

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return all.filter(
      (row) =>
        FILTERS[filter](row) &&
        (!shard || row.shard === shard) &&
        inGroup(row.shard) &&
        // The search reads what the row shows, so a message can be found as it is displayed.
        // …and an item can be found by its name as well as by its id.
        (!needle ||
          row.item_id.toLowerCase().includes(needle) ||
          (names?.get(row.item_id) ?? "").toLowerCase().includes(needle) ||
          messageOf(row).toLowerCase().includes(needle)),
    );
  }, [all, filter, shard, search, inGroup, messageOf, names]);
  const virtual = useVirtual<HTMLDivElement>(rows.length, rowHeight, 12, 37);

  // What undelivered shards report live — the server's own roll-up.
  const liveByShard = run.live_summary ?? {};
  const running = dispatch?.state === "RUNNING" || dispatch?.state === "DISPATCHED";
  const allShardNames = shards.map((entry) => entry.shard);
  const groupNames = groups.map((entry) => entry.group);
  // Files of a group as a whole, and files of the run as a whole.
  const groupLevel = (name: string) => artifacts.filter((artifact) => !artifact.shard && artifact.group === name);
  const runLevel = artifacts.filter(
    (artifact) => (!artifact.shard || !allShardNames.includes(artifact.shard)) && !(artifact.group && groupNames.includes(artifact.group)),
  );
  const shardCard = (entry: ShardSummary) => (
    <ShardCard
      key={entry.shard}
      shard={entry}
      artifacts={artifacts.filter((artifact) => artifact.shard === entry.shard)}
      live={liveByShard[entry.shard] ?? {}}
      running={running}
      over={over}
    />
  );
  // Failures first: the shards and groups that need a look are at the top,
  // and what passed completely folds away while something else did not.
  const shardRank = (entry: ShardSummary) => attention({ summary: entry.summary, done: entry.delivered }, over);
  const groupRank = (entry: GroupSummary) =>
    attention({ summary: entry.summary, done: entry.declared_shards > 0 && entry.delivered_shards >= entry.declared_shards }, over);
  const orderedGroups = byAttention(groups, groupRank, (entry) => entry.group);
  const ungrouped = byAttention(
    shards.filter((entry) => !(entry.shard in groupOf)),
    shardRank,
    (entry) => entry.shard,
  );
  const anyTrouble = shards.some((entry) => shardRank(entry) === 0);
  // Many shards: the ones without a failure share one folded line, so fifty
  // shards with one failure are one card and one line — not fifty cards.
  const shardList = (list: ShardSummary[]) => {
    const fold = list.length > FOLD_SHARDS_ABOVE;
    const needLook = fold ? list.filter((entry) => shardRank(entry) < 2) : list;
    const passed = fold ? list.filter((entry) => shardRank(entry) === 2) : [];
    // …and of forty shards that failed, the first few are cards; the rest are one more line.
    const shown = needLook.slice(0, FOLD_SHARDS_ABOVE);
    const further = needLook.slice(FOLD_SHARDS_ABOVE);
    return (
      <>
        {shown.length > 0 && <ul className="shard-grid">{shown.map(shardCard)}</ul>}
        {further.length > 0 && (
          <details className="group-shards more-shards">
            <summary>{t("run.moreShards", { count: further.length })}</summary>
            <ul className="shard-grid">{further.map(shardCard)}</ul>
          </details>
        )}
        {passed.length > 0 && (
          <details className="group-shards passed-shards">
            <summary>{t("run.passedShards", { count: passed.length })}</summary>
            <ul className="shard-grid">{passed.map(shardCard)}</ul>
          </details>
        )}
      </>
    );
  };
  const filesByShard = useMemo(() => {
    const byShard = new Map<string, { label: string; href: string }[]>();
    for (const name of new Set(artifacts.map((artifact) => artifact.shard ?? ""))) if (name) byShard.set(name, rowFiles(artifacts, name));
    return byShard;
  }, [artifacts]);
  const links = Object.entries(dispatch?.links ?? {});

  if (!dispatch) return <p className="state state-empty">{t("run.neverDispatched")}</p>;

  // With many shards the list of results per item — the messages, the logs —
  // is far down the page. What did not pass is one step away from the top.
  // (Counted over the whole run: the jump also takes a shard or group choice away.)
  const failedInAll = all.filter(FILTERS.failed).length;
  const missingInAll = all.filter(FILTERS.missing).length;
  const jump = failedInAll > 0 ? ("failed" as const) : missingInAll > 0 ? ("missing" as const) : null;
  const toResults = (status: StatusFilter) => {
    onQuery({ status, shard: "", group: "" });
    const target = verdictsTitle.current;
    target?.scrollIntoView?.({ block: "start" });
    target?.focus({ preventScroll: true });
  };

  return (
    <div className="results">
      {jump && shards.length > 1 && (
        <p className="results-jump">
          <button type="button" className="btn btn-small" data-testid="jump-to-results" onClick={() => toResults(jump)}>
            {jump === "failed" ? t("run.jumpFailed", { count: failedInAll }) : t("run.jumpMissing", { count: missingInAll })}
          </button>
        </p>
      )}
      {groups.length > 0 && (
        <section aria-labelledby="groups-title">
          <h2 id="groups-title">{t("run.groups", { count: groups.length })}</h2>
          <ul className="group-list">
            {orderedGroups.map((entry) => {
              const live = over ? {} : reportedOnly(entry.live);
              const merged = { ...entry.summary };
              for (const [status, value] of Object.entries(live)) merged[status] = (merged[status] ?? 0) + value;
              const nowRunning = over ? 0 : (entry.live.RUNNING ?? 0);
              // Reported: what its shards delivered (the server's `count`, which
              // holds no MISSING) plus, while they are at work, what they said live —
              // the same sum its shard cards show one by one.
              const reported = entry.count + sumCounts(live);
              const members = byAttention(
                shards.filter((candidate) => groupOf[candidate.shard] === entry.group),
                shardRank,
                (candidate) => candidate.shard,
              );
              // Open when it needs a look; a group that passed stays folded while another did not.
              const open = anyTrouble ? groupRank(entry) < 2 : members.length <= FOLD_SHARDS_ABOVE;
              const complete = entry.delivered_shards >= entry.declared_shards && entry.declared_shards > 0;
              return (
                <li key={entry.group} className="group-card" data-group={entry.group} data-attention={groupRank(entry)}>
                  <div className="shard-head">
                    <h3>{entry.group}</h3>
                    {complete ? (
                      <ResultBadge result={entry.completion} />
                    ) : (
                      <span className={`badge badge-${running ? "running" : over ? "warn" : "neutral"}`}>
                        {running ? t("run.shardRunning") : over ? t("run.groupIncomplete") : t("run.shardWaiting")}
                      </span>
                    )}
                  </div>
                  <VerdictBar counts={merged} planned={entry.planned ?? sumCounts(merged)} active={!complete && running} />
                  <p className="shard-numbers">
                    {t("run.groupShards", { done: entry.delivered_shards, total: entry.declared_shards })}
                    {entry.planned !== null && (
                      <span data-testid="group-reported">
                        {" · "}
                        {t("run.shardProgress", { done: reported, total: entry.planned })}
                      </span>
                    )}
                    {nowRunning > 0 && <span className="count-running"> · {t("run.nowRunning", { count: nowRunning })}</span>}
                  </p>
                  <VerdictCounts counts={merged} />
                  <ArtifactLinks artifacts={groupLevel(entry.group)} />
                  <details className="group-shards" open={open}>
                    <summary>{t("run.shards", { count: members.length })}</summary>
                    {shardList(members)}
                  </details>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {ungrouped.length > 0 && (
        <section aria-labelledby="shards-title">
          <h2 id="shards-title">{t("run.shards", { count: ungrouped.length })}</h2>
          {shardList(ungrouped)}
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
        <h2 id="verdicts-title" ref={verdictsTitle} tabIndex={-1}>
          {t("run.verdicts")}
        </h2>
        {all.length === 0 ? (
          <p className="state state-empty">{running ? t("run.noVerdictsYet") : t("run.noVerdicts")}</p>
        ) : (
          <>
            <div className="toolbar">
              <div className="chip-filter" role="group" aria-label={t("run.filterByStatus")}>
                {(Object.keys(FILTERS) as StatusFilter[])
                  // "Running" is offered while something runs — it is not a result a finished run has.
                  .filter((name) => name !== "running" || counts.running > 0 || filter === "running")
                  .map((name) => (
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
              {groups.length > 1 && (
                <label className="field field-inline">
                  <span>{t("run.group")}</span>
                  <select className="input" value={group} onChange={(event) => setGroup(event.target.value)}>
                    <option value="">{t("run.allGroups")}</option>
                    {groups.map((entry) => (
                      <option key={entry.group} value={entry.group}>
                        {entry.group}
                      </option>
                    ))}
                  </select>
                </label>
              )}
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
                      <th scope="col" className="col-status">
                        {t("run.column.status")}
                      </th>
                      <th scope="col" className="col-item">
                        {t("run.column.item")}
                      </th>
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
                      <th scope="col" className="col-files">
                        {t("run.column.files")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {virtual.padTop > 0 && (
                      <tr aria-hidden="true" className="spacer">
                        <td colSpan={7} style={{ height: virtual.padTop }} />
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
                        <td className="col-item">
                          <button type="button" className="item-btn" onClick={() => setOpened(row)} title={t("run.showDetails", { item: row.item_id })}>
                            {names?.has(row.item_id) ? (
                              // The name people know the item by, as in the preview — and the id it is stored under.
                              <>
                                <span className="item-name">{names.get(row.item_id)}</span>
                                <span className="item-id mono">{row.item_id}</span>
                              </>
                            ) : (
                              <span className="clamp mono">{row.item_id}</span>
                            )}
                          </button>
                        </td>
                        <td className="col-shard" data-label={t("run.column.shard")}>
                          {row.shard || "–"}
                        </td>
                        <td className="num col-attempt" data-label={t("run.column.attempt")}>
                          {row.attempt}
                          {row.superseded && <span className="sr-only"> ({t("run.superseded")})</span>}
                        </td>
                        <td className="num col-duration" data-label={t("run.column.duration")}>
                          {row.status === "MISSING" || row.status === "RUNNING" ? "–" : formatDuration(row.duration_ms, locale)}
                        </td>
                        <td className="col-message">
                          {row.message || row.seenLive ? (
                            <button type="button" className="message-btn" onClick={() => setOpened(row)} title={t("run.showMessage")}>
                              {messageOf(row)}
                            </button>
                          ) : null}
                        </td>
                        <td className="col-files">
                          {(filesByShard.get(row.shard) ?? []).map((file) => (
                            <a key={file.href} href={file.href} target="_blank" rel="noreferrer noopener" aria-label={t("run.fileOf", { file: file.label, item: row.item_id })}>
                              {file.label}
                            </a>
                          ))}
                        </td>
                      </tr>
                    ))}
                    {virtual.padBottom > 0 && (
                      <tr aria-hidden="true" className="spacer">
                        <td colSpan={7} style={{ height: virtual.padBottom }} />
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
        <Dialog title={names?.get(opened.item_id) ?? opened.item_id} onClose={() => setOpened(null)} size="large">
          <dl className="facts">
            {names?.has(opened.item_id) && (
              <>
                <dt>{t("preview.column.id")}</dt>
                <dd className="mono">{opened.item_id}</dd>
              </>
            )}
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
          {(opened.message || opened.seenLive) && <pre className="message-full">{messageOf(opened)}</pre>}
          <ArtifactLinks artifacts={artifacts.filter((artifact) => artifact.shard === opened.shard)} />
        </Dialog>
      )}
    </div>
  );
}

/** The headline progress of a dispatch: what is in so far against what is owed.
 * While it runs, a shard that has not delivered counts with what it reported
 * live. Once it is over, only what was delivered counts — the rest is missing. */
export function runProgress(run: RunDetail): { counts: Record<string, number>; done: number; planned: number; running: number } {
  const dispatch = run.dispatches.find((entry) => entry.dispatch_id === run.dispatch_id);
  const counts: Record<string, number> = {};
  if (dispatch && !isActive(dispatch.state)) {
    Object.assign(counts, dispatch.summary);
    const total = sumCounts(counts);
    const planned = Math.max(dispatch.planned_verdicts ?? run.planned_count, total);
    return { counts, done: total - (counts.MISSING ?? 0), planned, running: 0 };
  }
  for (const entry of run.shards) {
    if (!entry.delivered) continue;
    for (const [status, value] of Object.entries(entry.summary)) counts[status] = (counts[status] ?? 0) + value;
  }
  // Shards that have not delivered count with what they reported live —
  // except what is RUNNING: that is work in progress, never a result.
  let running = 0;
  for (const live of Object.values(run.live_summary ?? {})) {
    for (const [status, value] of Object.entries(live)) {
      if (status === "RUNNING") running += value;
      else counts[status] = (counts[status] ?? 0) + value;
    }
  }
  const done = sumCounts(counts);
  const planned = dispatch?.planned_verdicts ?? Math.max(run.planned_count, done + running);
  return { counts, done, planned: Math.max(planned, done + running), running };
}
