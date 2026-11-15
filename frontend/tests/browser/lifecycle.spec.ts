/** Prepared synthetic acceptance only; no real API, Docker, or operator runner is used. */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function setup(page: Page, loseReceipt = false, disconnected = false) {
  let authenticated = false;
  let operation: Record<string, unknown> | null = null;
  let idempotencyKey = '';
  let posted = 0;
  let state: 'queued' | 'succeeded' = 'queued';
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const now = Date.now() / 1000;
    const receipt = () => ({
      ...operation,
      status: state,
      updated_at: now,
      result: state === 'succeeded' ? { observed_state: 'running' } : null,
    });
    if (path === '/api/session') {
      if (request.method() === 'POST') authenticated = true;
      await route.fulfill({ json: { authenticated } });
    } else if (path === '/api/control/services') {
      await route.fulfill({
        json: {
          pending_restart: false,
          services: [
            {
              id: 'grafana',
              title: 'Grafana',
              description: 'Synthetic management fixture',
              profile: 'monitoring',
              profiles: ['monitoring'],
              enabled: false,
              configured: true,
              state: 'not_enabled',
              url: 'https://grafana.example.invalid/',
              link_mode: 'public',
              link_configured: true,
              missing_settings: [],
            },
          ],
        },
      });
    } else if (path === '/api/control/catalog') {
      await route.fulfill({ json: { version: 1, datasets: [] } });
    } else if (path === '/api/control/jobs') {
      await route.fulfill({ json: { jobs: [], persistent: true, history_limit: 20 } });
    } else if (path === '/api/control/lifecycle/capabilities') {
      await route.fulfill({
        json: {
          version: 1,
          manager: 'operator-compose',
          connected: !disconnected,
          last_seen: disconnected ? null : now,
          active_operation:
            operation && state === 'queued' ? { id: operation.id, status: state } : null,
          applied_profiles: [],
          desired_profiles: [],
          services: [
            {
              service_id: 'grafana',
              group_id: 'monitoring',
              title: 'Grafana',
              allowed_actions: disconnected ? [] : ['start', 'restart', 'stop'],
              blocked_reasons: disconnected ? ['RUNNER_DISCONNECTED'] : [],
              observed_state: state === 'succeeded' ? 'running' : 'stopped',
              last_seen: disconnected ? null : now,
              profile_selected: false,
            },
            {
              service_id: 'sail-server',
              group_id: 'sail-lab',
              title: 'Sail Lab Server',
              allowed_actions: disconnected ? [] : ['start', 'restart', 'stop'],
              blocked_reasons: disconnected ? ['RUNNER_DISCONNECTED'] : [],
              observed_state: 'unknown',
              last_seen: disconnected ? null : now,
              profile_selected: false,
            },
          ],
        },
      });
    } else if (path === '/api/control/lifecycle/plans' && request.method() === 'POST') {
      const body = request.postDataJSON();
      await route.fulfill({
        json: {
          id: '111111111111111111111111',
          service_id: body.service_id,
          action: body.action,
          group_id: 'monitoring',
          affected_services: ['prometheus', 'grafana', 'loki', 'vector'],
          added_profiles: ['monitoring'],
          warnings: ['GROUP_OPERATION', 'VOLUMES_PRESERVED'],
          confirmation: 'synthetic-confirmation-only',
          expires_at: now + 120,
          manifest_digest: 'a'.repeat(64),
        },
      });
    } else if (path === '/api/control/lifecycle/operations' && request.method() === 'POST') {
      posted += 1;
      idempotencyKey = request.postDataJSON().idempotency_key;
      operation = {
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
      if (loseReceipt) await route.abort('failed');
      else await route.fulfill({ json: receipt() });
    } else if (path === '/api/control/lifecycle/operations' && request.method() === 'GET') {
      await route.fulfill({ json: { operations: operation ? [receipt()] : [] } });
    } else if (
      path === '/api/control/lifecycle/operations/' + operation?.id ||
      path === '/api/control/lifecycle/operations/by-key/' + idempotencyKey
    ) {
      await route.fulfill({ json: receipt() });
    } else {
      // Fail closed: an unexpected request never reaches the configured real upstream.
      await route.fulfill({ status: 503, json: { code: 'SYNTHETIC_NOT_CONFIGURED' } });
    }
  });
  await page.goto('/');
  await page.getByLabel('ワークスペース接続キー', { exact: true }).fill('synthetic'.repeat(6));
  await page.getByRole('button', { name: '接続する', exact: true }).click();
  await page.getByRole('button', { name: 'サービス', exact: true }).click();
  return {
    posts: () => posted,
    finish: () => {
      state = 'succeeded';
    },
  };
}

test('profile-off service requires impact consent, submits once and polls actual completion', async ({
  page,
}) => {
  const fixture = await setup(page);
  await page.getByRole('button', { name: 'Grafanaを起動', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  for (const name of ['prometheus', 'grafana', 'loki', 'vector'])
    await expect(dialog.getByText(name, { exact: true })).toBeVisible();
  const confirm = dialog.getByRole('button', { name: '確認して起動', exact: true });
  await expect(confirm).toBeDisabled();
  await dialog.getByRole('checkbox').check();
  await expect(confirm).toBeEnabled();
  // Two immediate events exercise the synchronous in-flight guard, not only CSS disabling.
  await confirm.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(page.getByText('起動：受付済み・実行待ち', { exact: true })).toBeVisible();
  expect(fixture.posts()).toBe(1);
  fixture.finish();
  await expect(page.getByText('起動：完了', { exact: true })).toBeVisible();
  const card = page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name: 'Grafana', exact: true }) });
  await expect(card.getByText('稼働中', { exact: true })).toBeVisible();
  await expect(card.locator('a')).toHaveAttribute('href', 'https://grafana.example.invalid/');
  expect(fixture.posts()).toBe(1);
});

test('uncertain receipt closes the modal and recovers by GET without a second operation POST', async ({
  page,
}) => {
  const fixture = await setup(page, true);
  await page.getByRole('button', { name: 'Grafanaを起動', exact: true }).click();
  await page.getByRole('dialog').getByRole('checkbox').check();
  await page.getByRole('button', { name: '確認して起動', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Grafanaを起動', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '受付状況を確認', exact: true }).click();
  await expect(page.getByText('起動：受付済み・実行待ち', { exact: true })).toBeVisible();
  expect(fixture.posts()).toBe(1);
  expect(
    await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })),
  ).toEqual({ local: 0, session: 0 });
});

test('disconnected runner stays unknown and RPC-only lab offers no fabricated web link', async ({
  page,
}) => {
  const fixture = await setup(page, false, true);
  await expect(page.getByText('管理機能未接続', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Grafanaを起動', exact: true })).toBeDisabled();
  await expect(
    page.getByRole('heading', { name: '画面のないサービス', exact: true }),
  ).toBeVisible();
  const lab = page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name: 'Sail Lab Server', exact: true }) });
  await expect(lab.locator('a')).toHaveCount(0);
  await expect(lab.getByText('未確認', { exact: true })).toBeVisible();
  expect(fixture.posts()).toBe(0);
});
