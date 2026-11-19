/** Same-origin transport. Upstream bearer authentication belongs to gateway. */
const MAX_RESPONSE_BYTES = 1024 * 1024;
const MAX_REQUEST_BYTES = 256 * 1024;
const SAFE_MESSAGES: Record<number, string> = {
  400: '入力内容を確認してください。',
  401: 'セッションの確認が必要です。もう一度接続してください。',
  403: 'この操作は許可されていません。',
  404: 'この環境では操作を利用できません。',
  408: '応答を待つ時間を超えました。状態を確認してください。',
  409: '操作を開始できませんでした。実行中の処理と準備状態を確認してください。',
  413: '入力が256KiBを超えています。',
  422: '入力した項目・形式・範囲を確認してください。',
  429: '要求が集中しています。少し待って確認してください。',
  502: '操作APIに接続できません。配置と内部接続を確認してください。',
  503: '必要なサービス・設定がまだ準備できていません。',
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError(0, '応答を読み取れませんでした。');
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new ApiError(0, '応答が表示上限を超えました。対象を絞ってください。');
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const data = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    data.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data));
  } catch {
    throw new ApiError(0, 'API応答の形式を確認できませんでした。');
  }
}

/** No redirects or browser storage. Unsafe methods carry the CSRF marker. */
async function request<T>(
  path: string,
  method: string,
  payload?: unknown,
  timeout = 25_000,
  rawJson?: string,
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  const body = rawJson ?? (payload === undefined ? undefined : JSON.stringify(payload));
  if (body !== undefined && new TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES) {
    window.clearTimeout(timer);
    throw new ApiError(413, SAFE_MESSAGES[413]);
  }
  try {
    const response = await fetch(path, {
      method,
      credentials: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(method === 'GET' ? {} : { 'Content-Type': 'application/json' }),
        ...(method === 'GET' ? {} : { 'X-Poc-Request': '1' }),
      },
      body,
    });
    if (!response.ok) {
      // Never reflect raw error bodies, commands, credentials or source names.
      if (response.status === 401 && path !== '/api/session') {
        window.dispatchEvent(new Event('studio:unauthorized'));
      }
      await response.body?.cancel();
      throw new ApiError(
        response.status,
        SAFE_MESSAGES[response.status] ?? '操作を完了できませんでした。状態を確認してください。',
      );
    }
    return (await readJson(response)) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(
      0,
      controller.signal.aborted
        ? '応答を待つ時間を超えました。実行操作を繰り返さず、履歴を確認してください。'
        : '操作APIに接続できません。接続先とゲートウェイを確認してください。',
    );
  } finally {
    window.clearTimeout(timer);
  }
}

export function api<T>(
  path: string,
  options: { method?: 'GET' | 'POST' | 'PATCH'; payload?: unknown; timeout?: number } = {},
): Promise<T> {
  if (!path.startsWith('/') || path.startsWith('//') || /[\\\r\n]/.test(path)) {
    return Promise.reject(new ApiError(0, '許可されていないAPIパスです。'));
  }
  return request<T>(
    '/api/control' + path,
    options.method ?? 'GET',
    options.payload,
    options.timeout,
  );
}

/** Preserve uploaded JSON keys verbatim so server duplicate-key guards work. */
export function importMetadata<T>(rawJson: string): Promise<T> {
  return request<T>(
    '/api/control/onboarding/validate-discovery',
    'POST',
    undefined,
    25_000,
    rawJson,
  );
}

export async function getSession(): Promise<boolean> {
  const result = await request<{ authenticated: boolean }>('/api/session', 'GET');
  if (typeof result?.authenticated !== 'boolean')
    throw new ApiError(0, 'session応答の形式を確認できませんでした。');
  return result.authenticated;
}
export async function login(token: string): Promise<boolean> {
  await request<unknown>('/api/session', 'POST', { token });
  return getSession();
}
export async function logout(): Promise<void> {
  await request<void>('/api/session', 'DELETE');
}

/** Download only on an explicit click; no automatic server-side persistence. */
export function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: 'application/json;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
