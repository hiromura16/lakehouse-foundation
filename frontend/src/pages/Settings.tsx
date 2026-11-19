import { useMemo, useState } from 'react';
import { Globe2, KeyRound, LockKeyhole, RefreshCw, Save, Search, Settings2 } from 'lucide-react';
import { api } from '../api';
import { Badge, Button, ErrorNotice, Loading, Notice, PageHeader, Panel } from '../components';
import { useResource } from '../hooks';
import type { SettingsSaved, SettingsView } from '../types';

const LABELS: Record<string, string> = {
  COMPOSE_PROJECT_NAME: '環境の名前',
  DATA_VOLUME_PREFIX: '既存データ名の接頭辞',
  BIND_IP: 'ホスト側の待受アドレス',
  COMPOSE_PROFILES: '利用する追加機能',
  LANDING_DIR: 'ファイルの受け口',
  WORKER_UID: '処理ユーザーID',
  WORKER_GID: '処理グループID',
  PORTAL_LINK_MODE: 'サービスを開く場所',
  SOURCE_SQLSERVER_HOST: '業務参照元のSQL Server',
  SOURCE_SQLSERVER_DATABASE: '参照元のDB名',
  PORTAL_UI_MODE: 'ポータルの画面',
  SOURCE_SQLSERVER_USER: '参照専用ユーザー',
  SOURCE_SQLSERVER_CONTROL_USER: 'チェックポイント専用ユーザー',
  DEMO_SQLSERVER_HOST: '合成データ投入用SQL Server',
  DEMO_SQLSERVER_DATABASE: '投入専用のDB名',
  DEMO_SQLSERVER_USER: '投入用ユーザー',
  HTTP_PROXY: 'HTTP用プロキシ',
  HTTPS_PROXY: 'HTTPS用プロキシ',
  NO_PROXY: 'プロキシを使わない接続先',
  LAKEHOUSE_NO_PROXY: '統合したプロキシ迂回先',
  CORPORATE_CA_FILE: '会社CAのファイル',
  DEMO_GUI_PUBLIC_URL: 'PoCポータルの公開URL',
  DEMO_API_PUBLIC_URL: '操作API / Swaggerの公開URL',
  MCP_PUBLIC_URL: 'MCPの公開URL',
  TRINO_PUBLIC_URL: 'Trinoの公開URL',
  CATALOG_PUBLIC_URL: 'Lakekeeperの公開URL',
  GRAFANA_PUBLIC_URL: 'Grafanaの公開URL',
  PROMETHEUS_PUBLIC_URL: 'Prometheusの公開URL',
  SUPERSET_PUBLIC_URL: 'Supersetの公開URL',
  JUPYTER_PUBLIC_URL: 'JupyterLabの公開URL',
  AIRFLOW_PUBLIC_URL: 'Airflowの公開URL',
  N8N_PUBLIC_URL: 'n8nの公開URL',
  CDC_UI_PUBLIC_URL: 'Kafbatの公開URL',
  QDRANT_PUBLIC_URL: 'Qdrantの公開URL',
  MARQUEZ_UI_PUBLIC_URL: 'Marquezの公開URL',
  OPENOBSERVE_PUBLIC_URL: 'OpenObserveの公開URL',
  SPARK_MASTER_UI_PUBLIC_URL: 'Spark Masterの公開URL',
  SPARK_WORKER_UI_PUBLIC_URL: 'Spark Workerの公開URL',
  SPARK_HISTORY_UI_PUBLIC_URL: 'Spark Historyの公開URL',
};
const CATEGORIES = [
  '基本設定',
  '外部アクセス',
  'SQL Server',
  'プロキシ・接続制限',
  'サービス・配置',
  '資格情報',
];
function category(key: string): string {
  if (key === 'PORTAL_LINK_MODE' || key.endsWith('_PUBLIC_URL')) return '外部アクセス';
  if (key.startsWith('SOURCE_SQLSERVER_') || key.startsWith('DEMO_SQLSERVER_')) return 'SQL Server';
  if (/PROXY$|CA_FILE$|^N8N_SSRF_/.test(key)) return 'プロキシ・接続制限';
  if (
    [
      'COMPOSE_PROJECT_NAME',
      'DATA_VOLUME_PREFIX',
      'BIND_IP',
      'COMPOSE_PROFILES',
      'LANDING_DIR',
      'WORKER_UID',
      'WORKER_GID',
    ].includes(key)
  )
    return '基本設定';
  return 'サービス・配置';
}

