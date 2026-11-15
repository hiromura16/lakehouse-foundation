/** Lifecycle controls submit bounded plans; only the connected host runner executes them. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  Check,
  Clock3,
  Play,
  RefreshCw,
  RotateCw,
  ShieldCheck,
  Square,
  X,
} from 'lucide-react';
import { Badge, Button, Notice } from '../components.js';
import { ApiError } from '../api.js';
import { useResource } from '../hooks.js';
import {
  ACTION_LABELS,
  LifecycleSubmission,
  OPERATION_LABELS,
  lifecycleReason,
  observedState,
  parseLifecycleCapabilities,
  prepareLifecyclePlan,
  readLifecycleHistory,
  readLifecycleOperation,
  runnerFresh,
} from '../lifecycle.js';
import type {
  LifecycleAction,
  LifecycleCapabilities,
  LifecycleOperation,
  LifecyclePlan,
  LifecycleService,
} from '../lifecycle.js';
import type { Service } from '../types.js';

const OBSERVATION_LABELS = { running: '稼働中', stopped: '停止中', unknown: '未確認' };
const ACTION_ICONS = { start: Play, restart: RotateCw, stop: Square };
const ACTIVE = new Set(['queued', 'claimed', 'running']);

export function LifecycleServiceControls({
  service,
  capabilities,
  capability,
  busy,
  connectionError = false,
  now,
  onAction,
}: {
  service: Service;
  capabilities: LifecycleCapabilities | null;
  capability?: LifecycleService;
  busy: boolean;
  connectionError?: boolean;
  now?: number;
  onAction: (service: Service, action: LifecycleAction) => void;
}) {
  const connected = !connectionError && !!capabilities && runnerFresh(capabilities, now);
  const observed = connectionError ? 'unknown' : observedState(capabilities, capability, now);
  const blocked = busy || !!capabilities?.active_operation || !connected;
  return (
    <section className="service-lifecycle" aria-label={`${service.title}の起動管理`}>
      <div className="lifecycle-observation">
        <span>起動対象の設定</span>
        <strong>{(capability?.profile_selected ?? service.enabled) ? '選択済み' : '未選択'}</strong>
        <span>実コンテナ</span>
        <Badge tone={observed === 'running' ? 'green' : 'muted'} dot>
          {OBSERVATION_LABELS[observed]}
        </Badge>
      </div>
      <div className="lifecycle-actions">
        {(['start', 'restart', 'stop'] as const).map((action) => {
          const Icon = ACTION_ICONS[action];
          return (
            <Button
              key={action}
              variant={action === 'stop' ? 'danger' : 'secondary'}
              disabled={blocked || !capability?.allowed_actions.includes(action)}
              aria-label={`${service.title}を${ACTION_LABELS[action]}`}
              aria-describedby={`lifecycle-reason-${service.id}`}
              onClick={() => onAction(service, action)}
            >
              <Icon size={13} aria-hidden="true" />
              {ACTION_LABELS[action]}
            </Button>
          );
        })}
      </div>
      <p className="lifecycle-help" id={`lifecycle-reason-${service.id}`}>
        {!connected
          ? '管理機能未接続。起動設定だけから実際の稼働は判断しません。'
          : capability?.blocked_reasons.length
            ? lifecycleReason(capability.blocked_reasons[0])
            : !capability?.allowed_actions.length
              ? lifecycleReason('SERVICE_UNSUPPORTED')
              : capabilities?.active_operation
                ? lifecycleReason(
                    capabilities.active_operation.status === 'unknown'
                      ? 'OPERATION_UNKNOWN'
                      : 'OPERATION_BUSY',
                  )
                : '操作前に、共有する機能を含む影響対象を確認します。'}
      </p>
    </section>
  );
}

/** A queued receipt is not a running container; transport errors preserve uncertainty. */
export function LifecycleOperationNotice({
  operation,
  error,
  recovering,
  onRefresh,
}: {
  operation: LifecycleOperation | null;
  error: string | null;
  recovering: boolean;
  onRefresh: () => void;
}) {
  if (!operation && !error) return null;
  const tone =
    operation?.status === 'succeeded'
      ? 'success'
      : operation?.status === 'failed' || error
        ? 'warning'
        : 'info';
  return (
    <div className="lifecycle-operation" role="status" aria-live="polite">
      <Notice tone={tone}>
        <div className="lifecycle-operation-content">
          <strong>
            {operation
              ? `${ACTION_LABELS[operation.action]}：${OPERATION_LABELS[operation.status]}`
              : '受付結果を確認できません'}
          </strong>
          {operation && <span>対象グループ：{operation.group_id}</span>}
          {operation?.status === 'queued' && (
            <span>実行runnerによる開始は、まだ確認されていません。</span>
          )}
          {operation?.status === 'unknown' && <span>{lifecycleReason('OPERATION_UNKNOWN')}</span>}
          {operation?.status === 'failed' && (
            <span>{lifecycleReason(operation.error?.code ?? 'STATE_UNAVAILABLE')}</span>
          )}
          {error && <span>{error}</span>}
          {operation && <small>操作ID：{operation.id}</small>}
        </div>
        <Button busy={recovering} onClick={onRefresh}>
          <RefreshCw size={13} />
          {operation ? '状態を再取得' : '受付状況を確認'}
        </Button>
      </Notice>
    </div>
  );
}

