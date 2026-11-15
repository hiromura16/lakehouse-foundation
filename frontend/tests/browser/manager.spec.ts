/** Real browser with synthetic API only. No Docker or host privileges. */
import { expect, test, type Page } from '@playwright/test';

async function setup(page: Page, loseResponse = false, connected = true) {
  let authenticated = false;
  let posted = 0;
  let key = '';
  let operation: Record<string, unknown> | null = null;
  await page.route('http://127.0.0.1:4173/', async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(
      '</head>',
      '<meta name="lakehouse-mode" content="manager" /></head>',
    );
    await route.fulfill({ response, body });
  });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const now = Date.now() / 1000;
    if (path === '/api/session') {
      if (request.method() === 'POST') authenticated = true;
      await route.fulfill({
        json: {
          authenticated,
          mode: 'cloudflare-access',
          role: authenticated ? 'operator' : null,
          logout_url: '/cdn-cgi/access/logout',
        },
      });
    } else if (path === '/api/pocs') {
      await route.fulfill({
        json: {
          schema_version: 1,
          runner_connected: connected,
          pocs: ['foundation', 'vision'].map((poc_id) => ({
            poc_id,
            title: poc_id,
            state: 'stopped',
            services: [{ service: 'worker', state: 'stopped' }],
            blocked_reasons: connected ? [] : ['RUNNER_OFFLINE'],
            observed_at: now,
          })),
        },
      });
    } else if (/\/plans$/.test(path)) {
      const action = request.postDataJSON().action;
      await route.fulfill({
        json: {
          schema_version: 1,
          can_execute: false,
          poc_id: path.includes('/foundation/') ? 'foundation' : 'vision',
          action,
          requires_interruption_ack: action !== 'start',
          interruption_acknowledged: false,
          effect: action === 'start' ? 'resume_existing' : 'graceful_stop_interruption_possible',
          snapshot_digest: 'a'.repeat(64),
          steps: [{ service: 'worker', action: action === 'restart' ? 'stop' : action }],
          excluded_completed_bootstraps: [],
          volume_deletion: false,
          automatic_retry: false,
          plan_id: 'b'.repeat(24),
          confirmation: 'c'.repeat(32),
          expires_at: now + 60,
        },
      });
    } else if (path === '/api/operations' && request.method() === 'POST') {
      posted++;
      key = request.postDataJSON().idempotency_key;
      operation = {
        id: 'd'.repeat(24),
        poc_id: 'foundation',
        action: 'stop',
        status: 'queued',
        created_at: now,
        updated_at: now,
        completed_steps: 0,
        step_count: 1,
        error_code: null,
        interruption_ack: request.postDataJSON().interruption_ack,
        interruption_possible: true,
      };
      if (loseResponse) await route.abort('failed');
      else await route.fulfill({ status: 202, json: { operation } });
    } else if (path === '/api/operations') {
      await route.fulfill({ json: { operations: operation ? [operation] : [] } });
    } else if (path === `/api/operations/by-key/${key}`) {
      await route.fulfill({ json: { operation } });
    } else await route.fulfill({ status: 404, json: { detail: 'NOT_FOUND' } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Cloudflare Accessで接続' }).click();
  await expect(page.getByRole('heading', { name: '基盤PoC', exact: true })).toBeVisible();
  return { count: () => posted };
}

test('whole-PoC stop requires impact and interruption consent before one request', async ({
  page,
}) => {
  const fixture = await setup(page);
  await page
    .locator('.environment-foundation')
    .getByRole('button', { name: '停止', exact: true })
    .click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading', { name: '基盤PoCを停止' })).toBeVisible();
  const submit = dialog.getByRole('button', { name: '停止を実行' });
  await expect(submit).toBeDisabled();
  await dialog.getByLabel('基盤PoCへの操作と影響範囲を確認しました。').check();
  await expect(submit).toBeDisabled();
  await dialog.getByRole('checkbox').first().check();
  await submit.click();
  await expect(page.getByText('受付済み', { exact: true })).toBeVisible();
  expect(fixture.count()).toBe(1);
  await expect(
    page.locator('.environment-vision').getByRole('button', { name: '起動', exact: true }),
  ).toBeDisabled();
});

test('response loss permits GET recovery and never an automatic second POST', async ({ page }) => {
  const fixture = await setup(page, true);
  await page
    .locator('.environment-foundation')
    .getByRole('button', { name: '停止', exact: true })
    .click();
  const dialog = page.getByRole('dialog');
  for (const checkbox of await dialog.getByRole('checkbox').all()) await checkbox.check();
  await dialog.getByRole('button', { name: '停止を実行' }).click();
  await page.getByRole('button', { name: '受付結果だけを確認' }).click();
  await expect(page.getByRole('button', { name: '受付結果だけを確認' })).toHaveCount(0);
  expect(fixture.count()).toBe(1);
});

test('disconnected host never exposes enabled lifecycle controls', async ({ page }) => {
  await setup(page, false, false);
  await expect(page.getByText('ホスト側の管理処理が未接続です。', { exact: false })).toBeVisible();
  for (const button of await page.locator('.environment-actions button').all())
    await expect(button).toBeDisabled();
});
