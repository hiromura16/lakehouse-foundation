/** Public manager DTOs. Docker identity and execution authority stay on the host. */
import { ApiError } from './api.js';

export type PocId = 'foundation' | 'vision';
export type PocAction = 'start' | 'stop' | 'restart';
export type OperationStatus = 'queued' | 'claimed' | 'running' | 'succeeded' | 'failed' | 'unknown';
export interface ManagerSession {
  authenticated: boolean;
  mode: 'token' | 'cloudflare-access';
  role: 'operator' | null;
  logout_url: '/cdn-cgi/access/logout' | null;
}
export interface Operation {
  id: string;
  poc_id: PocId;
  action: PocAction;
  status: OperationStatus;
  created_at: number;
  updated_at: number;
  completed_steps: number;
  step_count: number;
  error_code: string | null;
  interruption_ack: boolean;
  interruption_possible: boolean;
}
export interface Poc {
  poc_id: PocId;
  title: string;
  state: 'running' | 'stopped' | 'partial' | 'unknown';
  services: { service: string; state: string }[];
  blocked_reasons: string[];
  observed_at: number | null;
  operation?: Operation | null;
}
export interface ManagerState {
  schema_version: 1;
  runner_connected: boolean;
  pocs: Poc[];
}
export interface ManagerPlan {
  schema_version: 1;
  can_execute: false;
  poc_id: PocId;
  action: PocAction;
  requires_interruption_ack: boolean;
  interruption_acknowledged: boolean;
  effect: string;
  snapshot_digest: string;
  steps: { service: string; action: 'start' | 'stop' }[];
  excluded_completed_bootstraps: string[];
  volume_deletion: false;
  automatic_retry: false;
  plan_id: string;
  confirmation: string;
  expires_at: number;
}

type RecordValue = Record<string, unknown>;
function requireValue(condition: unknown): asserts condition {
  if (!condition)
    throw new ApiError(
      0,
      '管理APIの応答を確認できません。操作を繰り返さず、受付結果を確認してください。',
    );
}
function record(value: unknown): RecordValue {
  requireValue(value && typeof value === 'object' && !Array.isArray(value));
  return value as RecordValue;
}
function text(value: unknown, max = 128): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= max &&
    !/[\x00-\x1f]/.test(value)
  );
}
function epoch(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}
function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 256;
}
function pocId(value: unknown): value is PocId {
  return value === 'foundation' || value === 'vision';
}
function action(value: unknown): value is PocAction {
  return value === 'start' || value === 'stop' || value === 'restart';
}
function list(value: unknown, max: number): unknown[] {
  requireValue(Array.isArray(value) && value.length <= max);
  return value;
}
export function parseManagerSession(value: unknown): ManagerSession {
  const v = record(value);
  requireValue(
    typeof v.authenticated === 'boolean' &&
      (v.mode === 'token' || v.mode === 'cloudflare-access') &&
      (v.role === 'operator' || v.role === null) &&
      (v.logout_url === null || v.logout_url === '/cdn-cgi/access/logout') &&
      (v.mode !== 'token' || v.logout_url === null),
  );
  return {
    authenticated: v.authenticated as boolean,
    mode: v.mode as ManagerSession['mode'],
    role: v.role as ManagerSession['role'],
    logout_url: v.logout_url as ManagerSession['logout_url'],
  };
}
export function parseOperation(value: unknown): Operation {
  const v = record(value);
  requireValue(
    typeof v.id === 'string' && /^[a-f0-9]{24}$/.test(v.id) && pocId(v.poc_id) && action(v.action),
  );
  requireValue(
    ['queued', 'claimed', 'running', 'succeeded', 'failed', 'unknown'].includes(String(v.status)) &&
      epoch(v.created_at) &&
      epoch(v.updated_at) &&
      count(v.completed_steps) &&
      count(v.step_count) &&
      v.completed_steps <= v.step_count &&
      (v.error_code === null ||
        (typeof v.error_code === 'string' && /^[A-Z_]{1,64}$/.test(v.error_code))) &&
      typeof v.interruption_ack === 'boolean' &&
      typeof v.interruption_possible === 'boolean',
  );
  // Copy a fixed public projection. Unexpected backend fields never become
  // future UI exports, state history, or browser persistence by accident.
  return {
    id: v.id as string,
    poc_id: v.poc_id as PocId,
    action: v.action as PocAction,
    status: v.status as OperationStatus,
    created_at: v.created_at as number,
    updated_at: v.updated_at as number,
    completed_steps: v.completed_steps as number,
    step_count: v.step_count as number,
    error_code: v.error_code as string | null,
    interruption_ack: v.interruption_ack as boolean,
    interruption_possible: v.interruption_possible as boolean,
  };
}
export function parseOperations(value: unknown): Operation[] {
  return list(record(value).operations, 30).map(parseOperation);
}
export function parseManagerState(value: unknown): ManagerState {
  const v = record(value);
  requireValue(v.schema_version === 1 && typeof v.runner_connected === 'boolean');
  const pocs = list(v.pocs, 2).map((item) => {
    const p = record(item);
    requireValue(
      pocId(p.poc_id) &&
        text(p.title) &&
        ['running', 'stopped', 'partial', 'unknown'].includes(String(p.state)),
    );
    requireValue(p.observed_at === null || epoch(p.observed_at));
    list(p.services, 128).forEach((item) => {
      const s = record(item);
      requireValue(
        text(s.service, 80) &&
          /^[a-z0-9][a-z0-9-]*$/.test(s.service) &&
          (s.state === 'running' || s.state === 'stopped'),
      );
    });
    list(p.blocked_reasons, 12).forEach((reason) =>
      requireValue(typeof reason === 'string' && /^[A-Z_]{1,64}$/.test(reason)),
    );
    if (p.operation !== undefined && p.operation !== null) parseOperation(p.operation);
    return {
      poc_id: p.poc_id as PocId,
      title: p.title as string,
      state: p.state as Poc['state'],
      services: (p.services as RecordValue[]).map((s) => ({
        service: s.service as string,
        state: s.state as string,
      })),
      blocked_reasons: p.blocked_reasons as string[],
      observed_at: p.observed_at as number | null,
      ...(p.operation ? { operation: parseOperation(p.operation) } : {}),
    };
  });
  requireValue(new Set(pocs.map((p) => p.poc_id)).size === pocs.length);
  return { schema_version: 1, runner_connected: v.runner_connected, pocs };
}
export function parseManagerPlan(value: unknown): ManagerPlan {
  const v = record(value);
  requireValue(
    v.schema_version === 1 && v.can_execute === false && pocId(v.poc_id) && action(v.action),
  );
  requireValue(
    typeof v.requires_interruption_ack === 'boolean' &&
      typeof v.interruption_acknowledged === 'boolean' &&
      text(v.effect) &&
      typeof v.snapshot_digest === 'string' &&
      /^[a-f0-9]{64}$/.test(v.snapshot_digest) &&
      v.volume_deletion === false &&
      v.automatic_retry === false &&
      typeof v.plan_id === 'string' &&
      /^[a-f0-9]{24}$/.test(v.plan_id) &&
      typeof v.confirmation === 'string' &&
      /^[a-f0-9]{32}$/.test(v.confirmation) &&
      epoch(v.expires_at),
  );
  list(v.steps, 256).forEach((item) => {
    const s = record(item);
    requireValue(
      text(s.service, 80) &&
        /^[a-z0-9][a-z0-9-]*$/.test(s.service) &&
        (s.action === 'start' || s.action === 'stop'),
    );
  });
  list(v.excluded_completed_bootstraps, 32).forEach((item) => requireValue(text(item, 80)));
  return {
    schema_version: 1,
    can_execute: false,
    poc_id: v.poc_id as PocId,
    action: v.action as PocAction,
    requires_interruption_ack: v.requires_interruption_ack as boolean,
    interruption_acknowledged: v.interruption_acknowledged as boolean,
    effect: v.effect as string,
    snapshot_digest: v.snapshot_digest as string,
    steps: (v.steps as RecordValue[]).map((step) => ({
      service: step.service as string,
      action: step.action as 'start' | 'stop',
    })),
    excluded_completed_bootstraps: [...(v.excluded_completed_bootstraps as string[])],
    volume_deletion: false,
    automatic_retry: false,
    plan_id: v.plan_id as string,
    confirmation: v.confirmation as string,
    expires_at: v.expires_at as number,
  };
}

