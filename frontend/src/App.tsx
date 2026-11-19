import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  Activity,
  ArrowRight,
  BookOpen,
  Cable,
  ChevronRight,
  CircleHelp,
  Database,
  GitBranch,
  Layers3,
  LayoutDashboard,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Menu,
  Search,
  Settings2,
  ShieldCheck,
  Workflow,
  X,
} from 'lucide-react';
import { getSession, login, logout } from './api';
import { Badge, Button, ErrorNotice } from './components';
import { useResource } from './hooks';
import type { Catalog, JobList, Page, ServiceList } from './types';
import { Data } from './pages/Data';
import { Onboarding } from './pages/Onboarding';
import { Operations } from './pages/Operations';
import { Overview } from './pages/Overview';
import { Services } from './pages/Services';
import { Settings } from './pages/Settings';

const NAV = [
  { id: 'overview' as Page, label: 'ワークスペース', icon: LayoutDashboard, group: 'workspace' },
  { id: 'services' as Page, label: 'サービス', icon: Cable, group: 'workspace' },
  { id: 'data' as Page, label: 'データカタログ', icon: Database, group: 'workspace' },
  { id: 'onboarding' as Page, label: 'データ連携を組む', icon: GitBranch, group: 'workspace' },
  { id: 'operations' as Page, label: '運用・ジョブ', icon: Workflow, group: 'manage' },
  { id: 'settings' as Page, label: '設定', icon: Settings2, group: 'manage' },
];

function Logo({ large = false }: { large?: boolean }) {
  return (
    <span className={`brand-mark ${large ? 'brand-mark-large' : ''}`} aria-hidden="true">
      <Layers3 size={large ? 32 : 21} strokeWidth={1.8} />
    </span>
  );
}

function Login({
  onAuthenticated,
  initialError,
}: {
  onAuthenticated: () => void;
  initialError: string | null;
}) {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!token || busy) return;
    const submitted = token;
    // Keep the initial input ephemeral. It never enters localStorage,
    // sessionStorage, URLs, console output, or a global state container.
    setToken('');
    setBusy(true);
    setError(null);
    try {
      if (!(await login(submitted)))
        throw new Error('認証を確認できませんでした。接続tokenを確認してください。');
      onAuthenticated();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '接続を確認できません。');
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login-screen">
      <div className="login-brand">
        <Logo />
        <span>
          Lakehouse <strong>Studio</strong>
        </span>
        <Badge tone="cyan">PREVIEW</Badge>
      </div>
      <section className="login-intro">
        <span className="eyebrow">YOUR DATA. ONE WORKSPACE.</span>
        <h1>
          データのつながりを、
          <br />
          <em>もっと自由に。</em>
        </h1>
        <p>
          ソースの確認から契約の組立て、処理と運用まで。
          <br />
          人が判断するための、ひとつのコントロールプレーン。
        </p>
        <div className="login-flow" aria-hidden="true">
          <div>
            <Database size={27} />
            <span>Source</span>
          </div>
          <i />
          <div>
            <GitBranch size={27} />
            <span>Contract</span>
          </div>
          <i />
          <div>
            <Layers3 size={27} />
            <span>Lakehouse</span>
          </div>
        </div>
        <div className="login-values">
          <span>
            <ShieldCheck size={16} />
            人が確認・承認する設計
          </span>
          <span>
            <Activity size={16} />
            実際の状態だけを表示
          </span>
        </div>
      </section>
      <section className="login-card">
        <div className="login-lock">
          <LockKeyhole size={22} />
        </div>
        <span className="eyebrow">OWNER CONNECTION</span>
        <h2>ワークスペースへ接続</h2>
        <p>管理者から受け取った操作APIの接続tokenを入力してください。</p>
        <ErrorNotice message={error} />
        <form onSubmit={(event) => void submit(event)}>
          <label className="field">
            接続token
            <input
              type="password"
              autoComplete="off"
              value={token}
              disabled={busy}
              onChange={(event) => setToken(event.target.value)}
              placeholder="操作APIの接続token"
              required
            />
          </label>
          <Button
            type="submit"
            variant="primary"
            busy={busy}
            disabled={!token}
            className="login-submit"
          >
            接続する
            <ArrowRight size={16} />
          </Button>
        </form>
        <div className="login-security">
          <ShieldCheck size={16} />
          <span>
            入力値は送信直後にクリアします。以後はHttpOnlyのsessionで接続し、ブラウザーのstorageにtokenを保存しません。
          </span>
        </div>
        <details>
          <summary>
            <CircleHelp size={14} />
            接続方法について
          </summary>
          <p>
            個人のSQL Server
            passwordやDBA資格情報は入力しません。接続tokenは管理者のprivate設定で管理します。sessionはgatewayの再起動や期限切れで終了します。
          </p>
        </details>
      </section>
      <footer className="login-footer">
        LAKEHOUSE STUDIO <span>METADATA-DRIVEN / HUMAN-GUIDED</span>
      </footer>
    </main>
  );
}