export function LifecyclePlanContents({
  plan,
  title,
  acknowledged,
  remaining,
  submitting,
  uncertain,
  connected,
  onAcknowledge,
  onSubmit,
  onCancel,
  onRenew,
}: {
  plan: LifecyclePlan;
  title: string;
  acknowledged: boolean;
  remaining: number;
  submitting: boolean;
  uncertain: boolean;
  connected: boolean;
  onAcknowledge: (checked: boolean) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onRenew: () => void;
}) {
  return (
    <>
      <header>
        <span className="eyebrow">SERVICE CONTROL / CONFIRM</span>
        <h2 id="lifecycle-dialog-title">
          {title}を{ACTION_LABELS[plan.action]}する
        </h2>
        <p>このサービスと共有する機能を含め、下記の対象だけを操作します。</p>
      </header>
      <div className="lifecycle-impact">
        <strong>影響するコンテナ</strong>
        <ul>
          {plan.affected_services.map((name) => (
            <li key={name}>
              <code>{name}</code>
            </li>
          ))}
        </ul>
        {!!plan.added_profiles.length && <p>追加する起動対象：{plan.added_profiles.join(' / ')}</p>}
      </div>
      <div className="lifecycle-plan-notes">
        <p>
          <ShieldCheck size={15} aria-hidden="true" />
          停止してもデータやvolumeは削除しません。
        </p>
        {plan.warnings
          .filter((code) => code !== 'VOLUMES_PRESERVED')
          .map((code) => (
            <p key={code}>{lifecycleReason(code)}</p>
          ))}
        {plan.action !== 'start' && <p>対象の画面や連携が一時的に使えなくなります。</p>}
      </div>
      {!connected && (
        <Notice tone="warning">
          管理側への接続を確認できません。操作内容を再取得してください。
        </Notice>
      )}
      {uncertain && (
        <Notice tone="warning">
          受付結果が未確認です。新しい実行要求は送信せず、受付状況を確認してください。
        </Notice>
      )}
      {remaining <= 0 && !uncertain && (
        <Notice tone="warning">確認の有効期限が切れました。操作内容を再取得してください。</Notice>
      )}
      <label className="lifecycle-consent">
        <input
          type="checkbox"
          checked={acknowledged}
          disabled={submitting || uncertain || remaining <= 0 || !connected}
          onChange={(event) => onAcknowledge(event.target.checked)}
        />
        <span>影響対象を確認し、このPoCでの操作に同意します。</span>
      </label>
      <div className="lifecycle-dialog-footer">
        <span>
          <Clock3 size={13} />
          {remaining > 0 ? `確認期限まで ${remaining} 秒` : '期限切れ'}
        </span>
        <Button disabled={submitting || uncertain} onClick={onCancel}>
          キャンセル
        </Button>
        {remaining <= 0 && !uncertain ? (
          <Button disabled={submitting} onClick={onRenew}>
            操作内容を再取得
          </Button>
        ) : (
          <Button
            variant={plan.action === 'stop' ? 'danger' : 'primary'}
            busy={submitting}
            disabled={!acknowledged || uncertain || remaining <= 0 || !connected}
            onClick={onSubmit}
          >
            <Check size={14} />
            確認して{ACTION_LABELS[plan.action]}
          </Button>
        )}
      </div>
    </>
  );
}

function PlanDialog({
  plan,
  service,
  connected,
  submitting,
  uncertain,
  onSubmit,
  onCancel,
  onRenew,
}: {
  plan: LifecyclePlan;
  service: Service;
  connected: boolean;
  submitting: boolean;
  uncertain: boolean;
  onSubmit: () => void;
  onCancel: () => void;
  onRenew: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    // Native modal semantics trap focus and restore it when the dialog closes.
    dialog.current?.showModal();
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => {
      window.clearInterval(timer);
      dialog.current?.close();
    };
  }, []);
  return (
    <dialog
      className="lifecycle-dialog"
      ref={dialog}
      aria-labelledby="lifecycle-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!submitting && !uncertain) onCancel();
      }}
    >
      <button
        className="lifecycle-dialog-close icon-button"
        disabled={submitting || uncertain}
        aria-label="操作確認を閉じる"
        onClick={onCancel}
      >
        <X size={18} />
      </button>
      <LifecyclePlanContents
        plan={plan}
        title={service.title}
        acknowledged={acknowledged}
        remaining={Math.ceil(plan.expires_at - now)}
        submitting={submitting}
        uncertain={uncertain}
        connected={connected}
        onAcknowledge={setAcknowledged}
        onSubmit={onSubmit}
        onCancel={onCancel}
        onRenew={onRenew}
      />
    </dialog>
  );
}

