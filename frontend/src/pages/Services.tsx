import { useMemo, useState } from 'react';
import { ArrowUpRight, Cable, Copy, RefreshCw, Search, Settings2 } from 'lucide-react';
import {
  Badge,
  Button,
  Empty,
  ErrorNotice,
  ExternalLink,
  Loading,
  Notice,
  PageHeader,
  SERVICE_STATES,
  serviceUrl,
} from '../components.js';
import { observedState } from '../lifecycle.js';
import {
  LifecycleConnectionNotice,
  LifecycleOperationNotice,
  LifecycleServiceControls,
  useLifecycle,
} from './ServiceLifecycle.js';
import type { Resource } from '../hooks';
import type { Page, Service, ServiceList } from '../types';

const GROUPS = [
  { id: 'all', label: 'すべて' },
  { id: 'data', label: '保存・クエリ' },
  { id: 'analysis', label: 'BI・分析' },
  { id: 'compute', label: '計算・連携' },
  { id: 'monitoring', label: '監視' },
  { id: 'ai', label: 'AI・操作API' },
];
function groupOf(service: Service): string {
  if (['catalog', 'trino', 'kafbat'].includes(service.id)) return 'data';
  if (['superset', 'jupyter', 'qdrant', 'marquez'].includes(service.id)) return 'analysis';
  if (['prometheus', 'grafana', 'openobserve'].includes(service.id)) return 'monitoring';
  if (['mcp', 'swagger'].includes(service.id)) return 'ai';
  return 'compute';
}

