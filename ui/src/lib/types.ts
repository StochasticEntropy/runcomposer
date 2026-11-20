// The shapes the HTTP API returns (DESIGN.md §9). One file, so a change of
// the API is a change here and the compiler names every place that reads it.

export type FilterAst = string | { op: "AND" | "OR"; items: FilterAst[] } | { not: FilterAst };

export interface ParameterSpec {
  name: string;
  kind: "string" | "integer" | "boolean" | "choice" | "multi_choice";
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

export interface QuickFilter {
  label: string;
  filter: string;
}

export interface Meta {
  version: string;
  locale_default: string;
  runners: RunnerInfo[];
  catalog: { items: number; tags: number; snapshot: string; loaded_at?: string; sources?: string[] };
  taxonomy: { configured: boolean };
  ingestion: { tokens: string; inbox: boolean };
  states: string[];
  completions: string[];
  ui: { quick_filters?: (string | QuickFilter)[] } & Record<string, unknown>;
  configured: { section: string; id: string; plugin: string }[];
}

export interface TaxonomyNode {
  id: string;
  label: string;
  filter?: string;
  origin: "file" | "tag" | "unassigned" | string;
  tag_count: number;
  item_count: number;
  children?: TaxonomyNode[];
}

export interface ResolvedTaxonomy {
  taxonomy: TaxonomyNode[];
  tags: string[];
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
  status: VerdictStatus;
  duration_ms: number;
  message: string;
  shard: string;
  attempt: number;
  live: boolean;
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
  url_or_path: string;
  kind: "local" | "url" | "external-path" | string;
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

export const isActive = (state: string): boolean => (ACTIVE_STATES as readonly string[]).includes(state);
