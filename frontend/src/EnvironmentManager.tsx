/** Always-on management plane, independent from the two managed PoCs. */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  BookOpen,
  Boxes,
  CheckCircle2,
  Layers3,
  LockKeyhole,
  LogOut,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
} from 'lucide-react';
import { ApiError, managerRequest } from './api.js';
import {
  Badge,
  Button,
  ErrorNotice,
  Loading,
  MiniProgress,
  Notice,
  PageHeader,
  Panel,
} from './components.js';
import {
  ACTION_TITLES,
  BLOCKED_TITLES,
  OPERATION_TITLES,
  POC_TITLES,
  STATE_TITLES,
  canPlan,
  parseManagerPlan,
  parseManagerSession,
  parseManagerState,
  parseOperation,
  parseOperations,
  type ManagerPlan,
  type ManagerSession,
  type ManagerState,
  type Operation,
  type Poc,
  type PocAction,
} from './manager.js';

const message = (error: unknown) =>
  error instanceof ApiError
    ? error.message
    : '状態を取得できませんでした。操作の重複送信は行いません。';
const time = (epoch: number | null) =>
  epoch ? new Date(epoch * 1000).toLocaleString('ja-JP') : '未取得';
const active = (operation: Operation) =>
  ['queued', 'claimed', 'running', 'unknown'].includes(operation.status);

/** Separately exported for source-only rendering/accessibility regressions. */
export function EnvironmentCard({
  poc,
  state,
  busy,
  onPlan,
}: {
  poc: Poc;
  state: ManagerState | null;
  busy: boolean;
  onPlan: (poc: Poc, action: PocAction) => void;
}) {
  const running = poc.services.filter((service) => service.state === 'running').length;
  return (
    <section className={`environment-card environment-${poc.poc_id}`}>
      <header>
        <div className="environment-symbol">
          {poc.poc_id === 'foundation' ? <Layers3 size={27} /> : <Boxes size={27} />}
        </div>
        <Badge
          tone={poc.state === 'running' ? 'green' : poc.state === 'partial' ? 'amber' : 'muted'}
          dot
        >
          {STATE_TITLES[poc.state]}
        </Badge>
      </header>
      <span className="eyebrow">
        {poc.poc_id === 'foundation' ? 'FOUNDATION' : 'FUTURE WORKSPACE'}
      </span>
      <h2>{POC_TITLES[poc.poc_id]}</h2>
      <p>
        {poc.poc_id === 'foundation'
          ? '既存の取り込み・変換・分析基盤を一括管理。'
          : 'Reactポータル、メタデータ駆動の将来構想を独立して検証。'}
      </p>
      <div className="environment-count">
        <strong>
          {running}
          <small> / {poc.services.length}</small>
        </strong>
        <span>登録サービス稼働</span>
      </div>
      <MiniProgress value={running} maximum={poc.services.length} />
      <div className="environment-actions">
        <Button
          variant="primary"
          disabled={!canPlan(state, poc, busy)}
          onClick={() => onPlan(poc, 'start')}
        >
          <Play size={15} />
          起動
        </Button>
        <Button disabled={!canPlan(state, poc, busy)} onClick={() => onPlan(poc, 'restart')}>
          <RefreshCw size={15} />
          再起動
        </Button>
        <Button
          variant="danger"
          disabled={!canPlan(state, poc, busy)}
          onClick={() => onPlan(poc, 'stop')}
        >
          <Square size={14} />
          停止
        </Button>
      </div>
      {poc.blocked_reasons.length > 0 && (
        <ul className="environment-reasons">
          {poc.blocked_reasons.map((code) => (
            <li key={code}>{BLOCKED_TITLES[code] ?? '操作前の構成確認が必要です。'}</li>
          ))}
        </ul>
      )}
      <details className="environment-services">
        <summary>サービスの内訳</summary>
        <ul>
          {poc.services.map((service) => (
            <li key={service.service}>
              <code>{service.service}</code>
              <span>
                {service.state === 'running'
                  ? '稼働'
                  : service.state === 'stopped'
                    ? '停止'
                    : '要確認'}
              </span>
            </li>
          ))}
        </ul>
      </details>
      <footer>観測日時 {time(poc.observed_at)}</footer>
    </section>
  );
}

