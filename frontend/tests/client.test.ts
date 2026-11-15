/** Synthetic transport contracts, independent of a browser or live API. */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import {
  api,
  ApiError,
  changeWorkspaceCredential,
  getSession,
  getSessionInfo,
  importMetadata,
  login,
  loginWithCloudflare,
  logout,
  parseSessionInfo,
} from '../src/api.js';
import { isSetupPlan, isSetupView, prepareSetup } from '../src/setup.js';
import type { SetupPlan } from '../src/setup.js';

type Call = { url: string; options: RequestInit };
const originalFetch = globalThis.fetch;
let calls: Call[];
let events: string[];

beforeEach(() => {
  calls = [];
  events = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
      dispatchEvent: (event: Event) => {
        events.push(event.type);
        return true;
      },
    },
  });
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json({ authenticated: true, ok: true });
  };
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  Reflect.deleteProperty(globalThis, 'window');
});

test('ordinary service and data paths use the fixed same-origin prefix', async () => {
  await api('/services');
  await api('/catalog/preview?dataset=production&limit=20');
  assert.equal(calls[0].url, '/api/control/services');
  assert.equal(calls[1].url, '/api/control/catalog/preview?dataset=production&limit=20');
  for (const { options } of calls) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.redirect, 'error');
    assert.equal(options.cache, 'no-store');
    assert.equal(new Headers(options.headers).has('authorization'), false);
  }
});

test('external, protocol-relative and control-character paths are refused', async () => {
  for (const path of ['https://example.invalid', '//example.invalid', '/a\\b', '/a\r', '/a\n']) {
    await assert.rejects(api(path), ApiError);
  }
  assert.equal(calls.length, 0);
});

test('unsafe methods carry the CSRF marker without a browser bearer', async () => {
  await api('/settings', { method: 'PATCH', payload: { values: {} } });
  const headers = new Headers(calls[0].options.headers);
  assert.equal(headers.get('X-Poc-Request'), '1');
  assert.equal(headers.get('Content-Type'), 'application/json');
  assert.equal(headers.has('Authorization'), false);
});

test('metadata bytes preserve duplicate fields for server-side rejection', async () => {
  const raw = '{"schema_version":1,"schema_version":2}';
  await importMetadata(raw);
  assert.equal(calls[0].url, '/api/control/onboarding/validate-discovery');
  assert.equal(calls[0].options.body, raw);
});

test('oversize metadata fails before fetch', async () => {
  await assert.rejects(importMetadata('x'.repeat(256 * 1024 + 1)), ApiError);
  assert.equal(calls.length, 0);
});

test('the initial key is used only in the login body, not later requests', async () => {
  const key = 'synthetic'.repeat(6);
  assert.equal(await login(key), true);
  assert.equal(calls[0].url, '/api/session');
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), { token: key });
  assert.equal(calls[1].url, '/api/session');
  assert.equal(calls[1].options.body, undefined);
  await api('/services');
  assert.equal(JSON.stringify(calls.slice(1)).includes(key), false);
  for (const call of calls) {
    assert.equal(new Headers(call.options.headers).has('Authorization'), false);
  }
});

test('login state is explicit and logout uses the protected endpoint', async () => {
  assert.equal(await getSession(), true);
  await logout();
  assert.equal(calls[1].url, '/api/session');
  assert.equal(calls[1].options.method, 'DELETE');
  assert.equal(new Headers(calls[1].options.headers).get('X-Poc-Request'), '1');
});

test('legacy session responses retain token mode without gaining credential mutation', async () => {
  assert.deepEqual(await getSessionInfo(), {
    authenticated: true,
    mode: 'token',
    credential_change_supported: false,
    logout_url: null,
  });
});

test('Access login uses only the same-origin session endpoint and no browser token', async () => {
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json({
      authenticated: true,
      mode: 'cloudflare-access',
      credential_change_supported: false,
      logout_url: '/cdn-cgi/access/logout',
    });
  };
  assert.equal(await loginWithCloudflare(), true);
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), { method: 'cloudflare-access' });
  assert.equal(calls[0].url, '/api/session');
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(new Headers(calls[0].options.headers).has('Authorization'), false);
});

test('auth DTOs reject forged modes, inconsistent capabilities and untrusted logout destinations', () => {
  const valid = {
    authenticated: true,
    mode: 'cloudflare-access',
    credential_change_supported: false,
    logout_url: '/cdn-cgi/access/logout',
  };
  for (const value of [
    null,
    { authenticated: 'true' },
    { ...valid, mode: ['token'] },
    { ...valid, credential_change_supported: true },
    { ...valid, logout_url: 'https://example.invalid/logout' },
    { ...valid, logout_url: '//example.invalid/logout' },
    { ...valid, logout_url: '/cdn-cgi/access/logout?redirect=other' },
    { ...valid, mode: 'token' },
    { authenticated: false, mode: 'token' },
  ]) {
    assert.throws(() => parseSessionInfo(value), ApiError);
  }
  assert.deepEqual(parseSessionInfo(valid), valid);
});

