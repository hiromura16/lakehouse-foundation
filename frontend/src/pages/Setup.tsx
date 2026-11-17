import { useEffect, useRef, useState } from 'react';
import {
  ArrowRight,
  Check,
  Download,
  Layers3,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react';
import { downloadJson } from '../api.js';
import {
  Badge,
  Button,
  Empty,
  ErrorNotice,
  Loading,
  Notice,
  PageHeader,
  Panel,
} from '../components.js';
import { useResource } from '../hooks.js';
import { prepareSetup } from '../setup.js';
import type { SetupComponent, SetupPlan, SetupView } from '../setup.js';
import type { Page } from '../types.js';

const STATES = {
  ready_for_operator_review: '設定は準備済み',
  configuration_required: '設定の追加が必要',
  selected: '起動対象に選択済み',
  operator_setup_required: '初回準備が必要',
};
const KINDS = {
  configuration: '設定',
  host: '保存領域',
  build: 'イメージ',
  bootstrap: '初期化',
  validation: '動作確認',
  access: 'ブラウザーの入口',
};

/** An actionable guide, without claiming that a configuration flag ran Docker. */
export function SetupCard({
  component,
  onPrepare,
  busy,
}: {
  component: SetupComponent;
  onPrepare: () => void;
  busy: boolean;
}) {
  return (
    <article className="service-card setup-card">
      <div className="service-card-top">
        <div className="service-monogram">
          <Layers3 size={23} />
        </div>
        <Badge tone={component.configured ? 'cyan' : 'amber'}>{STATES[component.state]}</Badge>
      </div>
      <h2>{component.title}</h2>
      <p>{component.description}</p>
      <div className="setup-facts">
        <span>
          <Check size={14} />
          {component.management_supported
            ? '初回準備後はGUIで起動・停止'
            : '初回準備と起動は管理者が実施'}
        </span>
        <span>
          <ShieldCheck size={14} />
          {component.external_link_configured ? '外部URLは設定済み' : '外部から開く入口は別途準備'}
        </span>
      </div>
      {!!component.missing_settings.length && (
        <div className="small-note">不足する設定: {component.missing_settings.join(', ')}</div>
      )}
      <footer>
        <code>{component.profile}</code>
        <Button disabled={busy} onClick={onPrepare}>
          準備を始める
          <ArrowRight size={14} />
        </Button>
      </footer>
    </article>
  );
}

export function SetupChecklist({ plan }: { plan: SetupPlan }) {
  return (
    <>
      <div className="setup-profile-change">
        <div>
          <span>現在の起動対象設定</span>
          <code>{plan.profiles_before.join(', ') || 'core'}</code>
        </div>
        <ArrowRight size={18} />
        <div>
          <span>追加後の候補</span>
          <code>{plan.profiles_after.join(', ')}</code>
        </div>
      </div>
      {!!plan.settings_to_review.length && (
        <Notice>
          設定画面で確認する項目: {plan.settings_to_review.join(', ')}
          。秘密値そのものは計画に含めません。
        </Notice>
      )}
      <ol className="setup-steps">
        {plan.steps.map((step, index) => (
          <li key={step.id}>
            <span className="setup-step-number">{index + 1}</span>
            <div>
              <Badge tone="muted">{KINDS[step.kind]}</Badge>
              <h3>{step.title}</h3>
              <p>{step.description}</p>
            </div>
          </li>
        ))}
      </ol>
      <Notice tone="warning">{plan.notice}</Notice>
    </>
  );
}

export function Setup({ navigate }: { navigate: (page: Page) => void }) {
  const resource = useResource<SetupView>('/setup');
  const [plan, setPlan] = useState<SetupPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const latest = useRef(0);
  const planHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (plan) planHeading.current?.focus();
  }, [plan]);
  useEffect(
    () => () => {
      latest.current += 1;
    },
    [],
  );
  const prepare = async (component: SetupComponent) => {
    if (busy) return;
    const generation = ++latest.current;
    setBusy(true);
    setError(null);
    setConfirmed(false);
    setPlan(null);
    try {
      const result = await prepareSetup(component.id);
      if (latest.current === generation) setPlan(result);
    } catch (failure) {
      if (latest.current === generation)
        setError(failure instanceof Error ? failure.message : '準備計画を取得できません。');
    } finally {
      if (latest.current === generation) setBusy(false);
    }
  };
  return (
    <>
      <PageHeader
        eyebrow="MANAGE / GUIDED SETUP"
        title="セットアップ"
        description="機能ごとの設定・保存領域・初期配置を確認します。"
        action={
          <Button busy={resource.loading} disabled={busy} onClick={() => void resource.refresh()}>
            <RefreshCw size={15} />
            準備状況を再取得
          </Button>
        }
      />
      <div className="setup-intro">
        <Sparkles size={26} />
        <div>
          <h2>初期セットアップ</h2>
          <p>
            この画面は準備計画を作ります。起動対象設定や秘密値の保存、イメージ取得、ホストの権限変更、コンテナ起動はまだ行いません。
          </p>
        </div>
      </div>
      <ErrorNotice message={resource.error} retry={() => void resource.refresh()} />
      <ErrorNotice message={error} />
      {resource.data?.pending_restart && (
        <Notice tone="warning">
          保存済みの設定に反映待ちがあります。実際の稼働状態は「サービス」で確認してください。
        </Notice>
      )}
      {busy && <Loading text="この機能の準備手順を確認しています" />}
      {resource.loading && !resource.data ? (
        <Loading />
      ) : (
        resource.data && (
          <>
            <Panel
              title="追加できる機能"
              eyebrow="OPTIONAL COMPONENTS"
              action={<Badge tone="muted">初回配置は管理者が確認</Badge>}
            >
              <p>
                「設定は準備済み」は資格情報等の設定状態です。イメージのビルド、稼働、外部接続の成功を表しません。
              </p>
            </Panel>
            <div className="service-grid setup-grid">
              {resource.data.components.map((component) => (
                <SetupCard
                  component={component}
                  key={component.id}
                  onPrepare={() => void prepare(component)}
                  busy={busy}
                />
              ))}
            </div>
            {!resource.data.components.length && (
              <Empty title="追加機能を取得できませんでした">
                APIと画面の版を確認してください。
              </Empty>
            )}
          </>
        )
      )}
      {plan && (
        <section className="setup-plan" aria-labelledby="setup-plan-title">
          <div className="setup-plan-heading">
            <div>
              <span className="eyebrow">PREPARATION CHECKLIST</span>
              <h2 id="setup-plan-title" ref={planHeading} tabIndex={-1}>
                {plan.title}を準備する
              </h2>
            </div>
            <button
              className="icon-button"
              onClick={() => setPlan(null)}
              aria-label="準備計画を閉じる"
            >
              <X size={18} />
            </button>
          </div>
          <SetupChecklist plan={plan} />
          <label className="setup-consent">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>
              これは準備計画です。管理者による初回配置・動作確認・管理対象への追加が必要なことを確認しました。
            </span>
          </label>
          <div className="setup-plan-actions">
            <Button onClick={() => navigate('settings')}>
              <Settings2 size={15} />
              必要な設定を確認
            </Button>
            <Button
              disabled={!confirmed}
              onClick={() => downloadJson(`lakehouse-setup-${plan.component_id}.json`, plan)}
            >
              <Download size={15} />
              管理者用チェックリストを保存
            </Button>
            <Button onClick={() => navigate('services')}>
              サービスの状態を見る
              <ArrowRight size={15} />
            </Button>
          </div>
          <p className="small-note">
            保存するJSONは設定名と手順のみです。手順の完了や配置の承認を証明するものではありません。
          </p>
        </section>
      )}
    </>
  );
}