function OperationRow({ operation }: { operation: Operation }) {
  return (
    <li className="manager-operation">
      <div>
        <strong>
          {POC_TITLES[operation.poc_id]} · {ACTION_TITLES[operation.action]}
        </strong>
        <span>{time(operation.updated_at)}</span>
      </div>
      <Badge
        tone={
          operation.status === 'succeeded'
            ? 'green'
            : ['failed', 'unknown'].includes(operation.status)
              ? 'amber'
              : 'muted'
        }
      >
        {OPERATION_TITLES[operation.status]}
      </Badge>
      <p>
        {operation.completed_steps} / {operation.step_count} ステップ
        {operation.interruption_possible ? ' · 処理中断の可能性あり' : ''}
      </p>
      {operation.error_code && (
        <p>
          {BLOCKED_TITLES[operation.error_code] ??
            'ホストの実行記録を管理者が確認してください。自動再試行はしません。'}
        </p>
      )}
    </li>
  );
}

function Login({
  session,
  onLogin,
  busy,
}: {
  session: ManagerSession;
  onLogin: (token?: string) => void;
  busy: boolean;
}) {
  const [token, setToken] = useState('');
  return (
    <section className="manager-login panel">
      <LockKeyhole size={30} />
      <h2>環境管理に接続</h2>
      <p>操作権限を確認して、2つのPoCを管理します。</p>
      {session.mode === 'cloudflare-access' ? (
        <Button variant="primary" busy={busy} onClick={() => onLogin()}>
          Cloudflare Accessで接続
        </Button>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const selected = token;
            setToken('');
            onLogin(selected);
          }}
        >
          <label htmlFor="manager-token">管理用接続トークン</label>
          <input
            id="manager-token"
            type="password"
            autoComplete="off"
            value={token}
            maxLength={64}
            onChange={(event) => setToken(event.target.value)}
          />
          <Button
            type="submit"
            variant="primary"
            busy={busy}
            disabled={!/^[a-f0-9]{64}$/.test(token)}
          >
            接続
          </Button>
          <small>トークンをブラウザーのストレージへ保存しません。</small>
        </form>
      )}
    </section>
  );
}