export function Settings() {
  const resource = useResource<SettingsView>('/settings');
  const [active, setActive] = useState('外部アクセス');
  const [query, setQuery] = useState('');
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const view = resource.data;
  const changed = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(edits).filter(([key, value]) => value !== view?.values[key]),
      ),
    [edits, view],
  );
  const secretChanges = Object.fromEntries(
    Object.entries(secrets).filter(([key, value]) => key !== 'CONTROL_API_TOKEN' && value !== ''),
  );
  const count = Object.keys(changed).length + Object.keys(secretChanges).length;
  const fields = Object.keys(view?.values ?? {}).filter(
    (key) =>
      category(key) === active &&
      `${LABELS[key] ?? key} ${key}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
  );
  const update = (key: string, value: string) => {
    setEdits((current) => ({ ...current, [key]: value }));
    setSaved(false);
  };
  const save = async () => {
    if (!count || busy) return;
    const values = { ...changed, ...secretChanges };
    // Empty secret inputs preserve values. A new secret is cleared from the
    // rendered form immediately; no existing secret is ever returned here.
    setSecrets({});
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await api<SettingsSaved>('/settings', {
        method: 'PATCH',
        payload: { values },
      });
      if (result.saved !== true || result.applied !== false)
        throw new Error('保存結果を確認できません。設定を再取得してください。');
      setEdits({});
      setSaved(true);
      await resource.refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '設定を保存できません。');
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHeader
        eyebrow="WORKSPACE / SETTINGS"
        title="設定は見やすく、反映は確実に。"
        description="許可された項目だけをprivate設定へ保存します。保存と実環境への反映は別の操作です。"
        action={
          <Button busy={resource.loading} disabled={busy} onClick={() => void resource.refresh()}>
            <RefreshCw size={15} />
            保存値を再取得
          </Button>
        }
      />
      <ErrorNotice message={resource.error} retry={() => void resource.refresh()} />
      <ErrorNotice message={error} />
      {saved && (
        <Notice tone="success">
          private設定へ保存しました。サービスの再配置やTunnel設定は行っていません。
        </Notice>
      )}
      {view?.pending_restart && (
        <Notice tone="warning">
          保存済みの設定は反映待ちです。入口と処理は現在の起動時設定を使い続けています。
        </Notice>
      )}
      {view?.pending_mount_change && (
        <Notice tone="warning">
          保存先やユーザーの変更には、配置側のmount・所有者確認も必要です。この画面ではhostの権限を変更しません。
        </Notice>
      )}
      {resource.loading && !view ? (
        <Loading />
      ) : (
        view && (
          <div className="settings-layout">
            <nav className="settings-nav" aria-label="設定の分類">
              {CATEGORIES.map((item) => (
                <button
                  key={item}
                  className={active === item ? 'active' : ''}
                  onClick={() => {
                    setActive(item);
                    setQuery('');
                  }}
                >
                  {item === '資格情報' ? (
                    <KeyRound size={16} />
                  ) : item === '外部アクセス' ? (
                    <Globe2 size={16} />
                  ) : (
                    <Settings2 size={16} />
                  )}
                  {item}
                  <span>
                    {item === '資格情報'
                      ? view.secret_keys.filter((key) => key !== 'CONTROL_API_TOKEN').length
                      : Object.keys(view.values).filter((key) => category(key) === item).length}
                  </span>
                </button>
              ))}
            </nav>
            <Panel
              title={active}
              eyebrow="PRIVATE CONFIGURATION"
              action={
                <Badge tone="muted">
                  <LockKeyhole size={11} />
                  秘密値は非表示
                </Badge>
              }
            >
              {active === '外部アクセス' && (
                <div className="settings-intro">
                  <h3>どこからサービスを開きますか？</h3>
                  <p>
                    外部から利用する場合は「外部公開URL」を選び、各サービスにHTTPSホスト名を設定します。/docsや/ui/は自動で付きます。
                  </p>
                  <Notice>
                    URLの保存だけでCloudflare
                    Tunnel・Access・DNSや、native製品の公開設定は作成しません。未設定の公開リンクをlocalhostへ戻すこともありません。
                  </Notice>
                </div>
              )}
              <div className="settings-search">
                <label className="search-input">
                  <Search size={15} />
                  <input
                    placeholder="この分類の設定を検索"
                    aria-label="設定を検索"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </label>
              </div>
              <fieldset disabled={busy} className="settings-fields">
                {active === '資格情報' ? (
                  <>
                    <Notice>
                      空欄のまま保存すると、既存の秘密値を保持します。現在の値を表示する機能はありません。接続用のcontrol
                      tokenは管理者のprivate設定側で管理します。
                    </Notice>
                    {view.secret_keys
                      .filter(
                        (key) =>
                          key !== 'CONTROL_API_TOKEN' &&
                          key.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
                      )
                      .map((key) => (
                        <label className="field settings-field" key={key}>
                          <span>
                            {LABELS[key] ?? key}
                            <Badge tone={view.configured_secrets.includes(key) ? 'green' : 'muted'}>
                              {view.configured_secrets.includes(key) ? '設定済み' : '未設定'}
                            </Badge>
                          </span>
                          <input
                            type="password"
                            autoComplete="off"
                            value={secrets[key] ?? ''}
                            placeholder="空欄は現在の値を保持"
                            onChange={(event) =>
                              setSecrets((current) => ({ ...current, [key]: event.target.value }))
                            }
                          />
                          <small>{key}</small>
                        </label>
                      ))}
                  </>
                ) : (
                  fields.map((key) => {
                    const value = edits[key] ?? view.values[key];
                    const isUrl = key.endsWith('_PUBLIC_URL');
                    return (
                      <label
                        className={`field settings-field ${Object.hasOwn(changed, key) ? 'edited' : ''}`}
                        key={key}
                      >
                        <span>
                          {LABELS[key] ?? key}
                          {Object.hasOwn(changed, key) && <Badge tone="cyan">変更あり</Badge>}
                        </span>
                        {key === 'PORTAL_UI_MODE' ? (
                          <select
                            value={value}
                            onChange={(event) => update(key, event.target.value)}
                          >
                            <option value="streamlit">従来の画面（Streamlit）</option>
                            <option value="react">Lakehouse Studio（React試作）</option>
                          </select>
                        ) : key === 'PORTAL_LINK_MODE' ? (
                          <select
                            value={value}
                            onChange={(event) => update(key, event.target.value)}
                          >
                            <option value="local">このPC・SSH転送（localhost）</option>
                            <option value="public">外部公開URL（HTTPS）</option>
                          </select>
                        ) : ['true', 'false'].includes(view.values[key]) ? (
                          <select
                            value={value}
                            onChange={(event) => update(key, event.target.value)}
                          >
                            <option value="true">有効（true）</option>
                            <option value="false">無効（false）</option>
                          </select>
                        ) : (
                          <input
                            type={isUrl ? 'url' : 'text'}
                            value={value}
                            placeholder={isUrl ? 'https://service.example.invalid' : ''}
                            autoComplete="off"
                            onChange={(event) => update(key, event.target.value)}
                          />
                        )}
                        <small>
                          {key}
                          {isUrl ? ' · path・query・認証情報を含めないHTTPS origin' : ''}
                        </small>
                      </label>
                    );
                  })
                )}
              </fieldset>
              <footer className="settings-save">
                <span>{count ? `${count} 項目の変更` : '変更はありません'}</span>
                <div>
                  <Button
                    disabled={!count || busy}
                    onClick={() => {
                      setEdits({});
                      setSecrets({});
                      setError(null);
                    }}
                  >
                    変更を取り消す
                  </Button>
                  <Button
                    variant="primary"
                    busy={busy}
                    disabled={!count}
                    onClick={() => void save()}
                  >
                    <Save size={15} />
                    設定を保存
                  </Button>
                </div>
              </footer>
            </Panel>
          </div>
        )
      )}
    </>
  );
}
