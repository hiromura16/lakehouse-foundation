import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';

export interface Resource<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  fetchedAt: Date | null;
  refresh: () => Promise<void>;
}

/** Reject missing envelopes before an absent response can become a zero count. */
function validResource(path: string, value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const body = value as Record<string, unknown>;
  if (path === '/services')
    return (
      Array.isArray(body.services) &&
      body.services.every(
        (item) =>
          item &&
          typeof item === 'object' &&
          typeof item.id === 'string' &&
          typeof item.title === 'string' &&
          typeof item.enabled === 'boolean' &&
          typeof item.configured === 'boolean' &&
          typeof item.url === 'string' &&
          ['available', 'unreachable', 'unconfigured', 'not_enabled'].includes(item.state),
      )
    );
  if (path === '/catalog')
    return (
      Array.isArray(body.datasets) &&
      body.datasets.every(
        (item) =>
          item &&
          typeof item === 'object' &&
          typeof item.id === 'string' &&
          typeof item.title === 'string' &&
          typeof item.table === 'string' &&
          Array.isArray(item.columns) &&
          Array.isArray(item.primary_key) &&
          Array.isArray(item.provenance),
      )
    );
  if (path.startsWith('/jobs?'))
    return (
      Array.isArray(body.jobs) &&
      body.jobs.every(
        (item) =>
          item &&
          typeof item === 'object' &&
          typeof item.id === 'string' &&
          typeof item.status === 'string' &&
          typeof item.kind === 'string',
      )
    );
  if (path === '/settings')
    return (
      !!body.values &&
      typeof body.values === 'object' &&
      !Array.isArray(body.values) &&
      Object.values(body.values).every((item) => typeof item === 'string') &&
      Array.isArray(body.secret_keys) &&
      body.secret_keys.every((item) => typeof item === 'string') &&
      Array.isArray(body.configured_secrets) &&
      body.configured_secrets.every((item) => typeof item === 'string')
    );
  return true;
}

/** Ignore old requests after unmount or a newer refresh, without faking data. */
export function useResource<T>(path: string): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const generation = useRef(0);
  const mounted = useRef(false);
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api<T>(path);
      if (!validResource(path, result))
        throw new Error('API応答の構造を確認できません。APIと画面の版を確認してください。');
      if (mounted.current && current === generation.current) {
        setData(result);
        setFetchedAt(new Date());
      }
    } catch (failure) {
      if (mounted.current && current === generation.current) {
        setError(failure instanceof Error ? failure.message : '応答を確認できません。');
      }
    } finally {
      if (mounted.current && current === generation.current) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, [refresh]);
  return { data, loading, error, fetchedAt, refresh };
}
