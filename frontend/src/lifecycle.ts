/** Public lifecycle contracts. Host privileges and credentials never enter this module. */
import { api, ApiError } from './api.js';

export type LifecycleAction = 'start' | 'restart' | 'stop';
export type ObservedState = 'running' | 'stopped' | 'unknown';
export type OperationStatus = 'queued' | 'claimed' | 'running' | 'succeeded' | 'failed' | 'unknown';

export interface LifecycleService {
  service_id: string;
  group_id: string | null;
  title: string;
  allowed_actions: LifecycleAction[];
  blocked_reasons: string[];
  observed_state: ObservedState;
  last_seen: number | null;
  profile_selected: boolean;
}

export interface LifecycleCapabilities {
  version: 1;
  manager: 'operator-compose';
  connected: boolean;
  last_seen: number | null;
  active_operation: { id: string; status: OperationStatus } | null;
  applied_profiles: string[];
  desired_profiles: string[];
  services: LifecycleService[];
}

export interface LifecyclePlan {
  id: string;
  service_id: string;
  action: LifecycleAction;
  group_id: string;
  affected_services: string[];
  added_profiles: string[];
  warnings: string[];
  confirmation: string;
  expires_at: number;
  manifest_digest: string;
}

export interface LifecycleOperation {
  id: string;
  service_id: string;
  action: LifecycleAction;
  group_id: string;
  status: OperationStatus;
  created_at: number;
  updated_at: number;
  error: { code: string } | null;
  result: { observed_state: ObservedState } | null;
}

export const ACTION_LABELS: Record<LifecycleAction, string> = {
  start: '起動',
  restart: '再起動',
  stop: '停止',
};
export const OPERATION_LABELS: Record<OperationStatus, string> = {
  queued: '受付済み・実行待ち',
  claimed: '実行準備中',
  running: '実行中',
  succeeded: '完了',
  failed: '失敗',
  unknown: '結果未確認',
};
const REASONS: Record<string, string> = {
  RUNNER_DISCONNECTED: '管理機能未接続。管理者がこのPoC用の実行runnerを接続してください。',
  INITIAL_SETUP_REQUIRED:
    '初回の準備が必要です。イメージ・設定・保存先を管理者に確認してください。',
  SERVICE_PROTECTED: '基盤サービスを保護するため、この画面からは操作できません。',
  SERVICE_UNSUPPORTED: 'このサービスは、現在の管理対象に含まれていません。',
  SOURCE_CONNECTION_APPROVAL_REQUIRED: '外部ソースへの接続を伴うため、専用の承認手順が必要です。',
  HOST_PRIVILEGE_APPROVAL_REQUIRED: 'ホストを参照する権限の準備・承認が必要です。',
  SOURCE_OR_HOST_APPROVAL_REQUIRED: '外部ソース接続またはホスト参照の承認が必要です。',
  OPERATION_BUSY: 'ほかの操作が進行中です。完了を確認してから操作してください。',
  LIFECYCLE_BUSY: '管理操作が進行中です。完了を確認してから操作してください。',
  OPERATION_UNKNOWN: '前の操作結果が未確認です。管理者による状態確認が必要です。',
  PLAN_EXPIRED: '確認計画の有効期限が切れました。操作内容を再取得してください。',
  PLAN_CHANGED: '対象の構成が変わりました。操作内容を再取得してください。',
  CONFIRMATION_REQUIRED: '影響対象を読み、確認欄を選択してください。',
  STATE_UNAVAILABLE: '稼働状態を確認できません。管理者の接続を確認してください。',
  MEMORY_BUDGET: '利用できるメモリが不足しています。追加起動を控えてください。',
  PROFILE_DEPENDENCY: '共有する依存機能があります。影響範囲を確認してください。',
  RUNNER_SCOPE_CHANGED: '実行runnerの承認範囲が変わりました。再確認が必要です。',
  APPROVAL_CHANGED: '承認済みの構成が変わりました。管理者による再確認が必要です。',
  OPERATOR_AUTH_REQUIRED: 'ホスト側の実行権限を確認できません。管理者の準備が必要です。',
  OPERATOR_COMMAND_FAILED:
    'ホスト側の操作を完了できませんでした。管理者が対象の状態を確認してください。',
  OPERATOR_RECONCILED: '管理者が前回操作の状態を確認しました。最新の稼働状態を再取得してください。',
  PROFILE_SAVE_FAILED:
    '起動対象の保存を完了できませんでした。管理者が設定と稼働状態を確認してください。',
  GROUP_OPERATION: '共有する機能を含むグループ単位の操作です。表示された対象をまとめて操作します。',
  VOLUMES_PRESERVED: '停止しても、保存済みデータやvolumeは削除しません。',
  PROFILE_REDEPLOY_PENDING: '起動対象の設定と、コンテナへの反映状況は別々に確認してください。',
  LOCAL_OPERATOR_REQUIRED: '管理者が接続したホスト側runnerが、このPoC限定の操作を実行します。',
  EXTERNAL_JOBS_NOT_ATOMICALLY_LOCKED:
    '外部からの処理投入は一括停止できません。自動実行も止めた時間に操作してください。',
};

