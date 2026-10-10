import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../claudlobby/plane/ui/owner-entry.js', import.meta.url), 'utf8');
const { mountOwnerEntry } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
const code = 'a'.repeat(43);
const pairing = {
  state: 'awaiting_local_approval', challenge: code,
  principal: { namespace: 'test-verifier', subject: '<human-001>' }, expires_at: 1800000300,
};
function harness(replies = [{ state: 'needs_pairing' }]) {
  const nodes = new Map();
  const document = { getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, { textContent: '', hidden: false, disabled: false,
      addEventListener(event, fn) { this[event] = fn; } });
    return nodes.get(id);
  } };
  let time = 1800000000000;
  let tick;
  const calls = [];
  const entry = mountOwnerEntry({ document, now: () => time,
    setInterval(fn) { tick = fn; return 1; }, clearInterval() {},
    setTimeout() { return 1; }, clearTimeout() {},
    async fetch(url, options) {
      calls.push({ url, options });
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      if (typeof reply === 'function') return reply();
      assert.ok(reply, 'unexpected request');
      return { status: reply.status ?? (reply.state === 'awaiting_local_approval' ? 202 : 200),
        async json() { return reply; } };
    },
  });
  const node = id => document.getElementById(`owner-${id}`);
  return { entry, calls, replies, node,
    async click(id) { assert.equal(node(id).disabled, false); node(id).click(); await flush(); },
    advance(milliseconds) { time += milliseconds; tick(); },
  };
}

test('pairing displays exact identity and code, checks approval, and requires explicit sign-in', async () => {
  const h = harness([{ state: 'needs_pairing' }, pairing, { state: 'needs_pairing' },
    { state: 'sign_in_required' }, { state: 'ready' }]);
  await h.entry.ready;
  assert.deepEqual(h.calls.map(c => c.url), ['/api/owner/status']);
  assert.equal(h.node('pair').hidden, false);
  await h.click('pair');
  assert.equal(h.node('namespace').textContent, 'test-verifier');
  assert.equal(h.node('subject').textContent, '<human-001>');
  assert.equal(h.node('challenge').textContent, code);
  assert.ok(h.node('expires').textContent);
  assert.equal(h.node('login').hidden, true);
  await h.click('check');
  assert.equal(h.node('challenge').textContent, code);
  assert.equal(h.node('check').textContent, 'Check approval');
  await h.click('check');
  assert.equal(h.node('challenge').textContent, '');
  assert.equal(h.node('pairing').hidden, true);
  assert.equal(h.node('login').hidden, false);
  assert.ok(h.calls.every(c => !c.url.endsWith('/login')));
  await h.click('login');
  assert.equal(h.node('open').hidden, false);
  assert.equal(h.calls.at(-1).options.body, '{}');
  assert.equal(h.calls.at(-1).options.credentials, 'same-origin');
  assert.equal(h.calls.at(-1).options.headers['X-Claudlobby-Owner'], '1');
  h.entry.dispose();
});

test('logout ends the displayed session and never automatically signs back in', async () => {
  const h = harness([{ state: 'ready' }, { state: 'signed_out' }]);
  await h.entry.ready;
  await h.click('logout');
  assert.match(h.node('status').textContent, /Signed out.*Local owner pairing remains/);
  assert.equal(h.node('login').hidden, false);
  assert.equal(h.node('open').hidden, true);
  assert.deepEqual(h.calls.map(c => c.url), ['/api/owner/status', '/api/owner/logout']);
  h.entry.dispose();
});

test('unavailable authority and network errors never claim an unpaired identity', async () => {
  const h = harness([{ state: 'unavailable', status: 503 }, new Error('private diagnostic'),
    { state: 'sign_in_required' }]);
  await h.entry.ready;
  assert.match(h.node('status').textContent, /does not mean you are unpaired/);
  assert.equal(h.node('pair').hidden, true);
  await h.click('check');
  assert.doesNotMatch(h.node('status').textContent, /private diagnostic/);
  assert.equal(h.node('pair').hidden, true);
  await h.click('check');
  assert.equal(h.node('login').hidden, false);
  h.entry.dispose();
});

test('challenge expiry erases credentials and offers explicit recovery without writes', async () => {
  const h = harness([{ state: 'needs_pairing' }, pairing]);
  await h.entry.ready;
  await h.click('pair');
  h.advance(301000);
  for (const id of ['namespace', 'subject', 'expires', 'challenge']) assert.equal(h.node(id).textContent, '');
  assert.match(h.node('status').textContent, /expired/);
  assert.equal(h.node('pair').hidden, false);
  assert.equal(h.node('pairing').hidden, true);
  assert.equal(h.calls.length, 2);
  h.entry.dispose();
});

test('in-flight requests disable controls, prevent duplicate mutations and abort on disposal', async () => {
  let settle;
  let signal;
  const h = harness([{ state: 'needs_pairing' }, () => new Promise(resolve => { settle = resolve; })]);
  await h.entry.ready;
  h.node('pair').click();
  signal = h.calls.at(-1).options.signal;
  for (const id of ['pair', 'check', 'login', 'logout']) assert.equal(h.node(id).disabled, true);
  h.node('pair').click();
  h.node('check').click();
  assert.equal(h.calls.length, 2);
  h.entry.dispose();
  assert.equal(signal.aborted, true);
  settle({ status: 202, json: async () => pairing });
  await flush();
  assert.equal(h.node('challenge').textContent, '');
});

test('malformed or refused pairing never displays a code or offers sign-in', async () => {
  const h = harness([{ state: 'needs_pairing' }, { ...pairing, challenge: 'invalid' },
    { state: 'denied', status: 403 }]);
  await h.entry.ready;
  await h.click('pair');
  assert.equal(h.node('challenge').textContent, '');
  assert.equal(h.node('login').hidden, true);
  await h.click('check');
  assert.match(h.node('status').textContent, /refused/);
  assert.equal(h.node('pair').hidden, true);
  h.entry.dispose();
});
