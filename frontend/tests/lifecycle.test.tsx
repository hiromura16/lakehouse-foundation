/** Synthetic management contracts: these tests execute neither Docker nor a live API. */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ApiError } from '../src/api.js';
import {
  LifecycleSubmission,
  observedState,
  parseLifecycleCapabilities,
  parseLifecycleOperation,
  parseLifecyclePlan,
  prepareLifecyclePlan,
  readLifecycleHistory,
  readLifecycleOperation,
  runnerFresh,
} from '../src/lifecycle.js';
import type { LifecycleCapabilities, LifecycleOperation, LifecyclePlan } from '../src/lifecycle.js';
import {
  LifecycleConnectionNotice,
  LifecycleOperationNotice,
  LifecyclePlanContents,
  LifecycleServiceControls,
} from '../src/pages/ServiceLifecycle.js';
import type { Service } from '../src/types.js';

const now = 10_000;
const service: Service = {
  id: 'grafana',
  title: 'Grafana',
  description: 'Synthetic management fixture',
  profile: 'monitoring',
  enabled: false,
  configured: true,
  state: 'not_enabled',
  url: 'https://grafana.example.invalid/',
  link_mode: 'public',
  link_configured: true,
  missing_settings: [],
};
function capabilities(): LifecycleCapabilities {
  return {
    version: 1,
    manager: 'operator-compose',
    connected: true,
    last_seen: now,
    active_operation: null,
    applied_profiles: [],
    desired_profiles: ['monitoring'],
    services: [
      {
        service_id: 'grafana',
        group_id: 'monitoring',
        title: 'Grafana',
        allowed_actions: ['start', 'restart', 'stop'],
        blocked_reasons: [],
        observed_state: 'stopped',
        last_seen: now,
        profile_selected: false,
      },
    ],
  };
}
function plan(): LifecyclePlan {
  return {
    id: '111111111111111111111111',
    service_id: 'grafana',
    action: 'start',
    group_id: 'monitoring',
    affected_services: ['prometheus', 'grafana', 'loki', 'vector'],
    added_profiles: ['monitoring'],
    warnings: ['GROUP_OPERATION', 'VOLUMES_PRESERVED', 'EXTERNAL_JOBS_NOT_ATOMICALLY_LOCKED'],
    confirmation: 'synthetic-confirmation-never-rendered',
    expires_at: Date.now() / 1000 + 120,
    manifest_digest: 'a'.repeat(64),
  };
}
function operation(): LifecycleOperation {
  return {
    id: '222222222222222222222222',
    service_id: 'grafana',
    action: 'start',
    group_id: 'monitoring',
    status: 'queued',
    created_at: now,
    updated_at: now,
    error: null,
    result: null,
  };
}
const originalFetch = globalThis.fetch;
let calls: { url: string; options: RequestInit }[] = [];
beforeEach(() => {
  calls = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
      dispatchEvent: () => true,
    },
  });
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json(operation());
  };
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  Reflect.deleteProperty(globalThis, 'window');
});

test('capabilities distinguish desired profiles from actual observations', () => {
  const value = parseLifecycleCapabilities(capabilities());
  assert.equal(value.services[0].profile_selected, false);
  assert.equal(observedState(value, value.services[0], now), 'stopped');
  value.services[0].observed_state = 'running';
  assert.equal(observedState(value, value.services[0], now), 'running');
  assert.equal(value.services[0].profile_selected, false);
});

test('stale, disconnected, missing and future observations are always unknown', () => {
  const value = capabilities();
  value.services[0].observed_state = 'running';
  assert.equal(runnerFresh(value, now + 41), false);
  assert.equal(observedState(value, value.services[0], now + 41), 'unknown');
  assert.equal(observedState(value, value.services[0], now - 6), 'unknown');
  assert.equal(observedState(value, undefined, now), 'unknown');
  assert.equal(observedState(null, value.services[0], now), 'unknown');
  value.connected = false;
  assert.equal(observedState(value, value.services[0], now), 'unknown');
  value.connected = true;
  value.services[0].last_seen = now - 41;
  assert.equal(observedState(value, value.services[0], now), 'unknown');
});

