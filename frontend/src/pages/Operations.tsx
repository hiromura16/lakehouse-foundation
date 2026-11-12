import { useEffect, useState } from 'react';
import {
  ArrowDownToLine,
  CheckCircle2,
  CirclePlay,
  FilePlus2,
  RefreshCw,
  ShieldCheck,
  Workflow,
} from 'lucide-react';
import { api } from '../api';
import {
  Badge,
  Button,
  Empty,
  ErrorNotice,
  MiniProgress,
  Notice,
  PageHeader,
  Panel,
} from '../components';
import type { Resource } from '../hooks';
import type { AcceptedJob, Job, JobList } from '../types';
import { JOB_LABELS } from './Overview';

const ACTIONS = [
  {
    id: 'ingest',
    title: '原本を取り込む',
    label: 'Landing → Bronze',
    icon: ArrowDownToLine,
    description: '完了マーカーのある素材を検査し、原本・Bronzeへ取り込みます。',
  },
  {
    id: 'medallion',
    title: '業務データへ整える',
    label: 'Bronze → Silver / Gold',
    icon: Workflow,
    description: '登録済みのSQLで、最新版・隔離・業務指標を更新します。',
  },
  {
    id: 'verify',
    title: '合成データで検証する',
    label: 'Synthetic verification',
    icon: ShieldCheck,
    description: '専用の合成fixtureで互換性と再実行を検証します。検証用の書込みを含みます。',
  },
];

