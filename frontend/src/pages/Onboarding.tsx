import { useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  CircleHelp,
  Columns3,
  Database,
  Download,
  FileJson,
  GitBranch,
  KeyRound,
  LockKeyhole,
  Plus,
  ShieldCheck,
  Table2,
  Upload,
} from 'lucide-react';
import { api, downloadJson, importMetadata } from '../api';
import { Badge, Button, Empty, ErrorNotice, Notice, PageHeader, Panel } from '../components';
import type {
  Classification,
  Discovery,
  DiscoveryResult,
  DraftResult,
  MetadataTable,
  ProposalRequest,
  Review,
  ReviewRole,
  TableSelection,
} from '../types';

const CLASSES: Record<Classification, string> = {
  unknown: '未確認',
  public: '公開可能',
  internal: '社内限定',
  sensitive: '機密・個人情報',
  excluded: '対象外',
};
const ROLES: Record<ReviewRole, string> = {
  business: '現場・業務担当',
  technical: 'システム担当',
  dba: 'DBA',
  security: '情報管理担当',
};
const NO_REVIEW: Review = { business: false, technical: false, dba: false, security: false };
const tableId = (table: { schema_name: string; table_name: string }) =>
  `${table.schema_name}.${table.table_name}`;

function selectionFor(table: MetadataTable): TableSelection {
  return {
    schema_name: table.schema_name,
    table_name: table.table_name,
    included_columns: table.columns.map((column) => column.name),
    logical_name: table.table_name,
    business_purpose: '',
    freshness_minutes: 60,
    delete_handling: 'unreviewed',
    column_classifications: {},
    primary_key_confirmed: false,
  };
}
function assertDraft(value: DraftResult): DraftResult {
  if (
    value?.can_apply !== false ||
    value?.applied !== false ||
    value?.approvals_verified !== false ||
    value?.database_checked !== false ||
    value?.review?.verified !== false ||
    !value.contract ||
    typeof value.contract !== 'object' ||
    !Array.isArray(value.blockers) ||
    !Array.isArray(value.warnings) ||
    !/^[0-9a-f]{64}$/.test(value.contract_sha256)
  ) {
    throw new Error(
      '下書きの安全状態を確認できません。正式承認・適用として扱わず、APIの版を確認してください。',
    );
  }
  return value;
}

