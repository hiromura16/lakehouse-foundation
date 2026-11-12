import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Box,
  Check,
  Database,
  GitBranch,
  Layers3,
  Plus,
  RefreshCw,
  Server,
  Table2,
  Workflow,
} from 'lucide-react';
import {
  Badge,
  Button,
  Empty,
  ErrorNotice,
  ExternalLink,
  Loading,
  PageHeader,
  Panel,
  SERVICE_STATES,
} from '../components';
import type { Resource } from '../hooks';
import type { Catalog, JobList, Page, ServiceList } from '../types';

export const JOB_LABELS: Record<string, string> = {
  queued: '受付済み',
  running: '実行中',
  succeeded: '完了',
  failed: '失敗',
  cancelled: '取消済み',
};

export function Overview({
  services,
  catalog,
  jobs,
  navigate,
}: {
  services: Resource<ServiceList>;
  catalog: Resource<Catalog>;
  jobs: Resource<JobList>;
  navigate: (page: Page, filter?: string) => void;
}) {
  const cards = services.data?.services;
  const datasets = catalog.data?.datasets;
  const history = jobs.data?.jobs;
  const available = cards?.filter((item) => item.state === 'available').length;
  const active = history?.filter((item) => ['queued', 'running'].includes(item.status)).length;
  const metrics = [
    {
      label: 'サービスの内部稼働',
      value: available ?? '—',
      suffix: cards ? `/ ${cards.length}` : '',
      note: cards ? '固定healthで確認した現在の状態' : '状態をまだ取得していません',
      icon: Server,
      color: 'cyan',
    },
    {
      label: '登録済みデータ契約',
      value: datasets?.length ?? '—',
      suffix: '',
      note: '行数ではなく、同梱された契約の数',
      icon: Database,
      color: 'green',
    },
    {
      label: '進行中の処理',
      value: active ?? '—',
      suffix: '',
      note: 'このAPIプロセスの直近履歴から',
      icon: Activity,
      color: 'violet',
    },
  ];
  const silver = datasets?.filter((item) => item.layer === 'silver').length;
  const gold = datasets?.filter((item) => item.layer === 'gold').length;
  return (
    <>
      <PageHeader
        eyebrow="WORKSPACE / OVERVIEW"
        title="データの現在地を、ひと目で。"
        description="接続・データ契約・処理の状態を、同じワークスペースで確認します。"
        action={
          <Button variant="primary" onClick={() => navigate('onboarding')}>
            <Plus size={16} />
            データ連携を組み立てる
          </Button>
        }
      />
      <ErrorNotice message={services.error} retry={() => void services.refresh()} />
      <div className="metric-grid">
        {metrics.map((item) => (
          <article className="metric-card" key={item.label}>
            <div className={`metric-icon ${item.color}`}>
              <item.icon size={21} />
            </div>
            <span className="metric-label">{item.label}</span>
            <div className="metric-value">
              {item.value}
              <small>{item.suffix}</small>
            </div>
            <p>{item.note}</p>
          </article>
        ))}
      </div>
      <Panel
        title="Lakehouseのデータフロー"
        eyebrow="FROM SOURCE TO INSIGHT"
        action={<Badge tone="muted">設計上の流れ</Badge>}
      >
        <div className="flow-grid">
          {[
            {
              label: 'Source',
              title: 'ソース',
              detail: '表・列・業務要件を選択',
              icon: GitBranch,
              tone: 'source',
              page: 'onboarding' as Page,
              filter: '',
            },
            {
              label: 'Bronze',
              title: '受領原本',
              detail: '件数は品質確認から取得',
              icon: Box,
              tone: 'bronze',
              page: 'data' as Page,
              filter: 'bronze',
            },
            {
              label: 'Silver',
              title: '整えたデータ',
              detail: silver === undefined ? '契約を未取得' : `${silver} 件の公開契約`,
              icon: Layers3,
              tone: 'silver',
              page: 'data' as Page,
              filter: 'silver',
            },
            {
              label: 'Gold',
              title: '業務の指標',
              detail: gold === undefined ? '契約を未取得' : `${gold} 件の公開契約`,
              icon: Database,
              tone: 'gold',
              page: 'data' as Page,
              filter: 'gold',
            },
            {
              label: 'Explore',
              title: 'BI・AI・分析',
              detail: '各サービスのnative画面へ',
              icon: Table2,
              tone: 'explore',
              page: 'services' as Page,
              filter: '',
            },
          ].map((item, index) => (
            <button
              className={`flow-stage ${item.tone}`}
              key={item.label}
              onClick={() => navigate(item.page, item.filter)}
            >
              <span className="flow-order">0{index + 1}</span>
              <item.icon size={24} />
              <span className="flow-name">{item.label}</span>
              <strong>{item.title}</strong>
              <small>{item.detail}</small>
              <ArrowUpRight className="flow-hover" size={17} />
            </button>
          ))}
        </div>
        <div className="panel-footnote">
          <Check size={14} />
          フローの表示は処理完了の証明ではありません。件数・実行結果はAPIから別に確認します。
        </div>
      </Panel>
      <div className="overview-bottom">
        <Panel
          title="サービスの入口"
          eyebrow="CONNECTED TOOLS"
          action={
            <button className="text-link" onClick={() => navigate('services')}>
              すべて見る
              <ArrowRight size={14} />
            </button>
          }
        >
          {services.loading && !cards ? (
            <Loading />
          ) : !cards?.length ? (
            <Empty title="サービス情報は未取得です">接続を確認して再取得してください。</Empty>
          ) : (
            <div className="service-rows">
              {[...cards]
                .sort((a, b) => Number(b.enabled) - Number(a.enabled))
                .slice(0, 5)
                .map((service) => (
                  <div className="service-row" key={service.id}>
                    <div className={`service-monogram monogram-${service.id}`} aria-hidden="true">
                      {service.title.slice(0, 2)}
                    </div>
                    <div className="service-row-name">
                      <strong>{service.title}</strong>
                      <span>
                        {service.link_mode === 'public' ? '外部公開URL' : 'ローカル / SSH転送'}
                      </span>
                    </div>
                    <Badge tone={SERVICE_STATES[service.state]?.tone ?? 'muted'} dot>
                      {SERVICE_STATES[service.state]?.label ?? '未取得'}
                    </Badge>
                    <ExternalLink service={service} compact />
                  </div>
                ))}
            </div>
          )}
        </Panel>
        <Panel
          title="最近の処理"
          eyebrow="ACTIVITY"
          action={
            <button className="text-link" onClick={() => navigate('operations')}>
              履歴を開く
              <ArrowRight size={14} />
            </button>
          }
        >
          <ErrorNotice message={jobs.error} retry={() => void jobs.refresh()} />
          {jobs.loading && !history ? (
            <Loading />
          ) : history?.length ? (
            <div className="activity-list">
              {history.slice(0, 4).map((job) => (
                <div className="activity-item" key={job.id}>
                  <span className={`activity-mark ${job.status}`}>
                    <Workflow size={16} />
                  </span>
                  <div>
                    <strong>
                      {job.kind === 'pipeline'
                        ? 'パイプライン'
                        : job.kind === 'cdc-source'
                          ? 'CDC素材操作'
                          : '素材生成'}
                    </strong>
                    <code>{job.id}</code>
                  </div>
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
                </div>
              ))}
            </div>
          ) : (
            <Empty title={jobs.error ? '履歴を確認できません' : 'まだ実行履歴がありません'}>
              最初の処理は「運用・ジョブ」から明示的に開始できます。
            </Empty>
          )}
          <div className="panel-footnote">
            最新最大20件・プロセス内の履歴です。再起動後の永続履歴ではありません。
          </div>
        </Panel>
      </div>
    </>
  );
}
