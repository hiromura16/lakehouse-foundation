/** Public response contracts. These types never contain the control token. */
export type Page = 'overview' | 'services' | 'onboarding' | 'data' | 'operations' | 'settings';
export type Tone = 'cyan' | 'green' | 'amber' | 'red' | 'muted';
export interface Service {
  id: string;
  title: string;
  description: string;
  profile: string;
  profiles?: string[];
  enabled: boolean;
  configured: boolean;
  state: 'not_enabled' | 'unconfigured' | 'available' | 'unreachable';
  url: string;
  link_mode?: 'local' | 'public';
  link_configured?: boolean;
  missing_settings: string[];
}
export interface ServiceList {
  services: Service[];
  pending_restart: boolean;
}
export interface Dataset {
  id: string;
  title: string;
  layer: 'silver' | 'gold';
  table: string;
  columns: { name: string; type: string; description: string }[];
  grain: string;
  primary_key: string[];
  provenance: string[];
}
export interface Catalog {
  version: number;
  datasets: Dataset[];
}
export interface Job {
  id: string;
  kind: string;
  status: string;
  completed: number;
  batches: number;
  pending_restart?: boolean;
  results: unknown[];
  error?: { code: string; message: string } | null;
}
export interface JobList {
  jobs: Job[];
  persistent: boolean;
  history_limit: number;
}
export interface AcceptedJob {
  id: string;
  status: string;
  kind: string;
  pending_restart?: boolean;
}
export interface SettingsView {
  values: Record<string, string>;
  secret_keys: string[];
  configured_secrets: string[];
  pending_restart: boolean;
  pending_mount_change: boolean;
}
export interface SettingsSaved extends SettingsView {
  saved: boolean;
  applied: boolean;
}
export interface Preview {
  datasets: { table: string; snapshot_id: string; rows: Record<string, unknown>[] }[];
}
export interface Quality {
  datasets: {
    dataset: string;
    bronze_rows: number;
    silver_rows: number;
    quarantine_rows: number;
  }[];
  description: string;
}
export interface MetadataColumn {
  name: string;
  sql_type: string;
  nullable: boolean;
  computed: boolean;
}
export interface MetadataTable {
  schema_name: string;
  table_name: string;
  columns: MetadataColumn[];
  primary_key: string[];
  capture_instance: string | null;
  cdc_enabled: boolean;
}
export interface Discovery {
  schema_version: 1;
  source_ref: string;
  origin: 'synthetic' | 'imported';
  tables: MetadataTable[];
}
export interface DiscoveryResult {
  schema_version: 1;
  discovery: Discovery;
  metadata_sha256?: string;
  database_checked: false;
}
export type Classification = 'unknown' | 'public' | 'internal' | 'sensitive' | 'excluded';
export interface TableSelection {
  schema_name: string;
  table_name: string;
  included_columns: string[];
  logical_name: string;
  business_purpose: string;
  freshness_minutes: number;
  delete_handling: 'unreviewed' | 'retain' | 'propagate';
  column_classifications: Record<string, Classification>;
  primary_key_confirmed: boolean;
}
export type ReviewRole = 'business' | 'technical' | 'dba' | 'security';
export type Review = Record<ReviewRole, boolean>;
export interface ProposalRequest {
  discovery: Discovery;
  source_id: string;
  owner_team: string;
  selections: TableSelection[];
  review?: Review & { contract_sha256: string };
}
export interface Finding {
  code: string;
  message: string;
  table?: string;
  column?: string;
}
export interface DraftResult {
  schema_version: 1;
  status: 'proposed' | 'plan';
  contract: Record<string, unknown>;
  metadata_sha256: string;
  contract_sha256: string;
  warnings: Finding[];
  blockers: Finding[];
  review: Review & { hash_matches: boolean; verified: false };
  applied: false;
  can_apply: false;
  approvals_verified: false;
  database_checked: false;
  steps?: { id: string; status: string; description: string }[];
}
