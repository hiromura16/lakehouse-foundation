/** Contract/render/transport regressions only; no host Docker acceptance. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { managerRequest } from '../src/api.js';
import { EnvironmentCard } from '../src/EnvironmentManager.js';
import {
  canPlan,
  parseManagerPlan,
  parseManagerSession,
  parseManagerState,
  parseOperation,
  type ManagerState,
} from '../src/manager.js';

const state: ManagerState = {
  schema_version: 1,
  runner_connected: true,
  pocs: [
    {
      poc_id: 'foundation',
      title: '基盤PoC',
      state: 'stopped',
      services: [{ service: 'worker', state: 'stopped' }],
      blocked_reasons: [],
      observed_at: 1700000000,
    },
  ],
};
const plan = {
  schema_version: 1,
  can_execute: false,
  poc_id: 'foundation',
  action: 'stop',
  requires_interruption_ack: true,
  interruption_acknowledged: false,
  effect: 'graceful_stop_interruption_possible',
  snapshot_digest: 'a'.repeat(64),
  steps: [{ service: 'worker', action: 'stop' }],
  excluded_completed_bootstraps: ['catalog-migrate'],
  volume_deletion: false,
  automatic_retry: false,
  plan_id: 'b'.repeat(24),
  confirmation: 'c'.repeat(32),
  expires_at: 1700000060,
};
test('unexpected response fields are excluded from the UI state projection', () => {
  const result = parseManagerPlan({ ...plan, private_command: 'synthetic-unused' });
  assert.equal('private_command' in result, false);
  const resultState = parseManagerState({
    ...state,
    pocs: [
      {
        ...state.pocs[0],
        private_project: 'synthetic-unused',
        services: [{ service: 'worker', state: 'stopped', private_id: 'synthetic-unused' }],
      },
    ],
  });
  assert.equal('private_project' in resultState.pocs[0], false);
  assert.equal('private_id' in resultState.pocs[0].services[0], false);
});
test('public manager contracts require finite status, bounded steps and explicit no-delete/no-retry', () => {
  assert.deepEqual(parseManagerState(state), state);
  assert.deepEqual(parseManagerPlan(plan), plan);
  for (const replacement of [
    { volume_deletion: true },
    { automatic_retry: true },
    { can_execute: true },
    { expires_at: Number.NaN },
    { steps: Array(257).fill(plan.steps[0]) },
  ]) {
    assert.throws(() => parseManagerPlan({ ...plan, ...replacement }));
  }
  assert.throws(() => parseManagerState({ ...state, pocs: [...state.pocs, ...state.pocs] }));
  assert.throws(() =>
    parseManagerState({ ...state, pocs: [{ ...state.pocs[0], state: 'healthy' }] }),
  );
});
test('unknown operation, mismatched counts and unsafe session logout fail closed', () => {
  const operation = {
    id: 'a'.repeat(24),
    poc_id: 'vision',
    action: 'start',
    status: 'unknown',
    created_at: 1700000000,
    updated_at: 1700000001,
    completed_steps: 0,
    step_count: 2,
    error_code: 'OUTCOME_UNKNOWN',
    interruption_ack: false,
    interruption_possible: false,
  };
  assert.equal(parseOperation(operation).status, 'unknown');
  assert.throws(() => parseOperation({ ...operation, completed_steps: 3 }));
  assert.throws(() => parseOperation({ ...operation, error_code: 'raw error with URL' }));
  assert.throws(() =>
    parseManagerSession({
      authenticated: true,
      mode: 'cloudflare-access',
      role: 'operator',
      logout_url: 'https://external.example.invalid/',
    }),
  );
});
test('low memory does not block stop preview; missing host evidence stays disabled', () => {
  const poc = { ...state.pocs[0], blocked_reasons: ['MEMORY_BUDGET'] };
  assert.equal(canPlan(state, poc, false), true);
  assert.equal(canPlan({ ...state, runner_connected: false }, poc, false), false);
  assert.equal(canPlan(state, { ...poc, blocked_reasons: ['OUTCOME_UNKNOWN'] }, false), false);
  assert.equal(canPlan(state, poc, true), false);
});
test('environment controls use logical targets and never expose host commands or fake running state', () => {
  const html = renderToStaticMarkup(
    <EnvironmentCard poc={state.pocs[0]} state={state} busy={false} onPlan={() => undefined} />,
  );
  assert.match(html, /基盤PoC/);
  assert.match(html, /停止中/);
  assert.match(html, /再起動/);
  assert.doesNotMatch(html, /docker|sudo|data-command|http:\/\/localhost/);
  const disconnected = renderToStaticMarkup(
    <EnvironmentCard
      poc={{ ...state.pocs[0], state: 'unknown' }}
      state={{ ...state, runner_connected: false }}
      busy={false}
      onPlan={() => undefined}
    />,
  );
  assert.match(disconnected, /未確認/);
  assert.equal((disconnected.match(/disabled=""/g) ?? []).length, 3);
});
test('manager transport has its own fixed same-origin routes and never retries mutation', async () => {
  const previousFetch = globalThis.fetch;
  const previousWindow = globalThis.window;
  let calls = 0;
  globalThis.window = { setTimeout, clearTimeout } as unknown as Window & typeof globalThis;
  globalThis.fetch = async (url, init) => {
    calls++;
    assert.equal(url, '/api/operations');
    assert.equal(init?.redirect, 'error');
    assert.equal(init?.credentials, 'same-origin');
    assert.equal((init?.headers as Record<string, string>)['X-Poc-Request'], '1');
    throw new TypeError('synthetic lost response');
  };
  try {
    await assert.rejects(managerRequest('/api/operations', 'POST', {}));
    assert.equal(calls, 1);
    for (const path of [
      '/internal/runner/claim',
      'https://example.invalid/api/pocs',
      '/api/pocs/unknown/plans',
      '/api/operations?limit=999',
      '/api/operations/by-key/../private',
    ])
      await assert.rejects(managerRequest(path));
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.window = previousWindow;
  }
});