export function Services({
  resource,
  search,
  navigate,
}: {
  resource: Resource<ServiceList>;
  search: string;
  navigate: (page: Page) => void;
}) {
  const [query, setQuery] = useState('');
  const [group, setGroup] = useState('all');
  const [enabledOnly, setEnabledOnly] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState(false);
  const lifecycle = useLifecycle(resource.refresh);
  const additionalServices = (lifecycle.capabilities?.services ?? [])
    .filter(
      (item) =>
        item.group_id && !resource.data?.services.some((service) => service.id === item.service_id),
    )
    .map((item): Service => ({
      id: item.service_id,
      title: item.title,
      description:
        'ブラウザー画面を持たない管理対象です。共有する機能を含む影響対象を確認して操作できます。',
      profile: item.group_id ?? '',
      enabled: item.profile_selected,
      configured: true,
      state: 'not_enabled',
      url: '',
      link_mode: 'public',
      link_configured: false,
      missing_settings: [],
    }));
  const cards = useMemo(
    () =>
      (resource.data?.services ?? []).filter(
        (service) =>
          (group === 'all' || groupOf(service) === group) &&
          (!enabledOnly || service.enabled) &&
          `${search} ${query}`
            .trim()
            .toLocaleLowerCase()
            .split(/\s+/)
            .every((term) =>
              `${service.title} ${service.id} ${service.description}`
                .toLocaleLowerCase()
                .includes(term),
            ),
      ),
    [resource.data, group, enabledOnly, search, query],
  );
  const copyTarget = async (service: Service) => {
    const url = serviceUrl(service);
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(service.id);
      setCopyError(false);
    } catch {
      setCopyError(true);
    }
  };
  return (
    <>
      <PageHeader
        eyebrow="WORKSPACE / SERVICES"
        title="サービス"
        description="稼働状態とアクセスURL。"
        action={
          <Button
            busy={resource.loading}
            onClick={() => {
              void resource.refresh();
              void lifecycle.resource.refresh();
            }}
          >
            <RefreshCw size={15} />
            状態を更新
          </Button>
        }
      />
      <ErrorNotice message={resource.error} retry={() => void resource.refresh()} />
      <LifecycleConnectionNotice
        connected={lifecycle.connected}
        loading={lifecycle.resource.loading}
        failed={!!lifecycle.resource.error}
        capabilities={lifecycle.capabilities}
        onRefresh={() => void lifecycle.resource.refresh()}
      />
      {lifecycle.planError && <Notice tone="warning">{lifecycle.planError}</Notice>}
      <LifecycleOperationNotice
        operation={lifecycle.operation}
        error={lifecycle.operationError}
        recovering={lifecycle.recovering}
        onRefresh={() => void lifecycle.refreshOperation()}
      />
      {resource.data?.pending_restart && (
        <Notice tone="warning">
          保存した設定は反映待ちです。カードは現在の起動時設定を表示しています。
        </Notice>
      )}
      <div className="toolbar">
        <div className="segmented" aria-label="サービスの種類">
          {GROUPS.map((item) => (
            <button
              key={item.id}
              className={group === item.id ? 'active' : ''}
              onClick={() => setGroup(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <label className="search-input">
          <Search size={16} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="サービスを絞り込む"
            aria-label="サービスを絞り込む"
          />
        </label>
      </div>
      <div className="list-meta">
        <span>{resource.data ? `${cards.length} サービス` : '未取得'}</span>
        <label className="check-inline">
          <input
            type="checkbox"
            checked={enabledOnly}
            onChange={(event) => setEnabledOnly(event.target.checked)}
          />
          選択中のprofileだけ
        </label>
      </div>
      {copyError && (
        <Notice tone="warning">
          ブラウザーがコピーを許可しませんでした。表示された接続先を手動でコピーできます。
        </Notice>
      )}
      {resource.loading && !resource.data ? (
        <Loading />
      ) : !cards.length ? (
        <Empty
          title={resource.data ? '該当するサービスがありません' : 'サービス一覧を取得できません'}
        >
          絞込み条件または操作APIとの接続を確認してください。
        </Empty>
      ) : (
        <div className="service-grid">
          {cards.map((service) => {
            const capability = lifecycle.capabilities?.services.find(
              (item) => item.service_id === service.id,
            );
            // Only fresh runner evidence may enable a link for a deselected profile.
            // Configured/public-origin checks still belong to ExternalLink.
            const launchService =
              !lifecycle.resource.error &&
              observedState(lifecycle.capabilities, capability) === 'running'
                ? { ...service, enabled: true }
                : service;
            const state = SERVICE_STATES[service.state] ?? {
              label: '状態未取得',
              tone: 'muted' as const,
            };
            const url = serviceUrl(service);
            return (
              <article
                className={`service-card ${!service.enabled ? 'service-card-inactive' : ''}`}
                key={service.id}
              >
                <div className="service-card-top">
                  <div className={`service-monogram monogram-${service.id}`}>
                    {service.title.slice(0, 2)}
                  </div>
                  <Badge tone={state.tone} dot>
                    {state.label}
                  </Badge>
                </div>
                <h2>{service.title}</h2>
                <p>{service.description}</p>
                <div className="service-card-meta">
                  <span>
                    <Cable size={13} />
                    {service.link_mode === 'public' ? '外部公開URL' : 'ローカル / SSH転送'}
                  </span>
                  <code>{service.profiles?.join(' / ') || service.profile || 'core'}</code>
                </div>
                {service.missing_settings?.length > 0 && (
                  <div className="small-note">必要設定: {service.missing_settings.join(', ')}</div>
                )}
                {service.id === 'mcp' && url && service.enabled && (
                  <div className="endpoint">
                    <code>{url}</code>
                    <button onClick={() => void copyTarget(service)} aria-label="MCP接続先をコピー">
                      <Copy size={14} />
                    </button>
                    {copied === service.id && <span role="status">コピー済み</span>}
                  </div>
                )}
                <LifecycleServiceControls
                  service={service}
                  capabilities={lifecycle.capabilities}
                  capability={capability}
                  busy={lifecycle.busy}
                  connectionError={!!lifecycle.resource.error}
                  onAction={(target, action) => void lifecycle.prepare(target, action)}
                />
                {(!service.enabled ||
                  capability?.blocked_reasons.includes('INITIAL_SETUP_REQUIRED')) && (
                  <button className="text-link" onClick={() => navigate('setup')}>
                    <Settings2 size={14} />
                    初回セットアップの手順を見る
                  </button>
                )}
                <footer>
                  <ExternalLink service={launchService} />
                  {!url && service.link_mode === 'public' ? (
                    <button className="text-link" onClick={() => navigate('settings')}>
                      <Settings2 size={14} />
                      URLを設定
                    </button>
                  ) : (
                    <span className="service-id">{service.id}</span>
                  )}
                </footer>
              </article>
            );
          })}
        </div>
      )}
      {!!additionalServices.length && (
        <section className="lifecycle-additional" aria-labelledby="lifecycle-additional-title">
          <h2 id="lifecycle-additional-title">画面のないサービス</h2>
          <p>RPCなどで利用する機能です。Web画面へのリンクはありません。</p>
          <div className="service-grid">
            {additionalServices.map((service) => (
              <article className="service-card" key={service.id}>
                <h3>{service.title}</h3>
                <p>{service.description}</p>
                <LifecycleServiceControls
                  service={service}
                  capabilities={lifecycle.capabilities}
                  capability={lifecycle.capabilities?.services.find(
                    (item) => item.service_id === service.id,
                  )}
                  busy={lifecycle.busy}
                  connectionError={!!lifecycle.resource.error}
                  onAction={(target, action) => void lifecycle.prepare(target, action)}
                />
                <code className="service-id">{service.id}</code>
                {!service.enabled && (
                  <button className="text-link" onClick={() => navigate('setup')}>
                    <Settings2 size={14} />
                    初回セットアップの手順を見る
                  </button>
                )}
              </article>
            ))}
          </div>
        </section>
      )}
      {lifecycle.dialog}
      <div className="page-footnote">
        <ArrowUpRight size={14} />
        外部URLの設定は、Tunnel・Access・native製品の認証や公開設定を自動作成しません。
      </div>
    </>
  );
}
