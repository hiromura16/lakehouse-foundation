/** Render the real shared UI without a browser, live server, or fabricated data. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  Badge,
  Button,
  ErrorNotice,
  ExternalLink,
  MiniProgress,
  serviceUrl,
} from '../src/components.js';
import type { Service } from '../src/types.js';
import { WorkspaceAuthentication, WorkspaceLoginMethod } from '../src/pages/Authentication.js';
import { SetupCard, SetupChecklist } from '../src/pages/Setup.js';
import type { SetupComponent, SetupPlan } from '../src/setup.js';

const service: Service = {
  id: 'grafana',
  title: 'Grafana',
  description: 'Synthetic service contract',
  profile: 'monitoring',
  enabled: true,
  configured: true,
  state: 'available',
  url: 'https://grafana.example.invalid/',
  link_mode: 'public',
  link_configured: true,
  missing_settings: [],
};

test('configured external links keep the public origin and isolate the opener', () => {
  const html = renderToStaticMarkup(<ExternalLink service={service} />);
  assert.match(html, /href="https:\/\/grafana\.example\.invalid\/"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /target="_blank"/);
});

test('an unconfigured public URL renders no anchor and never falls back locally', () => {
  const html = renderToStaticMarkup(
    <ExternalLink service={{ ...service, url: '', link_configured: false }} />,
  );
  assert.match(html, /外部URL未設定/);
  assert.doesNotMatch(html, /href=|localhost/);
});

test('disabled profiles and incomplete service settings cannot become active links', () => {
  for (const values of [{ enabled: false }, { configured: false }]) {
    const html = renderToStaticMarkup(<ExternalLink service={{ ...service, ...values }} />);
    assert.match(html, /aria-disabled="true"/);
    assert.doesNotMatch(html, /href=/);
  }
});

test('unsafe or local public destinations cannot render clickable links', () => {
  for (const url of [
    'javascript:alert(1)',
    'http://localhost:3000/',
    'https://localhost/',
    'https://127.0.0.1/',
    'https://[::1]/',
    'https://user@example.invalid/',
  ]) {
    const value = { ...service, url };
    assert.equal(serviceUrl(value), null);
    assert.doesNotMatch(renderToStaticMarkup(<ExternalLink service={value} />), /href=/);
  }
});

test('explicit local mode retains an operator-selected localhost link', () => {
  assert.equal(
    serviceUrl({ ...service, link_mode: 'local', url: 'http://localhost:3000/' }),
    'http://localhost:3000/',
  );
});

test('busy actions are disabled and the loading icon is accessible decoration', () => {
  const html = renderToStaticMarkup(<Button busy>処理を実行</Button>);
  assert.match(html, /disabled=""/);
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /処理を実行/);
});

test('untrusted text is escaped rather than inserted as active HTML', () => {
  const text = '<img src=x onerror=alert(1)>';
  const html = renderToStaticMarkup(
    <>
      <Badge>{text}</Badge>
      <ErrorNotice message={text} />
    </>,
  );
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /role="alert"/);
});

test('progress uses a bounded native element without CSP-unsafe inline CSS', () => {
  const html = renderToStaticMarkup(
    <>
      <MiniProgress value={10} maximum={0} />
      <MiniProgress value={999} maximum={1} />
    </>,
  );
  assert.match(html, /value="0"/);
  assert.match(html, /value="100"/);
  assert.doesNotMatch(html, /style=/);
});

test('Cloudflare login has no fallback key input or manual API credential prompt', () => {
  const html = renderToStaticMarkup(
    <WorkspaceLoginMethod
      mode="cloudflare-access"
      token=""
      busy={false}
      onToken={() => undefined}
    />,
  );
  assert.match(html, /Cloudflareでログイン/);
  assert.doesNotMatch(html, /<input|type="password"|CONTROL_API_TOKEN/);
});

test('local workspace login stays ephemeral and disabled until a key is provided', () => {
  const html = renderToStaticMarkup(
    <WorkspaceLoginMethod mode="token" token="" busy={false} onToken={() => undefined} />,
  );
  assert.match(html, /ワークスペース接続キー/);
  assert.match(html, /type="password"/);
  assert.match(html, /autoComplete="off"/);
  assert.match(html, /disabled=""/);
});

test('Access settings expose policy guidance without enabling local credential mutation', () => {
  const html = renderToStaticMarkup(
    <WorkspaceAuthentication
      session={{
        authenticated: true,
        mode: 'cloudflare-access',
        credential_change_supported: false,
        logout_url: '/cdn-cgi/access/logout',
      }}
    />,
  );
  assert.match(html, /Cloudflare Access/);
  assert.doesNotMatch(html, /<form|type="password"/);
});

test('key changes require current proof, confirmation and disable empty submit', () => {
  const html = renderToStaticMarkup(
    <WorkspaceAuthentication
      session={{
        authenticated: true,
        mode: 'token',
        credential_change_supported: true,
        logout_url: null,
      }}
    />,
  );
  assert.match(html, /現在の接続キー/);
  assert.match(html, /新しい接続キーを再入力/);
  assert.match(html, /全員のワークスペース接続が終了/);
  assert.match(html, /disabled=""/);
  assert.doesNotMatch(html, /CONTROL_API_TOKEN/);
});

test('configuration readiness never renders as runtime success or one-click install', () => {
  const component: SetupComponent = {
    id: 'compute',
    title: 'Spark',
    description: 'Synthetic preparation only',
    profile: 'compute',
    selected: false,
    configured: true,
    missing_settings: [],
    management_supported: true,
    required_profiles: ['compute'],
    depends_on: [],
    steps: [],
    state: 'ready_for_operator_review',
    external_link_configured: false,
  };
  const html = renderToStaticMarkup(
    <SetupCard component={component} onPrepare={() => undefined} busy={false} />,
  );
  assert.match(html, /設定は準備済み/);
  assert.match(html, /初回準備後はGUIで起動・停止/);
  assert.match(html, /外部から開く入口は別途準備/);
  assert.doesNotMatch(html, /稼働中|インストール済み|localhost/);
});

test('setup checklist preserves before/after selection and explicit operator boundary', () => {
  const plan: SetupPlan = {
    version: 1,
    component_id: 'notebooks',
    title: 'JupyterLab',
    profiles_before: ['demo'],
    profiles_after: ['demo', 'notebooks'],
    settings_to_review: ['NOTEBOOKS_DIR'],
    steps: [
      {
        id: 'workspace',
        title: '保存領域を確認',
        description: 'Synthetic checklist; no ownership mutation.',
        kind: 'host',
      },
    ],
    can_apply: false,
    requires_operator: true,
    notice: '初回配置は管理者が確認してください。',
  };
  const html = renderToStaticMarkup(<SetupChecklist plan={plan} />);
  assert.match(html, /NOTEBOOKS_DIR/);
  assert.match(html, /初回配置は管理者が確認/);
  assert.doesNotMatch(html, /<input|sudo |docker compose/);
});
