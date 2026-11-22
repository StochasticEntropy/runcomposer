// The shapes the HTTP API returns (DESIGN.md §9). One file, so a change of
// the API is a change here and the compiler names every place that reads it.

export type FilterAst = string | { op: "AND" | "OR"; items: FilterAst[] } | { not: FilterAst };

export interface ParameterSpec {
  name: string;
  kind: "string" | "text" | "integer" | "boolean" | "choice" | "multi_choice" | "mapping";
  /** A regular expression the whole value must match (each value of a mapping). */
  pattern?: string | null;
  /** For a mapping: a regular expression every NAME must match as a whole. */
  key_pattern?: string | null;
  label: string;
  help: string;
  default: unknown;
  choices: string[];
  required: boolean;
}

export interface RunnerHealth {
  status: "ok" | "degraded" | "down" | "unknown" | string;
  detail: string;
  data: Record<string, unknown>;
}

export interface RunnerInfo {
  id: string;
  /** Set instead of everything else when the runner could not be described. */
  error?: string;
  plugin?: string;
  title?: string;
  description?: string;
  capabilities?: string[];
  result_format?: string;
  parameters?: ParameterSpec[];
  health?: RunnerHealth;
}

/** A filter as a deployment configures it: one pattern, or a whole expression. */
export interface FilterEntry {
  tag_filter: FilterAst;
  text: string;
  kind: "pattern" | "expression";
}

export interface QuickFilter extends FilterEntry {
  label: string;
}

/** A named, complete starting point: everything a run needs. */
export interface Preset extends FilterEntry {
  name: string;
  label: string;
  description: string;
  runner: string | null;
  parameters: Record<string, unknown>;
  title: string;
  labels: Record<string, string>;
}

export interface HealthCheck {
  check: string;
  ok: boolean;
  detail: string;
}

export interface PlanShard {
  shard: string;
  group: string | null;
  count: number;
  item_ids: string[];
  estimate_ms: number | null;
}

export interface PlanGroup {
  group: string;
  shards: string[];
  count: number;
  estimate_ms: number | null;
}

/** What a dispatch would do — or the refusal it would meet. */
export interface PlanResult {
  runner: string;
  run_id: string | null;
  item_count: number;
  parameters: Record<string, unknown>;
  refused: boolean;
  refusal: string | null;
  plan: { text: string; shards: PlanShard[]; groups: PlanGroup[]; notes: string[]; data: Record<string, unknown> } | null;
  warnings: string[];
}

export interface Meta {
  version: string;
  locale_default: string;
  /** The language the server and its runners write free text in (`core.text_language`, a BCP 47 tag). */
  text_language?: string;
  /** Whether the API documentation viewers (`/docs`, `/redoc`) are served; `/openapi.json` always is. */
  api?: { docs: boolean };
  runners: RunnerInfo[];
  catalog: { items: number; tags: number; snapshot: string; loaded_at?: string; sources?: string[] };
  taxonomy: { configured: boolean };
  ingestion: { tokens: string; inbox: boolean };
  states: string[];
  completions: string[];
  ui: { quick_filters?: QuickFilter[]; default_filter?: FilterEntry; presets?: Preset[] } & Record<string, unknown>;
  configured: { section: string; id: string; plugin: string }[];
}

export interface TaxonomyNode {
  id: string;
  label: string;
  /** A resolved leaf's one pattern, as the AST holds it (not filter text). */
  filter?: string;
  origin: "file" | "tag" | "unassigned" | string;
  tag_count: number;
  item_count: number;
  children?: TaxonomyNode[];
}

export interface ResolvedTaxonomy {
  taxonomy: TaxonomyNode[];
  tags: string[];
  /** Leaves read as one pattern only by the fallback for text that is not filter text. */
  warnings?: string[];
}

export interface TagCount {
  tag: string;
  count: number;
}

export interface CatalogItem {
  id: string;
  name: string;
  tags: string[];
  hierarchy: string[];
}

export interface Provenance {
  provider?: string;
  query?: { run?: string; verdicts?: string[]; labels?: Record<string, string> };
  resolved_run_id?: string;
  resolved_run_title?: string;
  resolved_run_completed_at?: string;
  resolved_run_labels?: Record<string, string>;
}

export interface CompileResult {
  count: number;
  total: number;
  items: CatalogItem[];
  warnings: string[];
  derived_from: Provenance[];
  snapshot: string;
}

export interface Selection {
  tag_filter?: FilterAst;
  item_ids?: string[];
  /** A history query, e.g. `failed@run:<id>` (DESIGN.md §7). */
  history?: string;
}

export type VerdictStatus = "PASS" | "FAIL" | "ERROR" | "SKIP" | "MISSING" | "RUNNING" | string;