export default function EnvironmentManager() {
  const [session, setSession] = useState<ManagerSession | null>(null);
  const [state, setState] = useState<ManagerState | null>(null);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [plan, setPlan] = useState<ManagerPlan | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [interruption, setInterruption] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recoveryKey, setRecoveryKey] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const dialog = useRef<HTMLDialogElement>(null);
  // A synchronous latch closes the double-click gap before React re-renders.
  const submission = useRef(false);
  const refresh = useCallback(async () => {
    const [nextState, history] = await Promise.all([
      managerRequest<unknown>('/api/pocs'),
      managerRequest<unknown>('/api/operations?limit=10'),
    ]);
    setState(parseManagerState(nextState));
    setOperations(parseOperations(history));
  }, []);
  const identify = useCallback(async () => {
    const selected = parseManagerSession(await managerRequest<unknown>('/api/session'));
    setSession(selected);
    if (selected.authenticated) await refresh();
  }, [refresh]);
  useEffect(() => {
    void identify().catch((e: unknown) => setError(message(e)));
  }, [identify]);
  useEffect(() => {
    if (!session?.authenticated) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden')
        void refresh().catch((e: unknown) => setError(message(e)));
    }, 5000);
    const unauthorized = () => {
      setSession(null);
      setState(null);
      setOperations([]);
      setPlan(null);
      void identify().catch((e: unknown) => setError(message(e)));
    };
    window.addEventListener('studio:unauthorized', unauthorized);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('studio:unauthorized', unauthorized);
    };
  }, [session?.authenticated, refresh, identify]);
  useEffect(() => {
    if (!plan) return;
    setConfirmed(false);
    setInterruption(false);
    dialog.current?.showModal();
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.clearInterval(timer);
      dialog.current?.close();
    };
  }, [plan]);
  const login = async (token?: string) => {
    setBusy(true);
    setError(null);
    try {
      await managerRequest(
        '/api/session',
        'POST',
        token === undefined ? { method: 'cloudflare-access' } : { token },
      );
      await identify();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const preview = async (poc: Poc, action: PocAction) => {
    setBusy(true);
    setError(null);
    try {
      const next = parseManagerPlan(
        await managerRequest(`/api/pocs/${poc.poc_id}/plans`, 'POST', { action }),
      );
      if (next.poc_id !== poc.poc_id || next.action !== action)
        throw new ApiError(0, '確認する対象が一致しません。操作を送信していません。');
      setPlan(next);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const execute = async () => {
    if (
      !plan ||
      !confirmed ||
      submission.current ||
      now >= plan.expires_at * 1000 ||
      (plan.requires_interruption_ack && !interruption)
    )
      return;
    submission.current = true;
    const idempotency_key = crypto.randomUUID();
    // Keep the key for GET-only recovery. Never retry a lost mutation response.
    setRecoveryKey(idempotency_key);
    setBusy(true);
    setError(null);
    try {
      const result = await managerRequest<{ operation: unknown }>('/api/operations', 'POST', {
        plan_id: plan.plan_id,
        confirmation: plan.confirmation,
        idempotency_key,
        interruption_ack: interruption,
      });
      const operation = parseOperation(result.operation);
      setOperations((previous) =>
        [operation, ...previous.filter((o) => o.id !== operation.id)].slice(0, 10),
      );
      setRecoveryKey(null);
      setPlan(null);
      await refresh();
    } catch (e) {
      setPlan(null);
      setError(message(e));
    } finally {
      submission.current = false;
      setBusy(false);
    }
  };
  const recover = async () => {
    if (!recoveryKey || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await managerRequest<{ operation: unknown }>(
        `/api/operations/by-key/${recoveryKey}`,
      );
      const operation = parseOperation(result.operation);
      setOperations((previous) =>
        [operation, ...previous.filter((o) => o.id !== operation.id)].slice(0, 10),
      );
      setRecoveryKey(null);
      await refresh();
    } catch {
      setError(
        '受付結果を確定できません。新しい操作を送らず、ホストの管理記録を確認してください。',
      );
    } finally {
      setBusy(false);
    }
  };
  const logout = async () => {
    setBusy(true);
    try {
      await managerRequest('/api/session', 'DELETE');
      const destination = session?.logout_url;
      setSession(null);
      setState(null);
      setOperations([]);
      setPlan(null);
      if (destination === '/cdn-cgi/access/logout') window.location.assign(destination);
      else await identify();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const blocked = busy || !!recoveryKey || operations.some(active);
  return (
    <div className="manager-shell">
      <header className="manager-topbar">
        <a href="/" className="manager-brand">
          <span>
            <Layers3 size={21} />
          </span>
          Lakehouse <strong>CONTROL</strong>
        </a>
        <div>
          <Badge tone={state?.runner_connected ? 'green' : 'muted'} dot>
            {state?.runner_connected ? 'ホスト接続中' : 'ホスト未確認'}
          </Badge>
          {session?.authenticated && (
            <>
              <a href="/docs" className="button button-ghost">
                <BookOpen size={15} />
                API
              </a>
              <Button variant="ghost" onClick={() => void logout()} busy={busy}>
                <LogOut size={15} />
                切断
              </Button>
            </>
          )}
        </div>
      </header>
      <main className="manager-main">
        <PageHeader
          eyebrow="ENVIRONMENT CONTROL"
          title="検証環境を、自在に。"
          description="基盤と将来構想。2つのPoCを独立して切り替え、負荷検証に集中できます。"
          action={
            session?.authenticated ? (
              <Button
                onClick={() => void refresh().catch((e: unknown) => setError(message(e)))}
                busy={busy}
              >
                <RefreshCw size={15} />
                状態を更新
              </Button>
            ) : undefined
          }
        />
        <ErrorNotice message={error} />
        {!session ? (
          <Loading text="接続方式を確認しています" />
        ) : !session.authenticated ? (
          <Login session={session} onLogin={(token) => void login(token)} busy={busy} />
        ) : (
          <>
            <div className="manager-safety">
              <ShieldCheck size={19} />
              <span>データ・ボリュームを保持</span>
              <i /> <span>管理画面と共有SQL Serverは停止対象外</span>
            </div>
            {!state?.runner_connected && (
              <Notice tone="warning">
                ホスト側の管理処理が未接続です。初回の配置・構成登録を管理者が確認すると操作できます。画面から勝手にDockerの権限を付与しません。
              </Notice>
            )}
            {recoveryKey && (
              <Notice tone="warning">
                直前の送信結果を確認中です。重複送信を防ぐため新しい操作は保留しています。
                <Button onClick={() => void recover()} busy={busy}>
                  受付結果だけを確認
                </Button>
              </Notice>
            )}
            <div className="environment-grid">
              {state ? (
                state.pocs.map((poc) => (
                  <EnvironmentCard
                    key={poc.poc_id}
                    poc={poc}
                    state={state}
                    busy={blocked}
                    onPlan={(poc, action) => void preview(poc, action)}
                  />
                ))
              ) : (
                <Loading />
              )}
            </div>
            <Panel title="操作の記録" eyebrow="ACTIVITY" action={<Activity size={18} />}>
              {operations.length ? (
                <ol className="manager-history">
                  {operations.map((operation) => (
                    <OperationRow key={operation.id} operation={operation} />
                  ))}
                </ol>
              ) : (
                <p className="manager-empty">この管理環境では、まだ操作を実行していません。</p>
              )}
            </Panel>
            <div className="manager-notes">
              <CheckCircle2 size={18} />
              <p>
                「起動」は承認済みコンテナの再開です。未導入のコンポーネント、初回のビルド、profile変更はDokployで行います。表示はホストの最新観測に基づき、アプリの業務処理完了を保証しません。
              </p>
            </div>
          </>
        )}
      </main>
      <dialog
        ref={dialog}
        className="lifecycle-dialog manager-dialog"
        onCancel={() => {
          if (!submission.current) setPlan(null);
        }}
      >
        {plan && (
          <>
            <header>
              <Badge tone={plan.action === 'stop' ? 'amber' : 'muted'}>実行前の確認</Badge>
              <h2>
                {POC_TITLES[plan.poc_id]}を{ACTION_TITLES[plan.action]}
              </h2>
              <p>
                現在の構成を再確認し、以下の順序で実行します。有効期限を過ぎた場合は計画を取り直してください。
              </p>
            </header>
            <div className="lifecycle-impact">
              <strong>{plan.steps.length} ステップ</strong>
              <ol>
                {plan.steps.map((step, index) => (
                  <li key={`${step.action}-${step.service}-${index}`}>
                    <span>{ACTION_TITLES[step.action]}</span> <code>{step.service}</code>
                  </li>
                ))}
              </ol>
              {plan.steps.length === 0 && (
                <p>変更はありません。状態が一致していることを記録します。</p>
              )}
            </div>
            <p>
              管理画面・共有SQL
              Server・完了済み初期化処理を維持します。ボリューム削除、再ビルド、構成の作り直しは行いません。
            </p>
            {plan.requires_interruption_ack && (
              <label className="manager-confirm manager-warning">
                <input
                  type="checkbox"
                  checked={interruption}
                  onChange={(e) => setInterruption(e.target.checked)}
                />
                <span>
                  処理中のジョブがある、またはアイドルを証明できません。正常終了を待つ保証はなく、負荷検証中の処理が中断される可能性を承知して停止します。
                </span>
              </label>
            )}
            <label className="manager-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>{POC_TITLES[plan.poc_id]}への操作と影響範囲を確認しました。</span>
            </label>
            {now >= plan.expires_at * 1000 && (
              <Notice tone="warning">計画の有効期限が切れました。閉じて再確認してください。</Notice>
            )}
            <div className="manager-dialog-actions">
              <Button onClick={() => setPlan(null)} disabled={busy}>
                閉じる
              </Button>
              <Button
                variant={plan.action === 'stop' ? 'danger' : 'primary'}
                busy={busy}
                disabled={
                  !confirmed ||
                  (plan.requires_interruption_ack && !interruption) ||
                  now >= plan.expires_at * 1000
                }
                onClick={() => void execute()}
              >
                {ACTION_TITLES[plan.action]}を実行
              </Button>
            </div>
          </>
        )}
      </dialog>
    </div>
  );
}