/** Only application-owned codes become text; never reflect a command error body. */
export function lifecycleReason(code: string): string {
  return REASONS[code] ?? '操作条件を確認できません。管理者による確認が必要です。';
}

const FORMAT_ERROR = '管理API応答の形式を確認できません。画面とAPIの版を確認してください。';
const ID = /^[A-Za-z0-9_-]{8,80}$/;
const SERVICE_ID = /^[a-z][a-z0-9-]{0,63}$/;
const CODE = /^[A-Z][A-Z0-9_]{0,79}$/;
const STATUSES: OperationStatus[] = [
  'queued',
  'claimed',
  'running',
  'succeeded',
  'failed',
  'unknown',
];
const ACTIONS: LifecycleAction[] = ['start', 'restart', 'stop'];
const OBSERVATIONS: ObservedState[] = ['running', 'stopped', 'unknown'];

function invalid(): never {
  throw new ApiError(0, FORMAT_ERROR);
}
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}
function timestamp(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}
function nullableTime(value: unknown): value is number | null {
  return value === null || timestamp(value);
}
function list(value: unknown, pattern: RegExp, maximum = 100): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= maximum &&
    value.every((item) => typeof item === 'string' && pattern.test(item)) &&
    new Set(value).size === value.length
  );
}
function identity(value: unknown): value is string {
  return typeof value === 'string' && ID.test(value);
}
function serviceId(value: unknown): value is string {
  return typeof value === 'string' && SERVICE_ID.test(value);
}
function action(value: unknown): value is LifecycleAction {
  return ACTIONS.includes(value as LifecycleAction);
}
function status(value: unknown): value is OperationStatus {
  return STATUSES.includes(value as OperationStatus);
}
function observation(value: unknown): value is ObservedState {
  return OBSERVATIONS.includes(value as ObservedState);
}

/** Project explicit public fields; unexpected payload fields are not retained. */
export function parseLifecycleCapabilities(value: unknown): LifecycleCapabilities {
  const body = record(value);
  if (
    body.version !== 1 ||
    body.manager !== 'operator-compose' ||
    typeof body.connected !== 'boolean' ||
    !nullableTime(body.last_seen) ||
    (body.connected && body.last_seen === null) ||
    !list(body.applied_profiles, SERVICE_ID, 30) ||
    !list(body.desired_profiles, SERVICE_ID, 30) ||
    !Array.isArray(body.services) ||
    body.services.length > 100
  )
    invalid();
  let active: LifecycleCapabilities['active_operation'] = null;
  if (body.active_operation !== null) {
    const item = record(body.active_operation);
    if (!identity(item.id) || !status(item.status)) invalid();
    active = { id: item.id, status: item.status };
  }
  const services = body.services.map((value): LifecycleService => {
    const item = record(value);
    if (
      !serviceId(item.service_id) ||
      !(item.group_id === null || serviceId(item.group_id)) ||
      typeof item.title !== 'string' ||
      item.title.length > 160 ||
      !Array.isArray(item.allowed_actions) ||
      !item.allowed_actions.every(action) ||
      new Set(item.allowed_actions).size !== item.allowed_actions.length ||
      (item.group_id === null && item.allowed_actions.length > 0) ||
      !list(item.blocked_reasons, CODE, 20) ||
      !observation(item.observed_state) ||
      !nullableTime(item.last_seen) ||
      typeof item.profile_selected !== 'boolean'
    )
      invalid();
    return {
      service_id: item.service_id,
      group_id: item.group_id,
      title: item.title,
      allowed_actions: item.allowed_actions,
      blocked_reasons: item.blocked_reasons,
      observed_state: item.observed_state,
      last_seen: item.last_seen,
      profile_selected: item.profile_selected,
    };
  });
  if (new Set(services.map((item) => item.service_id)).size !== services.length) invalid();
  return {
    version: 1,
    manager: 'operator-compose',
    connected: body.connected,
    last_seen: body.last_seen,
    active_operation: active,
    applied_profiles: body.applied_profiles,
    desired_profiles: body.desired_profiles,
    services,
  };
}

export function isLifecycleCapabilities(value: unknown): value is LifecycleCapabilities {
  try {
    parseLifecycleCapabilities(value);
    return true;
  } catch {
    return false;
  }
}

/** A selected profile is configuration, not evidence of a running container. */
export function runnerFresh(capabilities: LifecycleCapabilities, now = Date.now() / 1000): boolean {
  if (!capabilities.connected || capabilities.last_seen === null) return false;
  const age = now - capabilities.last_seen;
  return age >= -5 && age <= 40;
}

export function observedState(
  capabilities: LifecycleCapabilities | null,
  service: LifecycleService | undefined,
  now = Date.now() / 1000,
): ObservedState {
  if (!capabilities || !service || !runnerFresh(capabilities, now) || service.last_seen === null)
    return 'unknown';
  const age = now - service.last_seen;
  return age >= -5 && age <= 40 ? service.observed_state : 'unknown';
}