test('capability parsing rejects malformed enums, duplicate identities and unsafe names', () => {
  for (const value of [
    { ...capabilities(), version: 2 },
    { ...capabilities(), connected: true, last_seen: null },
    { ...capabilities(), services: [...capabilities().services, ...capabilities().services] },
    { ...capabilities(), services: [{ ...capabilities().services[0], observed_state: 'healthy' }] },
    {
      ...capabilities(),
      services: [{ ...capabilities().services[0], allowed_actions: ['delete'] }],
    },
    { ...capabilities(), services: [{ ...capabilities().services[0], group_id: null }] },
    { ...capabilities(), services: [{ ...capabilities().services[0], service_id: '../runner' }] },
  ])
    assert.throws(() => parseLifecycleCapabilities(value), ApiError);
});

test('receipt projection discards raw errors, commands, log text and extra result fields', () => {
  const sentinel = 'synthetic-raw-content-never-render';
  const value = parseLifecycleOperation({
    ...operation(),
    status: 'failed',
    error: { code: 'STATE_UNAVAILABLE', message: sentinel },
    result: { observed_state: 'unknown', command: sentinel, stdout: sentinel },
    token: sentinel,
  });
  assert.equal(JSON.stringify(value).includes(sentinel), false);
  assert.deepEqual(value.error, { code: 'STATE_UNAVAILABLE' });
  assert.deepEqual(value.result, { observed_state: 'unknown' });
  assert.throws(() => parseLifecycleOperation({ ...operation(), status: 'complete' }), ApiError);
});

test('a success receipt without matching observed-state evidence is rejected', () => {
  for (const result of [null, { observed_state: 'unknown' }, { observed_state: 'stopped' }]) {
    assert.throws(
      () => parseLifecycleOperation({ ...operation(), status: 'succeeded', result }),
      ApiError,
    );
  }
  assert.equal(
    parseLifecycleOperation({
      ...operation(),
      status: 'succeeded',
      result: { observed_state: 'running' },
    }).status,
    'succeeded',
  );
  assert.equal(
    parseLifecycleOperation({
      ...operation(),
      action: 'stop',
      status: 'succeeded',
      result: { observed_state: 'stopped' },
    }).status,
    'succeeded',
  );
});

test('plan transport targets the selected action with cookie/CSRF and no browser bearer', async () => {
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json(plan());
  };
  await prepareLifecyclePlan('grafana', 'start');
  assert.equal(calls[0].url, '/api/control/lifecycle/plans');
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), {
    service_id: 'grafana',
    action: 'start',
  });
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.redirect, 'error');
  const headers = new Headers(calls[0].options.headers);
  assert.equal(headers.get('X-Poc-Request'), '1');
  assert.equal(headers.has('Authorization'), false);
});

test('plan validation refuses changed targets and malformed confirmation payloads', () => {
  for (const value of [
    { ...plan(), service_id: 'superset' },
    { ...plan(), action: 'stop' },
    { ...plan(), affected_services: [] },
    { ...plan(), affected_services: ['../docker'] },
    { ...plan(), warnings: ['raw command output'] },
    { ...plan(), manifest_digest: 'invalid' },
  ])
    assert.throws(
      () => parseLifecyclePlan(value, { service_id: 'grafana', action: 'start' }),
      ApiError,
    );
});

test('concurrent confirmation clicks share one promise and send exactly one operation POST', async () => {
  const submission = new LifecycleSubmission(plan(), 'synthetic_key_123456');
  const first = submission.submit();
  assert.equal(submission.submit(), first);
  const accepted = await first;
  assert.equal(accepted.status, 'queued');
  assert.equal(submission.submit(), first);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/control/lifecycle/operations');
  assert.deepEqual(JSON.parse(String(calls[0].options.body)), {
    plan_id: submission.plan.id,
    confirmation: submission.plan.confirmation,
    idempotency_key: 'synthetic_key_123456',
  });
});