export const POC_TITLES = { foundation: '基盤PoC', vision: '将来構想PoC' };
export const ACTION_TITLES = { start: '起動', stop: '停止', restart: '再起動' };
export const STATE_TITLES = {
  running: '稼働中',
  stopped: '停止中',
  partial: '一部稼働',
  unknown: '未確認',
};
export const OPERATION_TITLES: Record<OperationStatus, string> = {
  queued: '受付済み',
  claimed: '実行準備',
  running: '処理中',
  succeeded: '完了',
  failed: '停止・要確認',
  unknown: '結果未確定',
};
export const BLOCKED_TITLES: Record<string, string> = {
  RUNNER_OFFLINE: 'ホスト管理処理が未接続です。',
  OPERATION_BUSY: '別の操作を処理中です。',
  OUTCOME_UNKNOWN: '前の操作結果が未確定です。管理者の確認が必要です。',
  OPERATION_UNKNOWN: '前の操作結果が未確定です。管理者の確認が必要です。',
  WORKLOAD_BUSY: '処理の完了待ち、または稼働ジョブの確認が必要です。',
  INTERRUPTION_ACK_REQUIRED: '処理中断の可能性を確認して、停止・再起動を計画できます。',
  MEMORY_BUDGET: '起動のメモリ予算を確認してください。停止は実行できます。',
  STATE_UNAVAILABLE: 'ホストの状態を取得できません。',
  APPROVAL_CHANGED: '承認した構成と現在の構成が一致しません。',
  IDENTITY_CHANGED: '対象の構成が変わりました。再登録を確認してください。',
};

/** Unknown data never becomes an enabled operation or a green success state. */
export function canPlan(state: ManagerState | null, poc: Poc, busy: boolean): boolean {
  return (
    !!state?.runner_connected &&
    !busy &&
    !poc.blocked_reasons.some((code) =>
      [
        'RUNNER_OFFLINE',
        'OPERATION_BUSY',
        'OUTCOME_UNKNOWN',
        'OPERATION_UNKNOWN',
        'STATE_UNAVAILABLE',
        'APPROVAL_CHANGED',
        'IDENTITY_CHANGED',
      ].includes(code),
    )
  );
}
