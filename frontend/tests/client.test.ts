/** Synthetic transport contracts, independent of a browser or live API. */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { api, ApiError, getSession, importMetadata, login, logout } from '../src/api.js';

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