test('expired plan and invalid key fail before any operation request', async () => {
  const submission = new LifecycleSubmission({ ...plan(), expires_at: 1 }, 'synthetic_key_123456');
  await assert.rejects(submission.submit(), ApiError);
  assert.throws(() => new LifecycleSubmission(plan(), '../runner'), ApiError);
  await assert.rejects(readLifecycleOperation('../runner'), ApiError);
  assert.equal(calls.length, 0);
});

test('a lost receipt never repeats POST and is recovered only through the same read-only key', async () => {
  const submission = new LifecycleSubmission(plan(), 'synthetic_key_123456');
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    if (options.method === 'POST') throw new Error('synthetic transport interruption');
    return Response.json(operation());
  };
  const first = submission.submit();
  await assert.rejects(first, ApiError);
  assert.equal(submission.submit(), first);
  await assert.rejects(submission.submit(), ApiError);
  assert.equal((await submission.recover()).id, operation().id);
  assert.deepEqual(
    calls.map((item) => [item.url, item.options.method]),
    [
      ['/api/control/lifecycle/operations', 'POST'],
      ['/api/control/lifecycle/operations/by-key/synthetic_key_123456', 'GET'],
    ],
  );
});

test('a mismatched receipt cannot complete the selected service operation', async () => {
  globalThis.fetch = async () => Response.json({ ...operation(), service_id: 'superset' });
  const submission = new LifecycleSubmission(plan(), 'synthetic_key_123456');
  await assert.rejects(submission.submit(), ApiError);
  await assert.rejects(submission.recover(), ApiError);
});

test('recent journal history is bounded, parsed and read without repeating commands', async () => {
  globalThis.fetch = async (input, options = {}) => {
    calls.push({ url: String(input), options });
    return Response.json({ operations: [operation()] });
  };
  assert.equal((await readLifecycleHistory())[0].status, 'queued');
  assert.equal(calls[0].url, '/api/control/lifecycle/operations?limit=10');
  assert.equal(calls[0].options.method, 'GET');
  globalThis.fetch = async () => Response.json({ operations: Array(11).fill(operation()) });
  await assert.rejects(readLifecycleHistory(), ApiError);
});

function controls(value: LifecycleCapabilities | null, busy = false, connectionError = false) {
  return renderToStaticMarkup(
    <LifecycleServiceControls
      service={service}
      capabilities={value}
      capability={value?.services[0]}
      busy={busy}
      connectionError={connectionError}
      now={now}
      onAction={() => assert.fail('SSR must never submit')}
    />,
  );
}

test('disconnected management disables actions without reporting stopped containers', () => {
  const html = controls(null);
  assert.match(html, /管理機能未接続/);
  assert.match(html, /実コンテナ/);
  assert.match(html, /未確認/);
  assert.doesNotMatch(html, /停止中|稼働中/);
  assert.equal((html.match(/<button[^>]*disabled=""/g) ?? []).length, 3);
});

test('unselected profile can offer connected start while preserving separate running evidence', () => {
  const value = capabilities();
  value.services[0].observed_state = 'running';
  const html = controls(value);
  assert.match(html, /未選択/);
  assert.match(html, /稼働中/);
  assert.match(html, /aria-label="Grafanaを起動"/);
  assert.doesNotMatch(html, /disabled=""/);
});

test('protected, busy, stale and disconnected actions fail closed', () => {
  const protectedValue = capabilities();
  protectedValue.services[0].allowed_actions = [];
  protectedValue.services[0].blocked_reasons = ['SERVICE_PROTECTED'];
  assert.match(controls(protectedValue), /基盤サービスを保護/);
  const stale = capabilities();
  stale.last_seen = now - 41;
  for (const html of [
    controls(protectedValue),
    controls(capabilities(), true),
    controls(stale),
    controls(capabilities(), false, true),
  ]) {
    assert.equal((html.match(/<button[^>]*disabled=""/g) ?? []).length, 3);
  }
});