test('key rotation uses a dedicated CSRF-protected request and invalidates the UI session', async () => {
  const previous = 'synthetic-old'.repeat(3);
  const replacement = 'synthetic-new'.repeat(3);
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json({ changed: true, authenticated: false });
  };
  await changeWorkspaceCredential(previous, replacement);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/auth/credential');
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), {
    current_token: previous,
    new_token: replacement,
  });
  assert.equal(new Headers(calls[0].options.headers).get('X-Poc-Request'), '1');
  assert.deepEqual(events, ['studio:credential-changed']);
});

test('ambiguous key rotation results are not retried or falsely reported as success', async () => {
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json({ changed: true, authenticated: true });
  };
  await assert.rejects(
    changeWorkspaceCredential('synthetic-current-key', 'synthetic-replacement-key'),
    ApiError,
  );
  assert.equal(calls.length, 1);
  assert.deepEqual(events, []);
});

const setupPlan = (): SetupPlan => ({
  version: 1,
  component_id: 'compute',
  title: 'Spark',
  profiles_before: ['demo'],
  profiles_after: ['demo', 'compute'],
  settings_to_review: [],
  steps: [
    {
      id: 'build',
      title: 'イメージを準備',
      description: '管理者が対象を確認します。',
      kind: 'build',
    },
  ],
  can_apply: false,
  requires_operator: true,
  notice: '初回配置は管理者が確認します。',
});

test('setup preparation sends only the selected component and never an apply request', async () => {
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json(setupPlan());
  };
  const result = await prepareSetup('compute');
  assert.equal(calls[0].url, '/api/control/setup/plan');
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), { component_id: 'compute' });
  assert.equal(result.can_apply, false);
  assert.equal(result.requires_operator, true);
  assert.equal(calls.length, 1);
});

test('setup export projects only its public DTO and drops unexpected secret fields', async () => {
  globalThis.fetch = async () =>
    Response.json({
      ...setupPlan(),
      unexpected_private_value: 'synthetic-secret-sentinel',
      steps: [{ ...setupPlan().steps[0], raw_command: 'synthetic-sensitive-input' }],
    });
  const result = await prepareSetup('compute');
  assert.doesNotMatch(
    JSON.stringify(result),
    /synthetic-secret-sentinel|synthetic-sensitive-input/,
  );
});

test('setup refuses apply-capable, mismatched and malformed plans', async () => {
  for (const result of [
    { ...setupPlan(), can_apply: true },
    { ...setupPlan(), requires_operator: false },
    { ...setupPlan(), component_id: 'notebooks' },
    { ...setupPlan(), steps: [{ ...setupPlan().steps[0], kind: 'shell' }] },
  ]) {
    globalThis.fetch = async () => Response.json(result);
    await assert.rejects(prepareSetup('compute'));
  }
  assert.equal(isSetupPlan(setupPlan()), true);
  assert.equal(
    isSetupView({
      version: 1,
      manager: 'operator-compose',
      apply_supported: false,
      applied_profiles: ['demo'],
      desired_profiles: ['demo'],
      pending_restart: false,
      components: [],
    }),
    true,
  );
  assert.equal(
    isSetupView({
      version: 1,
      manager: 'operator-compose',
      apply_supported: true,
      applied_profiles: [],
      desired_profiles: [],
      pending_restart: false,
      components: [],
    }),
    false,
  );
});

test('unsafe component identifiers fail before any transport', async () => {
  for (const id of ['../../etc', '--all', 'compute\n', 'http://example.invalid'])
    await assert.rejects(prepareSetup(id));
  assert.equal(calls.length, 0);
});

test('HTTP failures cancel the body and never reflect private error contents', async () => {
  const sentinel = 'synthetic-' + 'private'.repeat(8);
  let cancelled = false;
  globalThis.fetch = async () =>
    new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(sentinel));
        },
        cancel() {
          cancelled = true;
        },
      }),
      { status: 422 },
    );
  await assert.rejects(api('/services'), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 422);
    assert.equal(error.message.includes(sentinel), false);
    return true;
  });
  assert.equal(cancelled, true);
});

test('unauthorized API responses invalidate UI authentication state', async () => {
  globalThis.fetch = async () => Response.json({}, { status: 401 });
  await assert.rejects(api('/services'), ApiError);
  assert.deepEqual(events, ['studio:unauthorized']);
});

test('oversize, malformed and invalid UTF-8 responses fail safely', async () => {
  for (const body of ['x'.repeat(1024 * 1024 + 1), '{', new Uint8Array([255])]) {
    globalThis.fetch = async () => new Response(body);
    await assert.rejects(api('/services'), ApiError);
  }
});

test('aborted requests are not retried or reported as successful operations', async () => {
  let attempts = 0;
  globalThis.fetch = async (_input, options = {}) => {
    attempts += 1;
    return new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new Error('synthetic abort')));
    });
  };
  await assert.rejects(api('/services', { timeout: 5 }), ApiError);
  assert.equal(attempts, 1);
});