export function Operations({ jobs }: { jobs: Resource<JobList> }) {
  const [selected, setSelected] = useState('ingest');
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<AcceptedJob | null>(null);
  const [detail, setDetail] = useState<Job | null>(null);
  const [fileConsent, setFileConsent] = useState(false);
  const [orders, setOrders] = useState(20);
  const [days, setDays] = useState(7);
  const [seed, setSeed] = useState(42);
  useEffect(() => {
    setConsent(false);
  }, [selected]);
  const submit = async (path: string, payload?: unknown) => {
    setBusy(true);
    setError(null);
    setAccepted(null);
    setDetail(null);
    try {
      const result = await api<AcceptedJob>(path, { method: 'POST', payload });
      if (!result?.id || result.status !== 'queued')
        throw new Error('受付結果の形式を確認できません。履歴を確認してください。');
      setAccepted(result);
      setConsent(false);
      setFileConsent(false);
      await jobs.refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '操作を受け付けられませんでした。');
    } finally {
      setBusy(false);
    }
  };
  const readJob = async (id: string) => {
    setError(null);
    try {
      setDetail(await api<Job>('/jobs/' + encodeURIComponent(id)));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'ジョブを確認できません。');
    }
  };
  return (
    <>
      <PageHeader
        eyebrow="OPERATIONS / RUNS"
        title="処理を動かす。結果まで追う。"
        description="素材の生成と後続処理を分け、実行は一つずつ明示的に開始します。"
        action={
          <Button busy={jobs.loading} onClick={() => void jobs.refresh()}>
            <RefreshCw size={15} />
            履歴を更新
          </Button>
        }
      />
      <ErrorNotice message={error} />
      {accepted && (
        <Notice tone="success">
          <strong>受付済み</strong> <code>{accepted.id}</code> —
          処理完了ではありません。下の履歴から結果を確認してください。
        </Notice>
      )}
      <Panel title="パイプライン" eyebrow="EXPLICIT ACTIONS">
        <div className="action-grid">
          {ACTIONS.map((action) => (
            <button
              className={`action-card ${selected === action.id ? 'selected' : ''}`}
              key={action.id}
              onClick={() => setSelected(action.id)}
              aria-pressed={selected === action.id}
            >
              <action.icon size={22} />
              <small>{action.label}</small>
              <strong>{action.title}</strong>
              <p>{action.description}</p>
              {selected === action.id && <CheckCircle2 className="selection-mark" size={17} />}
            </button>
          ))}
        </div>
        <div className="operation-confirm">
          <label className="check-inline">
            <input
              type="checkbox"
              checked={consent}
              onChange={(event) => setConsent(event.target.checked)}
            />
            対象のPoC環境と実行中の処理を確認し、データ・合成検証領域の更新に同意する
          </label>
          <Button
            variant="primary"
            busy={busy}
            disabled={!consent}
            onClick={() => void submit('/pipeline/' + selected)}
          >
            <CirclePlay size={16} />
            選んだ処理を実行
          </Button>
        </div>
      </Panel>
      <Panel
        title="検証用の素材を作る"
        eyebrow="SOURCE ONLY"
        action={<Badge tone="cyan">Landingのみ</Badge>}
      >
        <div className="generator-fields">
          <label className="field">
            指図・受注数
            <input
              type="number"
              min={1}
              max={1000}
              value={orders}
              onChange={(event) =>
                setOrders(Math.max(1, Math.min(1000, Number(event.target.value) || 1)))
              }
            />
          </label>
          <label className="field">
            業務日数
            <input
              type="number"
              min={1}
              max={366}
              value={days}
              onChange={(event) =>
                setDays(Math.max(1, Math.min(366, Number(event.target.value) || 1)))
              }
            />
          </label>
          <label className="field">
            乱数seed
            <input
              type="number"
              min={0}
              max={2147483647}
              value={seed}
              onChange={(event) =>
                setSeed(Math.max(0, Math.min(2147483647, Number(event.target.value) || 0)))
              }
            />
          </label>
        </div>
        <div className="operation-confirm">
          <label className="check-inline">
            <input
              type="checkbox"
              checked={fileConsent}
              onChange={(event) => setFileConsent(event.target.checked)}
            />
            専用Landingへ正常な合成ファイルを1batch作成する
          </label>
          <Button
            busy={busy}
            disabled={!fileConsent}
            onClick={() =>
              void submit('/jobs', {
                mode: 'batch',
                batches: 1,
                interval_seconds: 0,
                seed,
                orders,
                days,
                scenario: 'clean',
                anomalies: [],
                destination: 'landing',
                images: false,
                allow_sql_write: false,
                confirm_shared_source_database: false,
              })
            }
          >
            <FilePlus2 size={15} />
            素材を生成
          </Button>
        </div>
        <div className="panel-footnote">
          素材生成だけではIcebergへ取り込みません。SQL
          Serverへの書込みや、任意SQLの実行機能はこの画面にありません。
        </div>
      </Panel>
      <Panel
        title="最近のジョブ"
        eyebrow="PROCESS-LOCAL HISTORY"
        action={<Badge>最新最大20件</Badge>}
      >
        <ErrorNotice message={jobs.error} retry={() => void jobs.refresh()} />
        {jobs.data?.jobs.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Job ID</th>
                  <th>種別</th>
                  <th>状態</th>
                  <th>完了 / 全体</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {jobs.data.jobs.map((job) => (
                  <tr key={job.id}>
                    <td>
                      <code>{job.id}</code>
                    </td>
                    <td>{job.kind}</td>
                    <td>
                      <Badge
                        tone={
                          job.status === 'succeeded'
                            ? 'green'
                            : job.status === 'failed'
                              ? 'red'
                              : 'muted'
                        }
                      >
                        {JOB_LABELS[job.status] ?? job.status}
                      </Badge>
                    </td>
                    <td>
                      <span>
                        {job.completed} / {job.batches}
                      </span>
                      <MiniProgress value={job.completed} maximum={job.batches} />
                    </td>
                    <td>
                      <Button variant="ghost" onClick={() => void readJob(job.id)}>
                        詳細
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title={jobs.error ? '履歴を取得できません' : '実行履歴はまだありません'}>
            APIの再起動で失われるプロセス内履歴です。データの有無とは別に確認します。
          </Empty>
        )}
        {detail && (
          <div className="job-detail">
            <header>
              <strong>Job {detail.id}</strong>
              <Button variant="ghost" onClick={() => void readJob(detail.id)}>
                <RefreshCw size={14} />
                結果を更新
              </Button>
            </header>
            <Badge
              tone={
                detail.status === 'failed'
                  ? 'red'
                  : detail.status === 'succeeded'
                    ? 'green'
                    : 'cyan'
              }
            >
              {JOB_LABELS[detail.status] ?? detail.status}
            </Badge>
            {detail.error && (
              <Notice tone="warning">
                処理は完了していません。設定・権限・TLSと、実際のデータ状態を確認してください。
              </Notice>
            )}
            <details>
              <summary>返された処理結果</summary>
              <pre>{JSON.stringify(detail.results, null, 2)}</pre>
            </details>
          </div>
        )}
      </Panel>
    </>
  );
}
