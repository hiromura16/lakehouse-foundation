import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { AlertCircle, ArrowUpRight, Database, LoaderCircle, RefreshCw } from 'lucide-react';
import type { Service, Tone } from './types.js';

export function Button({
  children,
  className = '',
  variant = 'secondary',
  busy = false,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  busy?: boolean;
}) {
  return (
    <button
      type="button"
      className={`button button-${variant} ${className}`}
      {...props}
      disabled={props.disabled || busy}
    >
      {busy && <LoaderCircle size={15} className="spin" aria-hidden="true" />}
      {children}
    </button>
  );
}
export function Badge({
  children,
  tone = 'muted',
  dot = false,
}: {
  children: ReactNode;
  tone?: Tone;
  dot?: boolean;
}) {
  return (
    <span className={`badge badge-${tone}`}>
      {dot && <i aria-hidden="true" />}
      {children}
    </span>
  );
}
export function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="header-action">{action}</div>}
    </header>
  );
}
export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <Database size={24} />
      </div>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function Loading({ text = '最新の状態を取得しています' }: { text?: string }) {
  return (
    <div className="loading-state" role="status">
      <LoaderCircle className="spin" size={20} />
      <span>{text}</span>
    </div>
  );
}
export function ErrorNotice({ message, retry }: { message: string | null; retry?: () => void }) {
  if (!message) return null;
  return (
    <div className="notice notice-error" role="alert">
      <AlertCircle size={18} />
      <span>{message}</span>
      {retry && (
        <Button variant="ghost" onClick={retry}>
          <RefreshCw size={14} />
          再取得
        </Button>
      )}
    </div>
  );
}
export function Notice({
  children,
  tone = 'info',
}: {
  children: ReactNode;
  tone?: 'info' | 'warning' | 'success';
}) {
  return (
    <div className={`notice notice-${tone}`}>
      <AlertCircle size={17} />
      <div>{children}</div>
    </div>
  );
}
export function Panel({
  title,
  eyebrow,
  children,
  action,
  className = '',
}: {
  title: string;
  eyebrow?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <div>
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}
export const SERVICE_STATES: Record<Service['state'], { label: string; tone: Tone }> = {
  available: { label: '内部稼働', tone: 'green' },
  unreachable: { label: '内部未到達', tone: 'amber' },
  unconfigured: { label: '必要設定が未完了', tone: 'amber' },
  not_enabled: { label: 'profile未選択', tone: 'muted' },
};

/** A browser URL is not a health probe; public mode never uses localhost. */
export function serviceUrl(service: Service): string | null {
  if (service.link_configured === false || !service.url) return null;
  try {
    const url = new URL(service.url);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    if (
      service.link_mode === 'public' &&
      (url.protocol !== 'https:' || ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))
    )
      return null;
    return url.href;
  } catch {
    return null;
  }
}
export function ExternalLink({
  service,
  compact = false,
}: {
  service: Service;
  compact?: boolean;
}) {
  const url = serviceUrl(service);
  const enabled = service.enabled && service.configured && !!url;
  if (!enabled)
    return (
      <span className="disabled-link" aria-disabled="true">
        {!url && service.link_mode === 'public' ? '外部URL未設定' : '準備待ち'}
      </span>
    );
  return (
    <a
      className={compact ? 'text-link' : 'button button-secondary'}
      href={url!}
      target="_blank"
      rel="noopener noreferrer"
    >
      開く
      <ArrowUpRight size={14} />
    </a>
  );
}
export function MiniProgress({ value, maximum }: { value: number; maximum: number }) {
  const percent = maximum > 0 ? Math.max(0, Math.min(100, (value / maximum) * 100)) : 0;
  return <progress className="mini-progress" value={percent} max={100} aria-label="処理の進捗" />;
}