export function useLifecycle(onChanged: () => Promise<void>) {
  const resource = useResource<unknown>('/lifecycle/capabilities');
  const capabilities = useMemo(() => {
    if (!resource.data) return null;
    try {
      return parseLifecycleCapabilities(resource.data);
    } catch {
      return null;
    }
  }, [resource.data]);
  const [target, setTarget] = useState<Service | null>(null);
  const [plan, setPlan] = useState<LifecyclePlan | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [operation, setOperation] = useState<LifecycleOperation | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [recovering, setRecovering] = useState(false);
  const generation = useRef(0);
  const mutation = useRef(false);
  const submission = useRef<LifecycleSubmission | null>(null);
  const latestOperation = useRef<LifecycleOperation | null>(null);
  const historyRead = useRef(false);
  const mounted = useRef(true);
  const terminal = operation && !ACTIVE.has(operation.status);
  const connected = !resource.error && !!capabilities && runnerFresh(capabilities);

  useEffect(() => {
    mounted.current = true;
    let timer: number | undefined;
    const poll = async () => {
      await resource.refresh();
      if (mounted.current) timer = window.setTimeout(() => void poll(), 5000);
    };
    timer = window.setTimeout(() => void poll(), 5000);
    return () => {
      mounted.current = false;
      generation.current += 1;
      window.clearTimeout(timer);
    };
  }, [resource.refresh]);

  const showOperation = useCallback(
    (result: LifecycleOperation) => {
      const previous = latestOperation.current;
      // A delayed polling response must not replace newer completion evidence.
      if (
        previous?.id === result.id &&
        (previous.updated_at > result.updated_at ||
          (!ACTIVE.has(previous.status) && ACTIVE.has(result.status)))
      )
        return;
      latestOperation.current = result;
      setOperation(result);
      setOperationError(null);
      setUncertain(false);
      setPlan(null);
      setTarget(null);
      if (!ACTIVE.has(result.status)) {
        void resource.refresh();
        void onChanged();
      }
    },
    [onChanged, resource.refresh],
  );

  useEffect(() => {
    if (
      !capabilities ||
      historyRead.current ||
      capabilities.active_operation ||
      operation ||
      uncertain
    )
      return;
    historyRead.current = true;
    let current = true;
    void readLifecycleHistory()
      .then((items) => {
        if (current && !latestOperation.current && items[0]) showOperation(items[0]);
      })
      .catch(() => {
        /* No history receipt is not evidence that containers are stopped. */
      });
    return () => {
      current = false;
    };
  }, [!!capabilities, capabilities?.active_operation?.id, operation, uncertain, showOperation]);

  // Reopening this page can resume a durable active operation without resubmission.
  useEffect(() => {
    const id = capabilities?.active_operation?.id;
    if (!id || id === operation?.id || uncertain) return;
    let current = true;
    void readLifecycleOperation(id)
      .then((result) => {
        if (current) showOperation(result);
      })
      .catch(() => {
        if (current) setOperationError('進行中の操作を確認できません。状態を再取得してください。');
      });
    return () => {
      current = false;
    };
  }, [capabilities?.active_operation?.id, operation?.id, uncertain, showOperation]);

  useEffect(() => {
    if (!operation || !ACTIVE.has(operation.status)) return;
    let current = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const result = await readLifecycleOperation(operation.id);
        if (!current) return;
        showOperation(result);
      } catch {
        if (current)
          setOperationError(
            '通信が途切れたため結果は未確認です。実行要求を繰り返さず状態を再取得してください。',
          );
      }
      if (current) timer = window.setTimeout(() => void poll(), 2000);
    };
    timer = window.setTimeout(() => void poll(), 2000);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [operation?.id, operation?.status, showOperation]);

  const prepare = async (service: Service, action: LifecycleAction) => {
    if (mutation.current || uncertain || !connected || capabilities?.active_operation) return;
    const capability = capabilities.services.find((item) => item.service_id === service.id);
    if (!capability?.allowed_actions.includes(action)) return;
    mutation.current = true;
    const current = ++generation.current;
    setPreparing(service.id);
    setPlanError(null);
    try {
      const result = await prepareLifecyclePlan(service.id, action);
      if (!mounted.current || generation.current !== current) return;
      if (result.group_id !== capability.group_id)
        throw new Error('操作対象の構成が変わりました。状態を再取得してください。');
      submission.current = new LifecycleSubmission(result, 'studio-' + crypto.randomUUID());
      setTarget(service);
      setPlan(result);
    } catch {
      if (mounted.current && generation.current === current)
        setPlanError(
          '操作内容を確認できません。管理接続・権限・共有する依存機能を確認し、状態を更新してください。',
        );
    } finally {
      mutation.current = false;
      if (mounted.current) setPreparing(null);
    }
  };

  const execute = async () => {
    if (mutation.current || !submission.current || uncertain || !connected) return;
    mutation.current = true;
    setSubmitting(true);
    latestOperation.current = null;
    setOperation(null);
    setOperationError(null);
    try {
      const result = await submission.current.submit();
      if (mounted.current) showOperation(result);
    } catch (failure) {
      if (
        mounted.current &&
        failure instanceof ApiError &&
        [400, 401, 403, 404, 409, 410, 422].includes(failure.status)
      ) {
        setPlan(null);
        setTarget(null);
        submission.current = null;
        setPlanError('操作要求は拒否されました。状態を更新し、準備条件と権限を確認してください。');
        void resource.refresh();
        return;
      }
      // A lost response does not prove rejection. Keep the key and block new actions.
      if (mounted.current) {
        setUncertain(true);
        setPlan(null);
        setTarget(null);
        setOperationError(
          '受付結果が未確認です。同じ操作を再送せず「受付状況を確認」を選んでください。',
        );
        void resource.refresh();
      }
    } finally {
      mutation.current = false;
      if (mounted.current) setSubmitting(false);
    }
  };

  const refreshOperation = async () => {
    if (recovering) return;
    setRecovering(true);
    try {
      const result =
        uncertain && submission.current
          ? await submission.current.recover()
          : operation
            ? await readLifecycleOperation(operation.id)
            : null;
      if (result && mounted.current) showOperation(result);
      else await resource.refresh();
    } catch {
      if (mounted.current)
        setOperationError(
          '受付済みかどうかを確認できません。新しい操作を送らず、管理者に状態確認を依頼してください。',
        );
    } finally {
      if (mounted.current) setRecovering(false);
    }
  };

  const cancel = () => {
    if (mutation.current || uncertain) return;
    generation.current += 1;
    setPlan(null);
    setTarget(null);
    submission.current = null;
  };

  return {
    capabilities,
    connected,
    resource,
    preparing,
    busy:
      !!preparing ||
      submitting ||
      uncertain ||
      (!!operation && (!terminal || operation.status === 'unknown')),
    prepare,
    planError,
    operation,
    operationError,
    recovering,
    refreshOperation,
    dialog:
      plan && target ? (
        <PlanDialog
          key={plan.id}
          plan={plan}
          service={target}
          connected={connected}
          submitting={submitting}
          uncertain={uncertain}
          onSubmit={() => void execute()}
          onCancel={cancel}
          onRenew={() => {
            const action = plan.action;
            cancel();
            void prepare(target, action);
          }}
        />
      ) : null,
  };
}

