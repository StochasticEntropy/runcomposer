// Display formatting that does not depend on React: durations, times, order.

/** 950 → "950 ms", 12 400 → "12 s", 75 000 → "1 min 15 s", 3 780 000 → "1 h 3 min".
 * `coarse` is for times that are only known to the second: under one, "< 1 s". */
export function formatDuration(ms: number | null | undefined, locale = "en", options: { coarse?: boolean } = {}): string {
  if (ms == null || !Number.isFinite(ms)) return "–";
  if (options.coarse && ms < 1000) return "< 1 s";
  const number = (value: number, digits = 0) =>
    new Intl.NumberFormat(locale, { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value);
  if (ms < 1000) return `${number(Math.round(ms))} ms`;
  if (ms < 60_000) return `${number(ms / 1000, ms < 10_000 ? 1 : 0)} s`;
  const totalSeconds = Math.round(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours} h ${minutes} min`;
  return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
}

const STEPS: [limit: number, divisor: number, unit: Intl.RelativeTimeFormatUnit][] = [
  [60, 1, "second"],
  [3600, 60, "minute"],
  [86_400, 3600, "hour"],
  [604_800, 86_400, "day"],
  [2_629_800, 604_800, "week"],
  [31_557_600, 2_629_800, "month"],
  [Infinity, 31_557_600, "year"],
];

/** "3 minutes ago" / "vor 3 Minuten" — in the viewer's language, from their clock. */
export function relativeTime(iso: string | null | undefined, now: number, locale = "en"): string {
  if (!iso) return "–";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((then - now) / 1000);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const distance = Math.abs(seconds);
  // Under a minute it is "now": a count of seconds would be wrong a moment later.
  if (distance < 60) return formatter.format(0, "second");
  for (const [limit, divisor, unit] of STEPS) {
    if (distance < limit) return formatter.format(Math.trunc(seconds / divisor), unit);
  }
  return iso;
}

/** The viewer's local date and time, to the second. */
export function absoluteTime(iso: string | null | undefined, locale = "en"): string {
  if (!iso) return "–";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "medium" }).format(then);
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/** Sprint-2 before Sprint-10, case-insensitively. */
export const naturalCompare = (left: string, right: string): number => collator.compare(left, right);

export const sumCounts = (counts: Record<string, number>): number =>
  Object.values(counts).reduce((total, value) => total + value, 0);

/** How long a dispatch took, in ms — null while it has not ended. */
export function elapsedMs(startIso: string | null | undefined, endIso: string | null | undefined): number | null {
  if (!startIso || !endIso) return null;
  const elapsed = Date.parse(endIso) - Date.parse(startIso);
  return Number.isNaN(elapsed) ? null : Math.max(0, elapsed);
}

/** `key=value` → [key, value]; null when it is not one. */
export function parseLabel(text: string): [string, string] | null {
  const mark = text.indexOf("=");
  if (mark <= 0) return null;
  const key = text.slice(0, mark).trim();
  const value = text.slice(mark + 1).trim();
  return key ? [key, value] : null;
}

/**
 * Why a dispatch ended with an error, for what the run page says: its
 * `error_code` — or, for a record from before the codes existed, what its
 * state implies. null when it has no error.
 */
export function dispatchCause(dispatch: { error: string; error_code?: string; state: string } | undefined | null): string | null {
  if (!dispatch?.error) return null;
  if (dispatch.error_code) return dispatch.error_code;
  return dispatch.state === "STALE" ? "interrupted" : dispatch.state === "CANCELLED" ? "cancelled" : "runner-raised";
}
