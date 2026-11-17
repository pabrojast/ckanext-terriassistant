const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
// Keep the real template's elements and attributes; CKAN-specific expressions
// are not evaluated in these DOM tests. Route attributes are supplied below.
const html = fs.readFileSync(path.join(root, 'ckanext/terriassistant/templates/terriassistant.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'ckanext/terriassistant/public/terriassistant/terriassistant.js'), 'utf8');
const fixture = {
  csrf_token: 'session-csrf-token', storage_scope: 'alice',
  can_chat: true, chat_enabled: true, can_save: true,
  login_url: '/wins/user/login?came_from=map',
  saved_spec: { marker_color: 'red' }, active_spec: { marker_color: 'red' },
  default_spec: { marker_color: 'blue' },
  encoded_config: 'saved-config', default_encoded_config: 'default-config',
  warnings: [], suggested_prompts: ['Use blue points']
};
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const turn = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for (let i = 0; i < 5; i++) await turn(); }

async function app(t, options = {}) {
  const dom = new JSDOM(html, { url: 'https://catalogue.test/wins/dataset/example', runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, calls = [];
  const element = d.getElementById('terriassistant-app');
  Object.assign(element.dataset, {
    viewId: 'view-one', bootstrapUrl: '/wins/api/bootstrap',
    chatUrl: '/wins/api/chat', saveUrl: '/wins/api/save',
    terriaInstanceUrl: '/wins/terria/?hideWelcomeMessage=1#old'
  });
  d.getElementById('ta-iframe').src = '/wins/terria/#start=saved-config';
  const data = { ...fixture, ...options.bootstrap };
  for (const [key, value] of Object.entries(options.storage || {})) w.localStorage.setItem(key, value);
  if (options.brokenStorage) {
    Object.defineProperty(w, 'localStorage', { get() { throw new w.DOMException('Blocked', 'SecurityError'); } });
  }
  if (options.late) Object.defineProperty(d, 'readyState', { value: 'complete', configurable: true });
  w.confirm = () => true;
  w.fetch = async (url, request) => {
    calls.push({ url, request });
    if (options.fetch) return options.fetch(url, request, data);
    if (url.endsWith('/bootstrap')) return response(data);
    if (url.endsWith('/save')) return response({ saved_spec: JSON.parse(request.body).draft_spec, encoded_config: 'saved-updated' });
    return response({ action: 'apply_style', next_spec: { marker_color: 'green' }, encoded_config: 'green-config', assistant_message: 'Green points', warnings: [] });
  };
  w.eval(script);
  d.dispatchEvent(new w.Event('DOMContentLoaded'));
  await settle();
  return { w, d, calls, data, click: id => d.getElementById(id).click() };
}

function submit(a, value = 'Use green points') {
  a.d.getElementById('ta-input').value = value;
  a.d.getElementById('ta-chat-form').dispatchEvent(new a.w.Event('submit', { bubbles: true, cancelable: true }));
}

const scopedMessages = 'terriassistant:messages:alice:view-one';
const scopedDraft = 'terriassistant:draft:alice:view-one';

test('late-loaded bundle starts once, with CKAN-prefixed URLs and authenticated controls', async t => {
  const a = await app(t, { late: true });
  a.d.dispatchEvent(new a.w.Event('DOMContentLoaded'));
  await settle();
  assert.equal(a.calls.length, 1);
  assert.equal(a.calls[0].url, '/wins/api/bootstrap');
  assert.equal(a.d.getElementById('ta-input').disabled, false);
  assert.equal(a.d.getElementById('ta-save').classList.contains('ta-hidden'), false);
  assert.equal(a.d.getElementById('ta-login').classList.contains('ta-hidden'), true);
});

test('anonymous viewers can see the map but cannot chat or save and receive a login link', async t => {
  const a = await app(t, { bootstrap: { can_chat: false, can_save: false, storage_scope: 'anonymous' } });
  assert.equal(a.d.getElementById('ta-input').disabled, true);
  assert.equal(a.d.getElementById('ta-save').classList.contains('ta-hidden'), true);
  assert.equal(a.d.getElementById('ta-login').classList.contains('ta-hidden'), false);
  assert.equal(a.d.querySelector('#ta-login a').pathname, '/wins/user/login');
  assert.match(a.d.getElementById('ta-iframe').src, /#start=saved-config$/);
  submit(a); a.click('ta-save');
  await settle();
  assert.equal(a.calls.length, 1);
});

test('chat sends CSRF and serializes keyboard submits, saves and resets while a request is pending', async t => {
  let finish;
  const a = await app(t, { fetch: async (url, request, data) => {
    if (url.endsWith('/bootstrap')) return response(data);
    return new Promise(resolve => { finish = resolve; });
  }});
  submit(a);
  submit(a, 'An overlapping request');
  a.d.getElementById('ta-input').dispatchEvent(new a.w.KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true }));
  a.click('ta-save'); a.click('ta-reset-default');
  await settle();
  assert.equal(a.calls.length, 2);
  assert.equal(a.calls[1].request.headers['X-CSRFToken'], fixture.csrf_token);
  assert.equal(a.calls[1].request.credentials, 'same-origin');
  assert.equal(a.d.getElementById('ta-save').disabled, true);
  finish(response({ action: 'apply_style', next_spec: { marker_color: 'green' }, encoded_config: 'green-config', assistant_message: 'Green points' }));
  await settle();
  assert.equal(a.d.getElementById('ta-send').disabled, false);
  assert.deepEqual(JSON.parse(a.w.localStorage.getItem(scopedDraft)).spec, { marker_color: 'green' });
});

test('reset to default previews defaults as a recoverable draft and requires explicit save', async t => {
  const a = await app(t);
  a.click('ta-reset-default');
  await new Promise(resolve => setTimeout(resolve, 45));
  assert.equal(a.calls.length, 1);
  assert.match(a.d.getElementById('ta-iframe').src, /#start=default-config$/);
  assert.match(a.d.getElementById('ta-iframe').src, /hideWelcomeMessage=1/);
  assert.deepEqual(JSON.parse(a.w.localStorage.getItem(scopedDraft)).spec, fixture.default_spec);
  assert.match(a.d.getElementById('ta-status').textContent, /saved map has not changed/);
  a.click('ta-save'); await settle();
  assert.equal(a.calls[1].url, '/wins/api/save');
  assert.equal(a.calls[1].request.headers['X-CSRFToken'], fixture.csrf_token);
  assert.deepEqual(JSON.parse(a.calls[1].request.body).draft_spec, fixture.default_spec);
  assert.equal(a.w.localStorage.getItem(scopedDraft), null);
  assert.equal(a.d.getElementById('ta-status').textContent, 'Map saved.');
});

test('blocked browser storage does not abort map loading, chat or successful server saves', async t => {
  const a = await app(t, { brokenStorage: true });
  assert.equal(a.d.getElementById('ta-send').disabled, false);
  assert.match(a.d.getElementById('ta-warnings').textContent, /Browser storage is unavailable/);
  submit(a); await settle();
  a.click('ta-save'); await settle();
  assert.equal(a.d.getElementById('ta-status').textContent, 'Map saved.');
  assert.equal(a.calls.length, 3);
});

test('malformed histories and another account or legacy drafts are not restored', async t => {
  const a = await app(t, { storage: {
    [scopedMessages]: '{"not":"a conversation"}',
    'terriassistant:messages:bob:view-one': '[{"role":"user","content":"private question"}]',
    'terriassistant:draft:view-one': '{"spec":{"marker_color":"secret"},"encoded":"legacy"}',
    'terriassistant:draft:bob:view-one': '{"spec":{"marker_color":"other"},"encoded":"other"}'
  }});
  assert.equal(a.d.getElementById('ta-send').disabled, false);
  assert.doesNotMatch(a.d.getElementById('ta-messages').textContent, /private question/);
  assert.equal(a.d.getElementById('ta-draft-banner').classList.contains('ta-hidden'), true);
});

test('explicit draft recovery and reset to saved change only the preview', async t => {
  const a = await app(t, { storage: { [scopedDraft]: JSON.stringify({ spec: { marker_color: 'green' }, encoded: 'draft-config' }) } });
  assert.equal(a.d.getElementById('ta-draft-banner').classList.contains('ta-hidden'), false);
  assert.match(a.d.getElementById('ta-iframe').src, /#start=saved-config$/);
  a.click('ta-use-draft');
  await new Promise(resolve => setTimeout(resolve, 45));
  assert.match(a.d.getElementById('ta-iframe').src, /#start=draft-config$/);
  a.click('ta-reset-saved'); await settle();
  await new Promise(resolve => setTimeout(resolve, 45));
  assert.equal(a.w.localStorage.getItem(scopedDraft), null);
  assert.match(a.d.getElementById('ta-iframe').src, /#start=saved-config$/);
  assert.equal(a.calls.filter(c => c.request.method === 'POST').length, 0);
});

test('HTML/server failures preserve the question for retry and fail bootstrap controls closed', async t => {
  const a = await app(t, { fetch: async (url, request, data) => url.endsWith('/bootstrap') ? response(data) : {
    ok: false, status: 502, json: async () => { throw new SyntaxError('not JSON'); }
  }});
  submit(a, 'Keep this question'); await settle();
  assert.equal(a.d.getElementById('ta-input').value, 'Keep this question');
  assert.deepEqual(JSON.parse(a.w.localStorage.getItem(scopedMessages)), []);
  assert.match(a.d.getElementById('ta-warnings').textContent, /HTTP 502/);
  assert.equal(a.d.getElementById('ta-send').disabled, false);
  const b = await app(t, { fetch: async () => response({ message: 'Not authorized' }, 403) });
  assert.equal(b.d.getElementById('ta-input').disabled, true);
  assert.equal(b.d.getElementById('ta-save').classList.contains('ta-hidden'), true);
  assert.match(b.d.getElementById('ta-warnings').textContent, /Not authorized/);
});

test('long conversations send a bounded context without deleting visible history', async t => {
  const history = Array.from({ length: 120 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: 'Message ' + i }));
  const a = await app(t, { storage: { [scopedMessages]: JSON.stringify(history) } });
  submit(a); await settle();
  const sent = JSON.parse(a.calls[1].request.body).messages;
  assert.equal(sent.length, 100);
  assert.equal(sent.at(-1).content, 'Use green points');
  assert.equal(JSON.parse(a.w.localStorage.getItem(scopedMessages)).length, 122);
});

test('clarification retains a renderable draft and empty default styling is recoverable', async t => {
  const a = await app(t, { bootstrap: { default_spec: {} }, fetch: async (url, request, data) => url.endsWith('/bootstrap') ? response(data) : response({
    action: 'ask_clarification', next_spec: {}, encoded_config: 'default-config', assistant_message: 'Which column?'
  }) });
  a.click('ta-reset-default');
  assert.deepEqual(JSON.parse(a.w.localStorage.getItem(scopedDraft)).spec, {});
  assert.equal(a.d.getElementById('ta-summary').textContent, 'Default map styling');
  submit(a); await settle();
  const stored = JSON.parse(a.w.localStorage.getItem(scopedDraft));
  assert.deepEqual(stored.spec, {});
  assert.equal(stored.encoded, 'default-config');
});