export function LifecycleConnectionNotice({
  connected,
  loading,
  failed,
  capabilities,
  onRefresh,
}: {
  connected: boolean;
  loading: boolean;
  failed: boolean;
  capabilities: LifecycleCapabilities | null;
  onRefresh: () => void;
}) {
  const profilePending =
    capabilities &&
    [...capabilities.applied_profiles].sort().join(',') !==
      [...capabilities.desired_profiles].sort().join(',');
  return (
    <div className="lifecycle-connection">
      <div>
        <Badge tone={connected ? 'green' : 'muted'} dot>
          {connected
            ? '管理runner接続中'
            : loading && !capabilities
              ? '管理機能を確認中'
              : '管理機能未接続'}
        </Badge>
        <span>起動対象の選択と、実際の稼働状態は別に表示します。</span>
      </div>
      {!connected && (
        <p>
          {failed
            ? '管理APIまたはrunnerへの接続を確認できません。'
            : '管理者が、このPoCだけを操作するホスト側runnerを接続すると操作できます。'}{' '}
          Docker操作の権限をブラウザーへ渡しません。
        </p>
      )}
      {profilePending && (
        <p>
          <AlertCircle size={14} />
          保存された起動対象と、APIの起動時設定が異なります。稼働状態はrunnerの観測を確認してください。
        </p>
      )}
      <Button busy={loading} onClick={onRefresh}>
        <RefreshCw size={13} />
        管理状態を更新
      </Button>
    </div>
  );
}
