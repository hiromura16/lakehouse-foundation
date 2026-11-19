/** Render the real shared UI without a browser, live server, or fabricated data. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  Badge,
  Button,
  ErrorNotice,
  ExternalLink,
  MiniProgress,
  serviceUrl,
} from '../src/components.js';
import type { Service } from '../src/types.js';

const service: Service = {
  id: 'grafana',
  title: 'Grafana',
  description: 'Synthetic service contract',
  profile: 'monitoring',
  enabled: true,
  configured: true,
  state: 'available',
  url: 'https://grafana.example.invalid/',
  link_mode: 'public',
  link_configured: true,
  missing_settings: [],
};

test('configured external links keep the public origin and isolate the opener', () => {
  const html = renderToStaticMarkup(<ExternalLink service={service} />);
  assert.match(html, /href="https:\/\/grafana\.example\.invalid\/"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /target="_blank"/);
});

test('an unconfigured public URL renders no anchor and never falls back locally', () => {
  const html = renderToStaticMarkup(
    <ExternalLink service={{ ...service, url: '', link_configured: false }} />,
  );
  assert.match(html, /外部URL未設定/);
  assert.doesNotMatch(html, /href=|localhost/);
});

test('disabled profiles and incomplete service settings cannot become active links', () => {
  for (const values of [{ enabled: false }, { configured: false }]) {
    const html = renderToStaticMarkup(<ExternalLink service={{ ...service, ...values }} />);
    assert.match(html, /aria-disabled="true"/);
    assert.doesNotMatch(html, /href=/);
  }
});

test('unsafe or local public destinations cannot render clickable links', () => {
  for (const url of [
    'javascript:alert(1)',
    'http://localhost:3000/',
    'https://localhost/',
    'https://127.0.0.1/',
    'https://[::1]/',
    'https://user@example.invalid/',
  ]) {
    const value = { ...service, url };
    assert.equal(serviceUrl(value), null);
    assert.doesNotMatch(renderToStaticMarkup(<ExternalLink service={value} />), /href=/);
  }
});

test('explicit local mode retains an operator-selected localhost link', () => {
  assert.equal(
    serviceUrl({ ...service, link_mode: 'local', url: 'http://localhost:3000/' }),
    'http://localhost:3000/',
  );
});

test('busy actions are disabled and the loading icon is accessible decoration', () => {
  const html = renderToStaticMarkup(<Button busy>処理を実行</Button>);
  assert.match(html, /disabled=""/);
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /処理を実行/);
});

test('untrusted text is escaped rather than inserted as active HTML', () => {
  const text = '<img src=x onerror=alert(1)>';
  const html = renderToStaticMarkup(
    <>
      <Badge>{text}</Badge>
      <ErrorNotice message={text} />
    </>,
  );
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /role="alert"/);
});

test('progress uses a bounded native element without CSP-unsafe inline CSS', () => {
  const html = renderToStaticMarkup(
    <>
      <MiniProgress value={10} maximum={0} />
      <MiniProgress value={999} maximum={1} />
    </>,
  );
  assert.match(html, /value="0"/);
  assert.match(html, /value="100"/);
  assert.doesNotMatch(html, /style=/);
});