export interface Dispatch {
  dispatch_id: string;
  mode: string;
  state: string;
  created_at: string;
  shards: string[];
  declared_shards: number;
  completion: string | null;
  completed_at: string | null;
  summary: Record<string, number>;
  error: string;
  plan: string;
  links: Record<string, string>;
  parameters: Record<string, unknown>;
  spec_sha256: string | null;
  cancel_requested: boolean;
  executing: boolean;
  /** Verdicts the dispatch owes over all its shards; null when no shard declared its items. */
  planned_verdicts: number | null;
  /** Why it ended with an error (the server's DISPATCH_ERROR_CODES), "" for none. */
  error_code?: DispatchErrorCode | "";
  /** For an interrupted dispatch: the process that was executing it. */
  error_owner?: string | null;
  duration_s?: number | null;
  shard_groups?: Record<string, string>;
}

export interface RunRow {
  id: string;
  title: string;
  created_at: string;
  origin: string;
  labels: Record<string, string>;
  state: string;
  completion: string | null;
  completed_at: string | null;
  planned_count: number;
  verdict_summary: Record<string, number>;
  mode: string | null;
  duration_s: number | null;
  dispatch_count: number;
  dispatches: Dispatch[];
}

export interface VerdictRow {
  item_id: string;
  /** The item's name as the run was composed; its id when none was kept. */
  name?: string;
  status: VerdictStatus;
  duration_ms: number;
  message: string;
  shard: string;
  attempt: number;
  live: boolean;
  /** On a MISSING row: what the item last reported live before the dispatch ended. */
  last_live?: { status: string; duration_ms: number; message: string; shard: string } | null;
}

/** The roll-up of the shards a runner declared as one group (a partition, a lane). */
export interface GroupSummary {
  group: string;
  shards: string[];
  declared_shards: number;
  delivered_shards: number;
  planned: number | null;
  count: number;
  summary: Record<string, number>;
  live: Record<string, number>;
  completion: string | null;
}

export interface ShardSummary {
  shard: string;
  declared: boolean;
  delivered: boolean;
  count: number;
  /** How many items this shard was planned to report; null when it never said. */
  planned: number | null;
  summary: Record<string, number>;
  completion: string | null;
}

export interface Artifact {
  name: string;
  media_type: string;
  dispatch_id: string | null;
  shard: string | null;
  group?: string | null;
  url_or_path: string;
  kind: "local" | "url" | "external-path" | "directory" | string;
  href: string | null;
}

export interface RunEvent {
  seq: number;
  run_id: string;
  at: string;
  kind: string;
  dispatch_id: string | null;
  data: Record<string, unknown>;
}

export interface Delivery {
  delivery_id: string;
  dispatch_id: string;
  shard: string;
  content_hash: string;
  format: string;
  created_at: string;
}

export interface RunDetail extends RunRow {
  deliveries: Delivery[];
  dispatch_id: string | null;
  verdicts: VerdictRow[];
  shards: ShardSummary[];
  /** shard → group, for the dispatch shown; empty when the runner declared no groups. */
  shard_groups?: Record<string, string>;
  groups?: GroupSummary[];
  /** shard → counts by live status, for shards that have not delivered. */
  live_summary?: Record<string, Record<string, number>>;
  rerun_of?: { run_id: string; scope: string } | null;
  artifacts: Artifact[];
  events: RunEvent[];
  last_event_seq: number;
  selection: { tag_filter: FilterAst | null; item_ids: string[]; derived_from: Provenance[] };
  expect_format: string;
  source: { provider?: string; snapshot?: string };
}

export interface RunList {
  runs: RunRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface CreateRunResponse {
  run: RunRow;
  warnings: string[];
  derived_from: Provenance[];
  dispatch?: Dispatch | null;
  spec_url?: string;
  spec_sha256?: string;
  spec_media_type?: string;
  ingest_token?: string | null;
}

export interface DispatchResponse {
  run: RunRow;
  dispatch: Dispatch | null;
  source_run_id?: string;
  rerun_of?: { run_id: string; scope: string } | null;
  spec_url?: string;
  spec_sha256?: string;
  spec_media_type?: string;
  ingest_token?: string | null;
}

export interface QuarantineEntry {
  entry_id: string;
  received_at: string;
  reason: string;
  transport: string;
  claimed_run_id: string | null;
  content_hash: string;
  format: string | null;
  detail: string;
  sequence: number;
}

export interface IngestReport {
  run_id: string;
  dispatch_id: string;
  shard: string;
  outcome: string;
  verdict_count: number;
  run_state: string;
  completion: string | null;
  warnings: string[];
}

export const ACTIVE_STATES = ["DISPATCHED", "RUNNING", "AWAITING_RESULTS"] as const;
export const TERMINAL_STATES = ["COMPLETE", "FAILED", "CANCELLED", "STALE"] as const;
/** Why a dispatch ended with an error — the server's closed list (core/model.py DISPATCH_ERROR_CODES). */
export const DISPATCH_ERROR_CODES = ["runner-raised", "interrupted", "expired", "cancelled"] as const;
export type DispatchErrorCode = (typeof DISPATCH_ERROR_CODES)[number];

export const isActive = (state: string): boolean => (ACTIVE_STATES as readonly string[]).includes(state);