export function parseLifecyclePlan(
  value: unknown,
  expected: { service_id: string; action: LifecycleAction },
): LifecyclePlan {
  const item = record(value);
  if (
    !identity(item.id) ||
    item.service_id !== expected.service_id ||
    item.action !== expected.action ||
    !serviceId(item.group_id) ||
    !list(item.affected_services, SERVICE_ID, 40) ||
    item.affected_services.length === 0 ||
    !list(item.added_profiles, SERVICE_ID, 30) ||
    !list(item.warnings, CODE, 20) ||
    typeof item.confirmation !== 'string' ||
    item.confirmation.length < 8 ||
    item.confirmation.length > 256 ||
    !timestamp(item.expires_at) ||
    typeof item.manifest_digest !== 'string' ||
    !/^[a-f0-9]{64}$/.test(item.manifest_digest)
  )
    invalid();
  return {
    id: item.id,
    service_id: expected.service_id,
    action: expected.action,
    group_id: item.group_id,
    affected_services: item.affected_services,
    added_profiles: item.added_profiles,
    warnings: item.warnings,
    confirmation: item.confirmation,
    expires_at: item.expires_at,
    manifest_digest: item.manifest_digest,
  };
}

export function parseLifecycleOperation(value: unknown): LifecycleOperation {
  const item = record(value);
  if (
    !identity(item.id) ||
    !serviceId(item.service_id) ||
    !serviceId(item.group_id) ||
    !action(item.action) ||
    !status(item.status) ||
    !timestamp(item.created_at) ||
    !timestamp(item.updated_at)
  )
    invalid();
  let error: LifecycleOperation['error'] = null;
  if (item.error !== null && item.error !== undefined) {
    const supplied = record(item.error);
    if (typeof supplied.code !== 'string' || !CODE.test(supplied.code)) invalid();
    // A runner's message is never rendered, even if an upstream bug returns it.
    error = { code: supplied.code };
  }
  let result: LifecycleOperation['result'] = null;
  if (item.result !== null && item.result !== undefined) {
    const supplied = record(item.result);
    if (!observation(supplied.observed_state)) invalid();
    result = { observed_state: supplied.observed_state };
  }
  if (
    item.status === 'succeeded' &&
    (error || result?.observed_state !== (item.action === 'stop' ? 'stopped' : 'running'))
  )
    invalid();
  return {
    id: item.id,
    service_id: item.service_id,
    action: item.action,
    group_id: item.group_id,
    status: item.status,
    created_at: item.created_at,
    updated_at: item.updated_at,
    error,
    result,
  };
}

export async function prepareLifecyclePlan(service_id: string, requestedAction: LifecycleAction) {
  if (!serviceId(service_id) || !action(requestedAction)) invalid();
  const expected = { service_id, action: requestedAction };
  const result = await api<unknown>('/lifecycle/plans', { method: 'POST', payload: expected });
  return parseLifecyclePlan(result, expected);
}

export async function readLifecycleOperation(id: string): Promise<LifecycleOperation> {
  if (!identity(id)) invalid();
  return parseLifecycleOperation(await api<unknown>('/lifecycle/operations/' + id));
}

/** A page remount can recover the latest journal receipt without repeating a command. */
export async function readLifecycleHistory(): Promise<LifecycleOperation[]> {
  const body = record(await api<unknown>('/lifecycle/operations?limit=10'));
  if (!Array.isArray(body.operations) || body.operations.length > 10) invalid();
  const operations = body.operations.map(parseLifecycleOperation);
  if (new Set(operations.map((item) => item.id)).size !== operations.length) invalid();
  return operations;
}

/** One POST at most. An ambiguous receipt is recovered by a read-only key lookup. */
export class LifecycleSubmission {
  readonly plan: LifecyclePlan;
  private submitted: Promise<LifecycleOperation> | null = null;

  constructor(
    plan: LifecyclePlan,
    readonly key: string,
  ) {
    if (!identity(key)) invalid();
    this.plan = parseLifecyclePlan(plan, plan);
  }

  submit(): Promise<LifecycleOperation> {
    if (this.submitted) return this.submitted;
    if (this.plan.expires_at <= Date.now() / 1000)
      return Promise.reject(new ApiError(409, lifecycleReason('PLAN_EXPIRED')));
    this.submitted = api<unknown>('/lifecycle/operations', {
      method: 'POST',
      payload: {
        plan_id: this.plan.id,
        confirmation: this.plan.confirmation,
        idempotency_key: this.key,
      },
    }).then((value) => this.verify(value));
    return this.submitted;
  }

  async recover(): Promise<LifecycleOperation> {
    const value = await api<unknown>('/lifecycle/operations/by-key/' + this.key);
    return this.verify(value);
  }

  private verify(value: unknown): LifecycleOperation {
    const operation = parseLifecycleOperation(value);
    if (
      operation.service_id !== this.plan.service_id ||
      operation.action !== this.plan.action ||
      operation.group_id !== this.plan.group_id
    )
      invalid();
    return operation;
  }
}