test('queued receipt explicitly distinguishes acceptance from successful execution', () => {
  const html = renderToStaticMarkup(
    <LifecycleOperationNotice
      operation={operation()}
      error={null}
      recovering={false}
      onRefresh={() => {}}
    />,
  );
  assert.match(html, /受付済み・実行待ち/);
  assert.match(html, /開始は、まだ確認されていません/);
  assert.doesNotMatch(html, /起動：完了/);
});

test('failure display uses fixed reason codes and treats unknown as unconfirmed', () => {
  const failed = parseLifecycleOperation({
    ...operation(),
    status: 'failed',
    error: { code: 'STATE_UNAVAILABLE', message: 'synthetic-raw-private-message' },
  });
  const html = renderToStaticMarkup(
    <LifecycleOperationNotice
      operation={failed}
      error={null}
      recovering={false}
      onRefresh={() => {}}
    />,
  );
  assert.match(html, /稼働状態を確認できません/);
  assert.doesNotMatch(html, /synthetic-raw-private-message/);
  const unknown = renderToStaticMarkup(
    <LifecycleOperationNotice
      operation={{ ...operation(), status: 'unknown' }}
      error={null}
      recovering={false}
      onRefresh={() => {}}
    />,
  );
  assert.match(unknown, /結果未確認/);
  assert.doesNotMatch(unknown, /起動：完了/);
});

function confirmation(
  values: {
    acknowledged?: boolean;
    remaining?: number;
    connected?: boolean;
    uncertain?: boolean;
  } = {},
) {
  return renderToStaticMarkup(
    <LifecyclePlanContents
      plan={plan()}
      title="Grafana"
      acknowledged={values.acknowledged ?? false}
      remaining={values.remaining ?? 120}
      submitting={false}
      uncertain={values.uncertain ?? false}
      connected={values.connected ?? true}
      onAcknowledge={() => {}}
      onSubmit={() => assert.fail('SSR must not submit')}
      onCancel={() => {}}
      onRenew={() => {}}
    />,
  );
}

test('confirmation lists every impact and preserves volumes without rendering opaque authorization', () => {
  const html = confirmation();
  for (const target of plan().affected_services)
    assert.match(html, new RegExp(`<code>${target}</code>`));
  assert.match(html, /停止してもデータやvolumeは削除しません/);
  assert.match(html, /外部からの処理投入は一括停止できません/);
  assert.doesNotMatch(html, /synthetic-confirmation-never-rendered|a{64}|style=/);
  assert.match(html, /<button[^>]*disabled=""[^>]*>.*確認して起動/s);
});

test('confirmation requires fresh consent, unexpired plan and a connected runner', () => {
  assert.doesNotMatch(confirmation({ acknowledged: true }), /disabled=""/);
  for (const html of [
    confirmation(),
    confirmation({ acknowledged: true, connected: false }),
    confirmation({ acknowledged: true, uncertain: true }),
  ]) {
    assert.match(html, /<button[^>]*disabled=""[^>]*>.*確認して起動/s);
  }
  const expired = confirmation({ acknowledged: true, remaining: 0 });
  assert.match(expired, /有効期限が切れました/);
  assert.match(expired, /操作内容を再取得/);
  assert.doesNotMatch(expired, /確認して起動/);
});

test('connection notice keeps profile differences separate from container evidence', () => {
  const html = renderToStaticMarkup(
    <LifecycleConnectionNotice
      capabilities={capabilities()}
      connected={false}
      loading={false}
      failed={true}
      onRefresh={() => {}}
    />,
  );
  assert.match(html, /管理機能未接続/);
  assert.match(html, /起動対象の選択と、実際の稼働状態は別/);
  assert.match(html, /保存された起動対象と、APIの起動時設定が異なります/);
  assert.doesNotMatch(html, /実行済み|稼働中/);
});