function Workspace({ onLogout }: { onLogout: () => void }) {
  const initial = window.location.hash.slice(1);
  const [page, setPage] = useState<Page>(
    NAV.some((item) => item.id === initial) ? (initial as Page) : 'overview',
  );
  const [dataLayer, setDataLayer] = useState('');
  const [search, setSearch] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const [menu, setMenu] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const services = useResource<ServiceList>('/services');
  const catalog = useResource<Catalog>('/catalog');
  const jobs = useResource<JobList>('/jobs?limit=20');
  const navigate = (next: Page, filter = '') => {
    setPage(next);
    setDataLayer(next === 'data' ? filter : '');
    setSearch('');
    setSearchOpen(false);
    setMenu(false);
    window.history.replaceState(null, '', '#' + next);
    window.requestAnimationFrame(() => document.getElementById('workspace-main')?.focus());
  };
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        input.current?.focus();
      }
      if (event.key === 'Escape') {
        setMenu(false);
        setShowHelp(false);
        setSearchOpen(false);
        input.current?.blur();
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, []);
  const signOut = async () => {
    setLogoutBusy(true);
    setLogoutError(null);
    try {
      await logout();
      onLogout();
    } catch (failure) {
      setLogoutError(failure instanceof Error ? failure.message : 'session終了を確認できません。');
    } finally {
      setLogoutBusy(false);
    }
  };
  const active = NAV.find((item) => item.id === page)!;
  const searchHits = search.trim()
    ? [
        ...NAV.filter((item) => item.label.includes(search)).map((item) => ({
          id: item.id,
          title: item.label,
          description: '操作画面',
          target: item.id,
        })),
        ...(services.data?.services ?? [])
          .filter((item) =>
            `${item.title} ${item.id}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
          )
          .slice(0, 4)
          .map((item) => ({
            id: item.id,
            title: item.title,
            description: 'サービス',
            target: 'services' as Page,
          })),
        ...(catalog.data?.datasets ?? [])
          .filter((item) =>
            `${item.title} ${item.table}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
          )
          .slice(0, 3)
          .map((item) => ({
            id: item.id,
            title: item.title,
            description: 'データ契約',
            target: 'data' as Page,
          })),
      ]
    : [];
  const refreshed = services.fetchedAt?.toLocaleTimeString('ja-JP', {
    hour: '2-digit',
    minute: '2-digit',
  });
  return (
    <div className="studio-shell">
      <a className="skip-link" href="#workspace-main">
        本文へ移動
      </a>
      {menu && (
        <button
          className="mobile-scrim"
          onClick={() => setMenu(false)}
          aria-label="メニューを閉じる"
        />
      )}
      <aside className={`sidebar ${menu ? 'open' : ''}`} aria-label="メインナビゲーション">
        <button className="sidebar-brand" onClick={() => navigate('overview')}>
          <Logo />
          <span>
            Lakehouse<strong>Studio</strong>
          </span>
          <small>POC</small>
        </button>
        <div className="workspace-switch">
          <div className="workspace-avatar">LF</div>
          <div>
            <strong>Foundation</strong>
            <span>実験ワークスペース</span>
          </div>
          <ChevronRight size={15} />
        </div>
        <div className="sidebar-label">WORKSPACE</div>
        <nav>
          {NAV.filter((item) => item.group === 'workspace').map((item) => (
            <button
              key={item.id}
              className={`nav-item ${page === item.id ? 'active' : ''}`}
              onClick={() => navigate(item.id)}
              aria-current={page === item.id ? 'page' : undefined}
            >
              <item.icon size={18} />
              <span>{item.label}</span>
              {item.id === 'onboarding' && <small>NEW</small>}
            </button>
          ))}
        </nav>
        <div className="sidebar-label manage-label">MANAGE</div>
        <nav>
          {NAV.filter((item) => item.group === 'manage').map((item) => (
            <button
              key={item.id}
              className={`nav-item ${page === item.id ? 'active' : ''}`}
              onClick={() => navigate(item.id)}
              aria-current={page === item.id ? 'page' : undefined}
            >
              <item.icon size={18} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <GitBranch size={18} />
            <strong>Build with metadata.</strong>
            <p>検出した情報から、人が確認するデータ契約へ。</p>
            <button onClick={() => navigate('onboarding')}>
              連携を組み立てる
              <ArrowRight size={14} />
            </button>
          </div>
          <div className="sidebar-session">
            <span className="session-avatar">
              <ShieldCheck size={17} />
            </span>
            <div>
              <strong>認証済みsession</strong>
              <small>owner connection</small>
            </div>
            <button aria-label="sessionを終了" disabled={logoutBusy} onClick={() => void signOut()}>
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      <div className="workspace-area">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu"
              aria-label="メニューを開く"
              aria-expanded={menu}
              onClick={() => setMenu(!menu)}
            >
              <Menu size={20} />
            </button>
            <span>Foundation</span>
            <ChevronRight size={13} />
            <strong>{active.label}</strong>
          </div>
          <div className="global-search">
            <Search size={16} />
            <input
              ref={input}
              aria-label="ワークスペースを検索"
              placeholder="サービス・データを検索"
              value={search}
              onFocus={() => setSearchOpen(true)}
              onChange={(event) => {
                setSearch(event.target.value);
                setSearchOpen(true);
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && searchHits.length) {
                  const term = search;
                  navigate(searchHits[0].target);
                  if (searchHits[0].description !== '操作画面') setSearch(term);
                  input.current?.blur();
                }
              }}
            />
            {search ? (
              <button
                className="search-clear"
                aria-label="検索条件をクリア"
                onClick={() => {
                  setSearch('');
                  setSearchOpen(false);
                }}
              >
                <X size={13} />
              </button>
            ) : (
              <kbd>⌘ K</kbd>
            )}
            {search && searchOpen && (
              <div className="search-popover">
                <span className="eyebrow">WORKSPACE SEARCH</span>
                {searchHits.length ? (
                  searchHits.map((hit, index) => (
                    <button
                      key={`${hit.id}-${index}`}
                      onClick={() => {
                        const term = search;
                        navigate(hit.target);
                        if (hit.description !== '操作画面') setSearch(term);
                      }}
                    >
                      <Search size={14} />
                      <div>
                        <strong>{hit.title}</strong>
                        <small>{hit.description}</small>
                      </div>
                      <ArrowRight size={13} />
                    </button>
                  ))
                ) : (
                  <p>一致する項目がありません。</p>
                )}
              </div>
            )}
          </div>
          <div className="topbar-actions">
            <Badge tone={services.error ? 'amber' : services.data ? 'green' : 'muted'} dot>
              {services.error
                ? '取得エラー'
                : services.loading
                  ? '取得中'
                  : services.data
                    ? 'API接続済み'
                    : '未取得'}
            </Badge>
            <button
              className="icon-button"
              aria-label="画面の使い方"
              onClick={() => setShowHelp(!showHelp)}
            >
              <CircleHelp size={19} />
            </button>
          </div>
        </header>
        <main id="workspace-main" className="workspace-main" tabIndex={-1}>
          <ErrorNotice message={logoutError} />
          {showHelp && (
            <div className="help-banner">
              <BookOpen size={20} />
              <div>
                <strong>状態と、操作の結果を分けて確認します。</strong>
                <p>
                  サービスの緑表示は内部healthです。外部URL・製品認証・業務データの正しさは別の確認です。ソース登録は下書きだけを作り、DBやCDCを変更しません。
                </p>
              </div>
              <button aria-label="使い方を閉じる" onClick={() => setShowHelp(false)}>
                <X size={17} />
              </button>
            </div>
          )}
          {page === 'overview' && (
            <Overview services={services} catalog={catalog} jobs={jobs} navigate={navigate} />
          )}
          {page === 'services' && (
            <Services resource={services} search={search} navigate={navigate} />
          )}
          {page === 'data' && <Data catalog={catalog} initialLayer={dataLayer} search={search} />}
          {page === 'operations' && <Operations jobs={jobs} />}
          {page === 'settings' && <Settings />}
          {/* Keep a draft across navigation, but unmount everything on logout. */}
          <div hidden={page !== 'onboarding'}>
            <Onboarding />
          </div>
          <footer className="workspace-footer">
            <span>
              <Logo />
              Lakehouse Studio <b>PREVIEW</b>
            </span>
            <span>
              {refreshed ? `サービス最終取得 ${refreshed}` : 'サービス未取得'}
              <i />
              実APIの応答を表示
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void getSession()
      .then((value) => {
        if (active) setAuthenticated(value);
      })
      .catch((failure) => {
        if (active) {
          setAuthenticated(false);
          setError(failure instanceof Error ? failure.message : 'sessionを確認できません。');
        }
      });
    const expired = () => {
      setAuthenticated(false);
      setError('sessionの期限が切れました。もう一度接続してください。');
    };
    window.addEventListener('studio:unauthorized', expired);
    return () => {
      active = false;
      window.removeEventListener('studio:unauthorized', expired);
    };
  }, []);
  if (authenticated === null)
    return (
      <div className="boot-screen" role="status">
        <Logo large />
        <LoaderCircle className="spin" size={21} />
        <span>ワークスペースを確認しています</span>
      </div>
    );
  if (!authenticated)
    return (
      <Login
        initialError={error}
        onAuthenticated={() => {
          setAuthenticated(true);
          setError(null);
        }}
      />
    );
  return (
    <Workspace
      onLogout={() => {
        setAuthenticated(false);
        setError(null);
      }}
    />
  );
}
