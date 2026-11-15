/** Prepared synthetic UI acceptance; no live Access, operator or Docker calls. */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const component = {
  id: 'compute',
  title: 'Spark',
  description: 'Synthetic preparation fixture',
  profile: 'compute',
  selected: false,
  configured: true,
  missing_settings: [],
  management_supported: true,
  required_profiles: ['compute'],
  depends_on: [],
  steps: [
    {
      id: 'build',
      title: 'イメージを準備',
      description: '管理者が対象を確認します。',
      kind: 'build',
    },
  ],
  state: 'ready_for_operator_review',
  external_link_configured: false,
};
const plan = {
  version: 1,
  component_id: 'compute',
  title: 'Spark',
  profiles_before: ['demo'],
  profiles_after: ['compute', 'demo'],
  settings_to_review: [],
  steps: component.steps,
  can_apply: false,
  requires_operator: true,
  notice: '初回配置は管理者が確認します。',
};

async function fixture(page: Page, access = false, initiallyAuthenticated = false) {
  let authenticated = initiallyAuthenticated;
  const writes: string[] = [];
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') writes.push(path);
    if (path === '/api/session') {
      if (request.method() === 'POST') {
        expect(request.postDataJSON()).toEqual(
          access ? { method: 'cloudflare-access' } : { token: 'synthetic'.repeat(6) },
        );
        authenticated = true;
      }
      if (request.method() === 'DELETE') authenticated = false;
      await route.fulfill({
        json: {
          authenticated,
          mode: access ? 'cloudflare-access' : 'token',
          credential_change_supported: !access,
          logout_url: access ? '/cdn-cgi/access/logout' : null,
        },
      });
    } else if (path === '/api/auth/credential') {
      expect(request.postDataJSON()).toEqual({
        current_token: 'synthetic-current-key-123456',
        new_token: 'synthetic-replacement-key-123456',
      });
      authenticated = false;
      await route.fulfill({ json: { changed: true, authenticated: false } });
    } else if (path === '/api/control/services')
      await route.fulfill({ json: { pending_restart: false, services: [] } });
    else if (path === '/api/control/catalog')
      await route.fulfill({ json: { version: 1, datasets: [] } });
    else if (path === '/api/control/jobs')
      await route.fulfill({ json: { jobs: [], persistent: true, history_limit: 20 } });
    else if (path === '/api/control/settings')
      await route.fulfill({
        json: {
          values: {},
          secret_keys: [],
          configured_secrets: [],
          pending_restart: false,
          pending_mount_change: false,
        },
      });
    else if (path === '/api/control/setup')
      await route.fulfill({
        json: {
          version: 1,
          manager: 'operator-compose',
          apply_supported: false,
          applied_profiles: ['demo'],
          desired_profiles: ['demo'],
          pending_restart: false,
          components: [component],
        },
      });
    else if (path === '/api/control/setup/plan') {
      expect(request.postDataJSON()).toEqual({ component_id: 'compute' });
      await route.fulfill({ json: plan });
    } else await route.fulfill({ status: 503, json: { code: 'SYNTHETIC_ROUTE_NOT_ALLOWED' } });
  });
  return writes;
}

test('Access login needs an explicit click and never offers a key fallback', async ({ page }) => {
  const writes = await fixture(page, true);
  await page.goto('/');
  await expect(
    page.getByRole('button', { name: 'Cloudflareでログイン', exact: true }),
  ).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  expect(writes).toEqual([]);
  await page.getByRole('button', { name: 'Cloudflareでログイン', exact: true }).click();
  await expect(page.getByText('Cloudflare認証済み', { exact: true })).toBeVisible();
  expect(writes).toEqual(['/api/session']);
  expect(
    await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })),
  ).toEqual({ local: 0, session: 0 });
});

test('key rotation clears the inputs and requires a new local session', async ({ page }) => {
  const writes = await fixture(page, false, true);
  await page.goto('/#settings');
  await page.getByLabel('現在の接続キー', { exact: true }).fill('synthetic-current-key-123456');
  const inputs = page.locator('.authentication-panel input[type="password"]');
  await inputs.nth(1).fill('synthetic-replacement-key-123456');
  await inputs.nth(2).fill('synthetic-replacement-key-123456');
  const submit = page.getByRole('button', { name: '接続キーを変更して再接続', exact: true });
  await expect(submit).toBeDisabled();
  await page.locator('.authentication-panel input[type="checkbox"]').check();
  await submit.click();
  await expect(page.getByLabel('ワークスペース接続キー', { exact: true })).toHaveValue('');
  await expect(
    page.getByText('接続キーを変更しました。新しいキーで接続してください。', { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual(['/api/auth/credential']);
});

test('guided setup builds a checklist without saving settings or starting services', async ({
  page,
}) => {
  const writes = await fixture(page, false, true);
  await page.goto('/#setup');
  await page.getByRole('button', { name: '準備を始める', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sparkを準備する', exact: true })).toBeFocused();
  await expect(
    page.getByRole('button', { name: '管理者用チェックリストを保存', exact: true }),
  ).toBeDisabled();
  await expect(page.getByText('初回配置は管理者が確認します。', { exact: true })).toBeVisible();
  expect(writes).toEqual(['/api/control/setup/plan']);
  await expect(page.locator('button').filter({ hasText: /^適用$|^今すぐ起動$/ })).toHaveCount(0);
});
