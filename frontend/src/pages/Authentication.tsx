import { useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowRight, KeyRound, ShieldCheck } from 'lucide-react';
import { changeWorkspaceCredential } from '../api.js';
import type { SessionInfo } from '../api.js';
import { Badge, Button, ErrorNotice, Notice, Panel } from '../components.js';

/** Cloudflare mode never renders a fallback key input or stores a JWT. */
export function WorkspaceLoginMethod({
  mode,
  token,
  busy,
  onToken,
}: {
  mode: SessionInfo['mode'];
  token: string;
  busy: boolean;
  onToken: (value: string) => void;
}) {
  const cloudflare = mode === 'cloudflare-access';
  return (
    <>
      {!cloudflare && (
        <label className="field">
          ワークスペース接続キー
          <input
            type="password"
            autoComplete="off"
            value={token}
            disabled={busy}
            onChange={(event) => onToken(event.target.value)}
            placeholder="ワークスペース専用の接続キー"
            required
          />
        </label>
      )}
      <Button
        type="submit"
        variant="primary"
        busy={busy}
        disabled={!cloudflare && !token}
        className="login-submit"
      >
        {cloudflare ? 'Cloudflareでログイン' : '接続する'}
        <ArrowRight size={16} />
      </Button>
    </>
  );
}

/** Kept separate from service credentials and never persisted in browser storage. */
export function WorkspaceAuthentication({ session }: { session: SessionInfo }) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeated, setRepeated] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cloudflare = session.mode === 'cloudflare-access';
  const valid = /^[!-~]{24,128}$/.test(next) && next === repeated && next !== current;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (
      !valid ||
      !current ||
      !confirmed ||
      busy ||
      !session.credential_change_supported ||
      cloudflare
    )
      return;
    const previous = current;
    const replacement = next;
    setCurrent('');
    setNext('');
    setRepeated('');
    setConfirmed(false);
    setBusy(true);
    setError(null);
    try {
      await changeWorkspaceCredential(previous, replacement);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '接続キーの変更を確認できません。');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="ログインと接続キー" eyebrow="WORKSPACE SECURITY" className="authentication-panel">
      <div className="settings-intro">
        <Badge tone={cloudflare ? 'green' : 'cyan'}>
          <ShieldCheck size={12} />
          {cloudflare ? 'Cloudflare Access' : '接続キーで認証'}
        </Badge>
        <h3>{cloudflare ? 'Cloudflare Access' : '接続キーの変更'}</h3>
        <p>
          {cloudflare
            ? 'Cloudflare Accessで認証された本人情報をサーバーで検証します。操作APIのトークンを入力する必要はありません。'
            : 'ブラウザーのログイン用キーです。サーバー間APIの資格情報や、各アプリのパスワードとは別に管理します。'}
        </p>
      </div>
      {cloudflare ? (
        <Notice>
          ログインできるアカウントはCloudflare
          Accessのポリシーで管理します。この画面から認証方式や許可ユーザーを変更しません。
        </Notice>
      ) : !session.credential_change_supported ? (
        <Notice tone="warning">
          このゲートウェイでは接続キー変更が有効になっていません。対応したAPIとゲートウェイの配置を管理者へ確認してください。
        </Notice>
      ) : (
        <form onSubmit={(event) => void submit(event)}>
          <ErrorNotice message={error} />
          <fieldset disabled={busy} className="settings-fields">
            <label className="field">
              現在の接続キー
              <input
                type="password"
                autoComplete="off"
                value={current}
                maxLength={256}
                onChange={(event) => setCurrent(event.target.value)}
                required
              />
            </label>
            <label className="field">
              新しい接続キー
              <input
                type="password"
                autoComplete="off"
                value={next}
                minLength={24}
                maxLength={128}
                onChange={(event) => setNext(event.target.value)}
                required
              />
              <small>
                24〜128文字の半角英数字・記号。空白は使えません。パスワード管理アプリ等で保存してください。
              </small>
            </label>
            <label className="field">
              新しい接続キーを再入力
              <input
                type="password"
                autoComplete="off"
                value={repeated}
                minLength={24}
                maxLength={128}
                onChange={(event) => setRepeated(event.target.value)}
                required
              />
            </label>
            <label className="setup-consent">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              <span>
                変更すると全員のワークスペース接続が終了し、新しいキーでの再接続が必要になることを確認しました。
              </span>
            </label>
          </fieldset>
          <Button
            type="submit"
            variant="primary"
            busy={busy}
            disabled={!valid || !current || !confirmed}
          >
            <KeyRound size={15} />
            接続キーを変更して再接続
          </Button>
          <p className="small-note">
            現在のキーは表示しません。送信時に入力欄を消去し、ブラウザーのstorage・URLへ保存しません。
          </p>
        </form>
      )}
    </Panel>
  );
}
