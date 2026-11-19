import { useEffect, useState } from 'react';
import { ArrowRight, Database, Eye, KeyRound, RefreshCw, Search, Table2 } from 'lucide-react';
import { api } from '../api';
import {
  Badge,
  Button,
  Empty,
  ErrorNotice,
  Loading,
  Notice,
  PageHeader,
  Panel,
} from '../components';
import type { Resource } from '../hooks';
import type { Catalog, Dataset, Preview, Quality } from '../types';

function cell(value: unknown): string {
  if (value === undefined) return '—（未返却）';
  if (value === null) return 'NULL';
  if (value === '') return '""';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function Data({
  catalog,
  initialLayer,
  search,
}: {
  catalog: Resource<Catalog>;
  initialLayer: string;
  search: string;
}) {
  const [layer, setLayer] = useState(initialLayer || 'all');
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [limit, setLimit] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [qualityBusy, setQualityBusy] = useState(false);
  const [qualityError, setQualityError] = useState<string | null>(null);
  useEffect(() => {
    setLayer(initialLayer || 'all');
  }, [initialLayer]);
  const datasets = (catalog.data?.datasets ?? []).filter(
    (item) =>
      (layer === 'all' || item.layer === layer) &&
      [query, search].every((part) =>
        `${item.title} ${item.table}`.toLocaleLowerCase().includes(part.toLocaleLowerCase()),
      ),
  );
  const selected = datasets.find((item) => item.id === selectedId) ?? datasets[0];
  const previewable = selected && ['production', 'sales'].includes(selected.id);
  const readPreview = async (dataset: Dataset) => {
    setBusy(true);
    setError(null);
    setPreview(null);
    try {
      const result = await api<Preview>(
        `/catalog/preview?dataset=${encodeURIComponent(dataset.id)}&limit=${limit}`,
        { timeout: 35_000 },
      );
      setPreview(result);
      setPreviewId(dataset.id);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'プレビューを確認できません。');
    } finally {
      setBusy(false);
    }
  };
  const readQuality = async () => {
    setQualityBusy(true);
    setQualityError(null);
    try {
      setQuality(await api<Quality>('/quality'));
    } catch (failure) {
      setQualityError(failure instanceof Error ? failure.message : '品質件数を取得できません。');
    } finally {
      setQualityBusy(false);
    }
  };
  return (
    <>
      <PageHeader
        eyebrow="DATA / CATALOG"
        title="データの意味から、確かめる。"
        description="粒度・主キー・列の意味を先に確認。プレビューは固定Goldだけを上限付きで読み取ります。"
        action={
          <Button busy={catalog.loading} onClick={() => void catalog.refresh()}>
            <RefreshCw size={15} />
            契約を更新
          </Button>
        }
      />
      <ErrorNotice message={catalog.error} retry={() => void catalog.refresh()} />
      <div className="toolbar">
        <div className="segmented" aria-label="データの層">
          {['all', 'bronze', 'silver', 'gold'].map((item) => (
            <button
              key={item}
              className={layer === item ? 'active' : ''}
              onClick={() => setLayer(item)}
            >
              {item === 'all' ? 'すべて' : item[0].toUpperCase() + item.slice(1)}
            </button>
          ))}
        </div>
        <label className="search-input">
          <Search size={16} />
          <input
            aria-label="データ契約を検索"
            placeholder="テーブル・指標を検索"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      {catalog.loading && !catalog.data ? (
        <Loading />
      ) : !datasets.length ? (
        <Empty
          title={
            layer === 'bronze'
              ? 'Bronzeの個別契約はこの一覧の対象外です'
              : '一致するデータ契約がありません'
          }
        >
          Bronzeの実件数は下の品質確認から照会できます。未表示をデータ不存在とは判断しません。
        </Empty>
      ) : (
        <div className="catalog-layout">
          <div className="dataset-list">
            {datasets.map((item) => (
              <button
                className={`dataset-card ${selected?.id === item.id ? 'selected' : ''}`}
                key={item.id}
                onClick={() => setSelectedId(item.id)}
              >
                <div>
                  <span className={`layer-dot ${item.layer}`} />
                  <Badge tone={item.layer === 'gold' ? 'amber' : 'cyan'}>
                    {item.layer.toUpperCase()}
                  </Badge>
                </div>
                <strong>{item.title}</strong>
                <code>{item.table}</code>
                <span>
                  {item.columns.length} 列の契約
                  <ArrowRight size={14} />
                </span>
              </button>
            ))}
          </div>
          {selected && (
            <Panel title={selected.title} eyebrow="DATA CONTRACT" action={<Table2 size={20} />}>
              <div className="dataset-detail">
                <code className="table-path">{selected.table}</code>
                <p>{selected.grain}</p>
                <div className="key-line">
                  <KeyRound size={14} />
                  <span>主キー</span>
                  {selected.primary_key.map((key) => (
                    <code key={key}>{key}</code>
                  ))}
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>列</th>
                        <th>型</th>
                        <th>意味</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selected.columns.map((column) => (
                        <tr key={column.name}>
                          <td>
                            <code>{column.name}</code>
                          </td>
                          <td>
                            <span className="type-tag">{column.type}</span>
                          </td>
                          <td>{column.description}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="small-note">由来: {selected.provenance.join(' → ')}</div>
                <div className="preview-toolbar">
                  <label className="inline-field">
                    表示上限
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={limit}
                      onChange={(event) =>
                        setLimit(Math.max(1, Math.min(100, Number(event.target.value) || 1)))
                      }
                    />
                    <span>行 / 表</span>
                  </label>
                  <Button
                    variant="primary"
                    busy={busy}
                    disabled={!previewable}
                    onClick={() => void readPreview(selected)}
                  >
                    <Eye size={15} />
                    Goldを確認
                  </Button>
                </div>
                {!previewable && (
                  <p className="small-note">
                    この契約の行プレビューは未対応です。任意SQLは実行しません。
                  </p>
                )}
                <ErrorNotice message={error} />
                {preview && previewId === selected.id && (
                  <div className="preview-results">
                    {preview.datasets.length ? (
                      preview.datasets.map((item) => (
                        <section key={item.table}>
                          <div className="preview-heading">
                            <Badge tone="green">取得した行</Badge>
                            <span>{item.rows.length} 行</span>
                            <code>snapshot {item.snapshot_id}</code>
                          </div>
                          {!item.rows.length ? (
                            <Empty title="取得結果は0行でした" />
                          ) : (
                            <div className="table-scroll">
                              <table>
                                <thead>
                                  <tr>
                                    {Object.keys(item.rows[0]).map((key) => (
                                      <th key={key}>{key}</th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {item.rows.map((row, index) => (
                                    <tr key={index}>
                                      {Object.keys(item.rows[0]).map((key) => (
                                        <td key={key}>{cell(row[key])}</td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </section>
                      ))
                    ) : (
                      <Empty title="プレビュー対象の応答がありません" />
                    )}
                    <Notice>抜粋の行数です。全件合計や、表間の同時snapshotを保証しません。</Notice>
                  </div>
                )}
              </div>
            </Panel>
          )}
        </div>
      )}
      <Panel
        title="取込み品質の件数"
        eyebrow="BRONZE / SILVER / QUARANTINE"
        action={
          <Button busy={qualityBusy} onClick={() => void readQuality()}>
            <Database size={15} />
            実件数を照会
          </Button>
        }
      >
        <ErrorNotice message={qualityError} />
        {quality ? (
          <>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>ソース</th>
                    <th>Bronze</th>
                    <th>Silver</th>
                    <th>Quarantine</th>
                  </tr>
                </thead>
                <tbody>
                  {quality.datasets.map((item) => (
                    <tr key={item.dataset}>
                      <td>
                        <code>{item.dataset}</code>
                      </td>
                      <td>{item.bronze_rows.toLocaleString()}</td>
                      <td>{item.silver_rows.toLocaleString()}</td>
                      <td>{item.quarantine_rows.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="panel-footnote">{quality.description}</div>
          </>
        ) : (
          <div className="inline-empty">
            まだ照会していません。ボタンを押すまでDBへの件数queryは開始しません。
          </div>
        )}
      </Panel>
    </>
  );
}