export function Onboarding() {
  const [step, setStep] = useState(1);
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [drafts, setDrafts] = useState<Record<string, TableSelection>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [active, setActive] = useState('');
  const [sourceId, setSourceId] = useState('configured-demo');
  const [owner, setOwner] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bulkClass, setBulkClass] = useState<Classification>('unknown');
  const [review, setReview] = useState<Review>({ ...NO_REVIEW });
  const [proposal, setProposal] = useState<{ signature: string; result: DraftResult } | null>(null);
  const [plan, setPlan] = useState<{ signature: string; result: DraftResult } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const pending = useRef(0);
  const request = useMemo<ProposalRequest | null>(
    () =>
      discovery
        ? {
            discovery,
            source_id: sourceId,
            owner_team: owner,
            selections: selected.map((key) => drafts[key]).filter(Boolean),
          }
        : null,
    [discovery, sourceId, owner, selected, drafts],
  );
  const signature = JSON.stringify(request);
  const visibleProposal = proposal?.signature === signature ? proposal.result : null;
  const planRequest =
    request && visibleProposal
      ? { ...request, review: { ...review, contract_sha256: visibleProposal.contract_sha256 } }
      : null;
  const planSignature = JSON.stringify(planRequest);
  const visiblePlan = plan?.signature === planSignature ? plan.result : null;
  const tables = discovery?.tables ?? [];
  const current = tables.find((table) => tableId(table) === active);
  const currentDraft = current ? drafts[tableId(current)] : null;

  // Invalidating is an explicit state transition, not just a hash comparison.
  // Editing and then reverting cannot revive a previous download or review.
  const invalidate = () => {
    generation.current += 1;
    setProposal(null);
    setPlan(null);
    setReview({ ...NO_REVIEW });
    setError(null);
  };
  const edit = (id: string, patch: Partial<TableSelection>) => {
    invalidate();
    setDrafts((previous) => ({ ...previous, [id]: { ...previous[id], ...patch } }));
  };
  const load = async (raw?: string) => {
    invalidate();
    const currentGeneration = generation.current;
    pending.current += 1;
    setBusy(true);
    try {
      const result =
        raw === undefined
          ? await api<DiscoveryResult>('/onboarding/example')
          : await importMetadata<DiscoveryResult>(raw);
      if (currentGeneration !== generation.current) return;
      if (
        result.database_checked !== false ||
        !Array.isArray(result.discovery?.tables) ||
        !result.discovery.tables.every(
          (table) =>
            Array.isArray(table.columns) &&
            Array.isArray(table.primary_key) &&
            typeof table.schema_name === 'string' &&
            typeof table.table_name === 'string' &&
            table.columns.every(
              (column) => typeof column.name === 'string' && typeof column.sql_type === 'string',
            ),
        )
      ) {
        throw new Error('metadata応答を確認できません。');
      }
      const value = result.discovery;
      setDiscovery(value);
      setDrafts(
        Object.fromEntries(value.tables.map((table) => [tableId(table), selectionFor(table)])),
      );
      setSelected(value.tables.slice(0, 1).map(tableId));
      setActive(value.tables[0] ? tableId(value.tables[0]) : '');
      setStep(1);
    } catch (failure) {
      if (currentGeneration === generation.current)
        setError(failure instanceof Error ? failure.message : 'metadataを読み込めません。');
    } finally {
      pending.current -= 1;
      setBusy(pending.current > 0);
    }
  };
  const upload = async (file?: File) => {
    if (!file) return;
    invalidate();
    const atStart = generation.current;
    if (file.size > 256 * 1024) {
      setError('metadata JSONは256KiB以下にしてください。');
      if (fileInput.current) fileInput.current.value = '';
      return;
    }
    try {
      const raw = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
      // A newer upload or edit wins, even while the first file is being read.
      if (atStart !== generation.current) return;
      await load(raw);
    } catch {
      if (atStart === generation.current)
        setError('UTF-8のmetadata JSONを確認してください。行データ・資格情報は含めません。');
    } finally {
      if (fileInput.current) fileInput.current.value = '';
    }
  };
  const toggleTable = (id: string, checked: boolean) => {
    if (checked && selected.length >= 6) {
      setError('この試作では最大6表まで選択できます。');
      return;
    }
    invalidate();
    setSelected((previous) =>
      checked ? [...previous, id] : previous.filter((value) => value !== id),
    );
    setActive(id);
  };
  const build = async (kind: 'propose' | 'plan') => {
    const payload = kind === 'plan' ? planRequest : request;
    if (!payload || !selected.length) return;
    const atStart = generation.current;
    const stamp = kind === 'plan' ? planSignature : signature;
    pending.current += 1;
    setBusy(true);
    setError(null);
    try {
      const result = assertDraft(
        await api<DraftResult>('/onboarding/' + kind, { method: 'POST', payload }),
      );
      if (atStart !== generation.current) return;
      if (kind === 'propose') {
        setProposal({ signature: stamp, result });
        setPlan(null);
        setReview({ ...NO_REVIEW });
      } else setPlan({ signature: stamp, result });
    } catch (failure) {
      if (atStart === generation.current)
        setError(failure instanceof Error ? failure.message : '下書きを確認できません。');
    } finally {
      pending.current -= 1;
      setBusy(pending.current > 0);
    }
  };
  const renderFindings = (result: DraftResult) => (
    <div className="finding-list">
      {result.blockers.map((finding, index) => (
        <div className="finding blocking" key={`b-${index}`}>
          <Badge tone="amber">BLOCK</Badge>
          <div>
            <strong>{finding.message}</strong>
            <small>
              {finding.code}
              {finding.table ? ` · ${finding.table}` : ''}
              {finding.column ? ` / ${finding.column}` : ''}
            </small>
          </div>
        </div>
      ))}
      {result.warnings.map((finding, index) => (
        <div className="finding warning" key={`w-${index}`}>
          <Badge tone="muted">WARN</Badge>
          <div>
            <strong>{finding.message}</strong>
            <small>
              {finding.code}
              {finding.table ? ` · ${finding.table}` : ''}
              {finding.column ? ` / ${finding.column}` : ''}
            </small>
          </div>
        </div>
      ))}
    </div>
  );
  return (
    <>
      <PageHeader
        eyebrow="SOURCE / CONTRACT BUILDER"
        title="データ連携を、組み立てる。"
        description="YAMLではなく、表・列と業務上の意味を選択。取り込む前に、人が判断すべきことを見える化します。"
        action={
          <Badge tone="cyan">
            <LockKeyhole size={12} />
            下書き専用
          </Badge>
        }
      />
      <Notice>
        DB接続・CDC適用は行いません。現在は合成metadataまたは取込JSONから、契約案と確認計画を作ります。
      </Notice>
      <ErrorNotice message={error} />
      <nav className="wizard-steps" aria-label="ソース登録の工程">
        {[
          { number: 1, name: '表と列を選ぶ', detail: 'SELECT SOURCE', icon: Table2 },
          { number: 2, name: '業務要件を確認', detail: 'DEFINE INTENT', icon: GitBranch },
          { number: 3, name: '契約と確認計画', detail: 'REVIEW CONTRACT', icon: ShieldCheck },
        ].map((item) => (
          <button
            key={item.number}
            className={step === item.number ? 'active' : step > item.number ? 'done' : ''}
            disabled={!discovery && item.number > 1}
            onClick={() => setStep(item.number)}
          >
            <span className="step-index">
              {step > item.number ? <Check size={16} /> : `0${item.number}`}
            </span>
            <div>
              <small>{item.detail}</small>
              <strong>{item.name}</strong>
            </div>
            <item.icon size={20} />
          </button>
        ))}
      </nav>
      {step === 1 && (
        <>
          <div className="source-actions">
            <div>
              <FileJson size={22} />
              <div>
                <strong>
                  {discovery
                    ? `${tables.length} 表のmetadataを読み込み済み`
                    : 'まず、表の情報を読み込む'}
                </strong>
                <span>
                  {discovery
                    ? discovery.origin === 'synthetic'
                      ? '合成metadata · 実DB未確認'
                      : '取込JSON · 実DB未確認'
                    : 'データの値や接続情報は不要です'}
                </span>
              </div>
            </div>
            <div>
              <input
                className="sr-only"
                ref={fileInput}
                type="file"
                accept=".json,application/json"
                onChange={(event) => void upload(event.target.files?.[0])}
                aria-label="private metadata JSONを選択"
              />
              <Button busy={busy} onClick={() => fileInput.current?.click()}>
                <Upload size={15} />
                JSONを読み込む
              </Button>
              <Button variant="primary" busy={busy} onClick={() => void load()}>
                <Plus size={15} />
                合成6表で試す
              </Button>
            </div>
          </div>
          {!discovery ? (
            <div className="builder-intro">
              <div className="builder-illustration" aria-hidden="true">
                <div>
                  <Database size={28} />
                  <span>Metadata</span>
                </div>
                <ArrowRight size={26} />
                <div>
                  <Columns3 size={28} />
                  <span>Your choices</span>
                </div>
                <ArrowRight size={26} />
                <div>
                  <ShieldCheck size={28} />
                  <span>Contract</span>
                </div>
              </div>
              <h2>技術設定を、人にやさしい選択へ。</h2>
              <p>
                主キー候補や型はmetadataから引き継ぎます。業務上の名前・必要な列・削除の意味は人が確認します。
              </p>
              <div className="intro-checks">
                <span>
                  <CheckCircle2 size={15} />
                  YAML・SQLの手書き不要
                </span>
                <span>
                  <CheckCircle2 size={15} />
                  実DBを変更しない
                </span>
                <span>
                  <CheckCircle2 size={15} />
                  確認事項を自動で整理
                </span>
              </div>
            </div>
          ) : (
            <div className="builder-layout">
              <aside className="builder-table-list">
                <div className="builder-list-title">
                  TABLES <Badge tone="cyan">{selected.length} / 6 選択</Badge>
                </div>
                {tables.map((table) => {
                  const id = tableId(table);
                  const chosen = selected.includes(id);
                  return (
                    <div className={`builder-table-item ${active === id ? 'active' : ''}`} key={id}>
                      <input
                        type="checkbox"
                        checked={chosen}
                        aria-label={`${id}を取込対象にする`}
                        onChange={(event) => toggleTable(id, event.target.checked)}
                      />
                      <button onClick={() => setActive(id)}>
                        <strong>{table.table_name}</strong>
                        <small>
                          {table.schema_name} · {table.columns.length} columns
                        </small>
                      </button>
                      <Table2 size={16} />
                    </div>
                  );
                })}
              </aside>
              {current && currentDraft && (
                <Panel
                  title={current.table_name}
                  eyebrow={current.schema_name}
                  action={
                    <Badge tone={selected.includes(active) ? 'cyan' : 'muted'}>
                      {selected.includes(active) ? '取込対象' : '未選択'}
                    </Badge>
                  }
                >
                  <div className="column-intro">
                    <span>
                      <KeyRound size={14} />
                      主キー候補: <code>{current.primary_key.join(', ') || 'なし'}</code>
                    </span>
                    <span>{currentDraft.included_columns.length} 列を選択</span>
                  </div>
                  <div className="bulk-classification">
                    <label>
                      選択列の情報区分
                      <select
                        value={bulkClass}
                        onChange={(event) => setBulkClass(event.target.value as Classification)}
                      >
                        {Object.entries(CLASSES).map(([value, label]) => (
                          <option value={value} key={value}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <Button
                      disabled={!selected.includes(active)}
                      onClick={() =>
                        edit(active, {
                          column_classifications: {
                            ...currentDraft.column_classifications,
                            ...Object.fromEntries(
                              currentDraft.included_columns.map((name) => [name, bulkClass]),
                            ),
                          },
                        })
                      }
                    >
                      まとめて反映
                    </Button>
                  </div>
                  <div className="table-scroll columns-table">
                    <table>
                      <thead>
                        <tr>
                          <th>取込</th>
                          <th>列</th>
                          <th>SQL型</th>
                          <th>NULL</th>
                          <th>情報区分</th>
                        </tr>
                      </thead>
                      <tbody>
                        {current.columns.map((column) => {
                          const chosen = currentDraft.included_columns.includes(column.name);
                          return (
                            <tr key={column.name} className={!chosen ? 'column-excluded' : ''}>
                              <td>
                                <input
                                  type="checkbox"
                                  aria-label={`${column.name}を取り込む`}
                                  checked={chosen}
                                  disabled={!selected.includes(active)}
                                  onChange={(event) =>
                                    edit(active, {
                                      included_columns: event.target.checked
                                        ? current.columns
                                            .map((item) => item.name)
                                            .filter(
                                              (name) =>
                                                name === column.name ||
                                                currentDraft.included_columns.includes(name),
                                            )
                                        : currentDraft.included_columns.filter(
                                            (name) => name !== column.name,
                                          ),
                                    })
                                  }
                                />
                              </td>
                              <td>
                                <code>{column.name}</code>
                                {current.primary_key.includes(column.name) && (
                                  <KeyRound className="key-icon" size={12} />
                                )}
                              </td>
                              <td>
                                <span className="type-tag">{column.sql_type}</span>
                                {column.computed && <Badge tone="amber">計算列</Badge>}
                              </td>
                              <td>{column.nullable ? '許可' : '不可'}</td>
                              <td>
                                <select
                                  aria-label={`${column.name}の情報区分`}
                                  value={
                                    currentDraft.column_classifications[column.name] ?? 'unknown'
                                  }
                                  disabled={!chosen || !selected.includes(active)}
                                  onChange={(event) =>
                                    edit(active, {
                                      column_classifications: {
                                        ...currentDraft.column_classifications,
                                        [column.name]: event.target.value as Classification,
                                      },
                                    })
                                  }
                                >
                                  {Object.entries(CLASSES).map(([value, label]) => (
                                    <option value={value} key={value}>
                                      {label}
                                    </option>
                                  ))}
                                </select>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  <div className="panel-footnote">
                    <CircleHelp size={14} />
                    一括設定も自己判断の記録です。機密区分を自動で承認するものではありません。
                  </div>
                </Panel>
              )}
            </div>
          )}
        </>
      )}
      {step === 2 && discovery && (
        <div className="requirements-layout">
          <Panel title="連携の基本情報" eyebrow="SOURCE IDENTITY">
            <div className="form-body">
              <label className="field">
                連携の識別名
                <input
                  value={sourceId}
                  maxLength={64}
                  onChange={(event) => {
                    invalidate();
                    setSourceId(event.target.value);
                  }}
                />
                <small>小文字英字から始まる英小文字・数字・_・-</small>
              </label>
              <label className="field">
                責任を持つ部門・チーム
                <input
                  value={owner}
                  maxLength={128}
                  placeholder="このデータの運用責任を持つチーム"
                  onChange={(event) => {
                    invalidate();
                    setOwner(event.target.value);
                  }}
                />
              </label>
              <Notice>必要な鮮度は業務要望の記録です。実際の遅延やSLAを保証しません。</Notice>
            </div>
          </Panel>
          <div>
            {selected.length ? (
              selected.map((id) => {
                const value = drafts[id];
                const table = tables.find((item) => tableId(item) === id)!;
                return (
                  <Panel key={id} title={value.logical_name || table.table_name} eyebrow={id}>
                    <div className="form-body">
                      <div className="form-two">
                        <label className="field">
                          業務上の名前
                          <input
                            value={value.logical_name}
                            maxLength={128}
                            onChange={(event) => edit(id, { logical_name: event.target.value })}
                          />
                        </label>
                        <label className="field">
                          必要な鮮度（分）
                          <input
                            type="number"
                            min={1}
                            max={10080}
                            value={value.freshness_minutes}
                            onChange={(event) =>
                              edit(id, {
                                freshness_minutes: Math.max(
                                  1,
                                  Math.min(10080, Number(event.target.value) || 1),
                                ),
                              })
                            }
                          />
                        </label>
                      </div>
                      <label className="field">
                        このデータを使う目的
                        <input
                          value={value.business_purpose}
                          maxLength={512}
                          placeholder="どの業務・判断に利用しますか？"
                          onChange={(event) => edit(id, { business_purpose: event.target.value })}
                        />
                      </label>
                      <label className="field">
                        元データが削除されたとき
                        <select
                          value={value.delete_handling}
                          onChange={(event) =>
                            edit(id, {
                              delete_handling: event.target
                                .value as TableSelection['delete_handling'],
                            })
                          }
                        >
                          <option value="unreviewed">まだ決めていない</option>
                          <option value="retain">削除前の情報を保持したい</option>
                          <option value="propagate">連携先にも削除を反映したい</option>
                        </select>
                      </label>
                      <label className="check-card">
                        <input
                          type="checkbox"
                          checked={value.primary_key_confirmed}
                          onChange={(event) =>
                            edit(id, { primary_key_confirmed: event.target.checked })
                          }
                        />
                        <span>
                          <strong>主キー候補の安定性を確認した</strong>
                          <small>
                            {table.primary_key.join(', ') || '主キー未検出'} ·
                            この下書きの確認メモです
                          </small>
                        </span>
                      </label>
                    </div>
                  </Panel>
                );
              })
            ) : (
              <Empty title="テーブルが選択されていません" />
            )}
          </div>
        </div>
      )}
      {step === 3 && discovery && (
        <>
          <div className="contract-summary">
            <div>
              <span className="eyebrow">CONTRACT DRAFT</span>
              <h2>{sourceId || '識別名が未入力'}</h2>
              <p>
                {selected.length} 表 ·{' '}
                {selected.reduce((count, id) => count + drafts[id].included_columns.length, 0)}{' '}
                選択列 · {owner || '責任部門が未入力'}
              </p>
            </div>
            <Button
              variant="primary"
              busy={busy}
              disabled={!selected.length}
              onClick={() => void build('propose')}
            >
              <ShieldCheck size={16} />
              契約案を検証する
            </Button>
          </div>
          {!visibleProposal ? (
            <Empty title="現在の入力で契約案を検証してください">
              変更前の契約案・確認メモ・ダウンロードは引き継ぎません。
            </Empty>
          ) : (
            <>
              <Panel
                title="確認が必要なこと"
                eyebrow="VALIDATION"
                action={
                  <Badge tone={visibleProposal.blockers.length ? 'amber' : 'green'}>
                    {visibleProposal.blockers.length} BLOCK / {visibleProposal.warnings.length} WARN
                  </Badge>
                }
              >
                {renderFindings(visibleProposal)}
              </Panel>
              <Panel
                title="役割別の確認メモ"
                eyebrow="REVIEW NOTES"
                action={<Badge tone="muted">正式承認ではありません</Badge>}
              >
                <div className="review-grid">
                  {Object.entries(ROLES).map(([role, title]) => (
                    <label className="check-card" key={role}>
                      <input
                        type="checkbox"
                        checked={review[role as ReviewRole]}
                        onChange={(event) => {
                          generation.current += 1;
                          setPlan(null);
                          setReview((value) => ({ ...value, [role]: event.target.checked }));
                        }}
                      />
                      <span>
                        <strong>{title}</strong>
                        <small>この契約案を確認したという自己申告メモ</small>
                      </span>
                    </label>
                  ))}
                </div>
                <div className="review-footer">
                  <span>
                    契約hash <code>{visibleProposal.contract_sha256.slice(0, 16)}…</code>
                  </span>
                  <Button busy={busy} onClick={() => void build('plan')}>
                    実行しない計画を作る
                    <ArrowRight size={15} />
                  </Button>
                </div>
              </Panel>
              {visiblePlan && (
                <Panel title="次の工程と未実施の確認" eyebrow="NON-EXECUTABLE PLAN">
                  <div className="plan-steps">
                    {visiblePlan.steps?.map((item) => (
                      <div key={item.id}>
                        <span className="plan-dot" />
                        <strong>{item.description}</strong>
                        <Badge tone="muted">{item.status}</Badge>
                      </div>
                    ))}
                  </div>
                  {renderFindings(visiblePlan)}
                  <Notice>
                    正式承認・実DB確認・適用はいずれも未実施です。すべての確認欄を埋めても、DBやCDCを変更することはできません。
                  </Notice>
                </Panel>
              )}
              <div className="export-card">
                <div>
                  <LockKeyhole size={21} />
                  <div>
                    <strong>確認用のJSONをprivateに保存</strong>
                    <span>
                      表名・列名も社内情報になり得ます。保存先の公開範囲は利用者が管理してください。
                    </span>
                  </div>
                </div>
                <div>
                  <Button
                    onClick={() => downloadJson('source-contract-draft.json', visibleProposal)}
                  >
                    <Download size={15} />
                    契約案
                  </Button>
                  {visiblePlan && (
                    <Button
                      onClick={() => downloadJson('source-onboarding-plan.json', visiblePlan)}
                    >
                      <Download size={15} />
                      計画
                    </Button>
                  )}
                </div>
              </div>
              <details className="technical-details">
                <summary>技術担当向けの生成内容</summary>
                <pre>{JSON.stringify(visiblePlan ?? visibleProposal, null, 2)}</pre>
              </details>
            </>
          )}
        </>
      )}
      {discovery && (
        <footer className="wizard-footer">
          <span>編集中の内容はこのブラウザーのメモリだけに保持します。</span>
          <div>
            {step > 1 && (
              <Button onClick={() => setStep((value) => value - 1)}>
                <ArrowLeft size={15} />
                戻る
              </Button>
            )}
            {step < 3 && (
              <Button
                variant="primary"
                disabled={!selected.length}
                onClick={() => setStep((value) => value + 1)}
              >
                {step === 1 ? '業務要件へ' : '契約の確認へ'}
                <ArrowRight size={15} />
              </Button>
            )}
          </div>
        </footer>
      )}
    </>
  );
}
