import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
const source = await readFile(new URL('../claudlobby/plane/ui/owner-api-client.js', import.meta.url), 'utf8');
const load = text => import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const { createOwnerTransport } = await load(source);
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
const readProfile = { version: 1, profile: 'direct-owner-read-v1', host_uid: 'host_' + '1'.repeat(32) };
const ready = { status: 200, data: { state: 'ready', read_profile: readProfile } };
const denied = { status: 403, data: { state: 'denied' } };
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
function harness(replies = [ready], hooks = {}) {
  const monotonicNow = hooks.monotonicNow || (() => 0);
  const calls = [], natives = [], replacements = [], timers = new Set(), timeouts = [], nodes = new Map();
  let paused = 0, resumed = 0;
  const document = { getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, { textContent: '', hidden: true, disabled: false,
      addEventListener(name, callback) { this[name] = callback; } });
    return nodes.get(id);
  } };
  class NativeStream {
    constructor(url, options) { this.url = url; this.options = options; this.listeners = {}; natives.push(this); }
    close() { this.closed = true; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const api = createOwnerTransport({
    monotonicNow,
    EventSource: NativeStream, location: { replace(path) { replacements.push(path); } },
    setTimeout(callback, duration) { timers.add(callback); timeouts.push(duration); return callback; }, clearTimeout(callback) { timers.delete(callback); },
    async fetch(url, options) {
      calls.push({ url, options });
      let reply = replies.shift();
      assert.ok(reply, `unexpected request ${url}`);
      if (typeof reply === 'function') reply = await reply(options);
      if (reply instanceof Error) throw reply;
      return { status: reply.status, async json() {
        if (reply.data instanceof Error) throw reply.data;
        return reply.data;
      } };
    },
  });
  const node = id => document.getElementById(`owner-session-${id}`);
  const element = document.getElementById('owner-session');
  const controls = api.mountSessionControls({ document, element,
    onPause() { paused++; hooks.onPause?.(api); }, onResume() { resumed++; hooks.onResume?.(api); }, onActionPause(scope, kind, recipient) { hooks.onActionPause?.(api, scope, kind, recipient); } });
  return { api, calls, natives, replacements, timers, timeouts, replies, node, element, controls,
    get resumed() { return resumed; }, get paused() { return paused; },
    async click(id) { node(id).click(); await flush(); },
  };
}

test('default and synthetic transports do not mount session controls or probe owner APIs', async () => {
  const oldFetch = globalThis.fetch, oldDocument = globalThis.document, oldLocation = globalThis.location;
  const calls = [];
  globalThis.fetch = async url => { calls.push(url); return { json: async () => [] }; };
  globalThis.document = { createElement: () => ({ style: {} }), body: { prepend() {} } };
  globalThis.location = { origin: 'http://fixture.example.test', search: '' };
  try {
    const normal = await load(await readFile(new URL('../claudlobby/plane/ui/api-client.js', import.meta.url), 'utf8'));
    assert.equal(normal.mountSessionControls, undefined);assert.equal(normal.nudgeContext,undefined);assert.equal(normal.feedbackContext,undefined);assert.equal(normal.prepareAction,undefined);
    await normal.jget('/api/tasks');
    const synthetic = await load(await readFile(new URL('./fixtures/plane_work_loop/api-client.js', import.meta.url), 'utf8'));
    assert.equal(synthetic.mountSessionControls, undefined);assert.equal(synthetic.nudgeContext,undefined);assert.equal(synthetic.feedbackContext,undefined);assert.equal(synthetic.prepareAction,undefined);
    await synthetic.jget('/api/tasks');
    await synthetic.jget('/api/channel');
    assert.deepEqual(calls, ['/api/tasks', '/fixture/records']);
  } finally { globalThis.fetch = oldFetch; globalThis.document = oldDocument; globalThis.location = oldLocation; }
});

test('owner mount checks current session and exposes explicit renewal without clock assumptions', async () => {
  const h = harness(); await h.controls.ready;
  assert.equal(h.element.hidden, false);
  assert.equal(h.node('renew').disabled, false);
  assert.equal(h.calls[0].url, '/api/owner/status');
  assert.match(h.node('status').textContent, /renew before it expires/);
  assert.equal(h.resumed, 1);
  assert.equal(h.timers.size, 0);
  h.controls.dispose();
});

test('renewal fences delayed old-cookie403 and old SSE events, then reconnects and refreshes', async () => {
  const late = deferred(), renewed = deferred();
  const h = harness([ready, () => late.promise, () => renewed.promise]); await h.controls.ready;
  const stream = h.api.createEventSource('/api/stream'); let messages = 0;
  stream.onmessage = () => { messages++; };
  const old = h.natives[0];
  const read = h.api.jget('/api/tasks');
  h.node('renew').click();
  assert.equal(old.closed, true);
  assert.equal(h.calls[1].options.signal.aborted, true);
  assert.equal(h.node('logout').disabled, true);
  renewed.resolve(ready); await flush();
  assert.equal(h.natives.length, 2);
  assert.equal(h.resumed, 2); // renew paints immediately even when SSE is buffered
  h.natives[1].onopen({});
  assert.equal(h.resumed, 3); // then closes the snapshot/stream gap
  assert.match(h.node('status').textContent, /Session renewed/);
  old.onmessage({ data: 'old' }); assert.equal(messages, 0);
  h.natives[1].onmessage({ data: 'new' }); assert.equal(messages, 1);
  late.resolve(denied); assert.equal(await read, null);
  assert.deepEqual(h.replacements, []);
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 1);
  h.controls.dispose();
});

test('renew and logout serialize locally and successful logout stops all private reads', async () => {
  const done = deferred();
  const h = harness([ready, () => done.promise]); await h.controls.ready;
  const stream = h.api.createEventSource('/api/stream');
  h.node('logout').click(); h.node('renew').click(); h.node('logout').click();
  assert.equal(h.calls.length, 2);
  done.resolve({ status: 200, data: { state: 'signed_out' } }); await flush();
  assert.deepEqual(h.replacements, ['/owner']);
  assert.match(h.node('status').textContent, /Signed out.*pairing remains/);
  assert.equal(await h.api.jget('/api/tasks'), null);
  assert.equal(h.calls.length, 2);
  assert.equal(h.natives[0].closed, true);
  stream.close(); h.controls.dispose();
});

test('lost logout response never claims success, and explicit session check can resume', async () => {
  const h = harness([ready, new Error('private failure detail'), ready]); await h.controls.ready;
  h.api.createEventSource('/api/stream');
  await h.click('logout');
  assert.match(h.node('status').textContent, /unknown/);
  assert.doesNotMatch(h.node('status').textContent, /Signed out|private failure/);
  assert.deepEqual(h.replacements, []);
  assert.equal(await h.api.jget('/api/tasks'), null);
  await h.click('check');
  assert.equal(h.node('renew').disabled, false);
  assert.equal(h.natives.length, 2);
  assert.equal(h.calls.some(c => /login|pair$/.test(c.url)), false);
  h.controls.dispose();
});

test('five simultaneous current403s use one current-cookie status check then stop and redirect', async () => {
  const pending = Array.from({ length: 5 }, deferred);
  const h = harness([ready, ...pending.map(p => () => p.promise), { status: 200, data: { state: 'sign_in_required' } }]);
  await h.controls.ready; h.api.createEventSource('/api/stream');
  const reads = pending.map(() => h.api.jget('/api/tasks'));
  pending.forEach(p => p.resolve(denied));
  assert.deepEqual(await Promise.all(reads), [null, null, null, null, null]);
  assert.deepEqual(h.replacements, ['/owner']);
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 2);
  assert.equal(h.natives[0].closed, true);
  assert.equal(await h.api.jget('/api/tasks'), null);
  h.controls.dispose();
});

test('another tab cookie rotation recovers through status only, without replaying renewal', async () => {
  const h = harness([ready, denied, ready, { status: 200, data: { state: 'ok', data: ['current'] } }]);
  await h.controls.ready;
  h.api.createEventSource('/api/stream');
  await h.click('renew');
  assert.equal(h.calls.filter(c => c.url === '/api/owner/renew').length, 1);
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 2);
  assert.deepEqual(h.replacements, []);
  assert.deepEqual(await h.api.jget('/api/tasks'), { state: 'ok', data: ['current'] });
  assert.equal(h.natives.length, 2);
  h.controls.dispose();
});

test('private503 and status503 are unavailable, never signed out or unpaired', async () => {
  const h = harness([ready, { status: 503, data: { state: 'unavailable' } }, { status: 503, data: { state: 'unavailable' } }]);
  await h.controls.ready; h.api.createEventSource('/api/stream');
  assert.equal(await h.api.jget('/api/tasks'), null);
  assert.equal(h.natives[0].closed, true);
  await h.click('check');
  assert.match(h.node('status').textContent, /unknown/);
  assert.doesNotMatch(h.node('status').textContent, /Signed out|unpaired/);
  assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('SSE access ending checks current cookie and redirects when session expired', async () => {
  const h = harness([ready, { status: 200, data: { state: 'sign_in_required' } }]);
  await h.controls.ready;
  const stream = h.api.createEventSource('/api/stream'); let errors = 0;
  stream.onerror = () => { errors++; };
  h.natives[0].onerror({}); await flush();
  assert.equal(errors, 1);
  assert.deepEqual(h.replacements, ['/owner']);
  assert.equal(h.natives[0].closed, true);
  h.controls.dispose();
});

test('repeated SSE errors with a valid session stop as unavailable instead of retry-looping', async () => {
  const h = harness([ready, ready]); await h.controls.ready;
  h.api.createEventSource('/api/stream');
  h.natives[0].onerror({}); await flush();
  h.natives[1].onerror({}); await flush();
  assert.match(h.node('status').textContent, /unknown/);
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('requests keep bounded timeout through body, credentials and mutation intent; disposal aborts', async () => {
  const pending = deferred(); const h = harness([ready, () => pending.promise]); await h.controls.ready;
  h.node('renew').click();
  const request = h.calls[1];
  assert.equal(request.options.method, 'POST'); assert.equal(request.options.body, '{}');
  assert.deepEqual(request.options.headers, { 'Content-Type': 'application/json', 'X-Claudlobby-Owner': '1' });
  assert.equal(request.options.credentials, 'same-origin'); assert.equal(request.options.cache, 'no-store');
  assert.equal(request.options.redirect, 'error');
  assert.equal(h.timers.size, 1);
  [...h.timers][0](); assert.equal(request.options.signal.aborted, true);
  h.controls.dispose();
  pending.resolve(ready); await flush();
  assert.equal(h.resumed, 1);
  assert.equal(h.timers.size, 0);
});

test('named SSE listeners survive renewal and stale generations cannot publish', async () => {
  const h = harness([ready, ready]); await h.controls.ready;
  const stream = h.api.createEventSource('/api/stream'); let sources = 0;
  const listener = () => { sources++; };
  stream.addEventListener('source', listener);
  const old = h.natives[0];
  await h.click('renew');
  old.listeners.source({ data: '{}' }); assert.equal(sources, 0);
  h.natives[1].listeners.source({ data: '{}' }); assert.equal(sources, 1);
  stream.removeEventListener('source', listener);
  h.natives[1].listeners.source({ data: '{}' }); assert.equal(sources, 1);
  h.controls.dispose();
});

const context = { version: 1, room: 'synthetic', simulation: false,
  scope: { workspace: 'workspace-example', host: 'host-example', fleet: 'synthetic', viewer: 'stable-viewer' },
  recipients: [{ id: 'bot-example', label: 'Example bot' }], actions: ['message'] };
const action = { request_id: '11111111-1111-4111-8111-111111111111', kind: 'message',
  scope: context.scope, target: { recipient: 'bot-example', task_id: null },
  submitted_at: '2026-01-01T00:00:00.000Z', body: 'Exact message\nwith a second line.' };
const receipt = value => ({ ...value, body: undefined, version: 1, status: 'delivered' });
const ok = data => ({ status: 200, data });

test('owner actions use exact canonical metadata and protected POST headers, stripping receipt bodies', async () => {
  const h = harness([ready, ok(context), ok(receipt(action)), ok(receipt(action))]); await h.controls.ready;
  assert.deepEqual(await h.api.interactionContext('synthetic'), context);
  assert.equal((await h.api.sendAction(action)).status, 'delivered');
  assert.equal((await h.api.actionReceipt({ ...action, room: 'ignore', authority: 'ignore' })).status, 'delivered');
  assert.deepEqual(h.calls.slice(1).map(c => c.url), [
    '/api/owner/actions/context', '/api/owner/actions/send', '/api/owner/actions/receipt']);
  assert.deepEqual(JSON.parse(h.calls[1].options.body), { room: 'synthetic' });
  assert.deepEqual(JSON.parse(h.calls[2].options.body), action);
  const { body, ...metadata } = action;
  assert.deepEqual(JSON.parse(h.calls[3].options.body), metadata);
  for (const call of h.calls.slice(1)) {
    assert.equal(call.options.method, 'POST');
    assert.equal(call.options.credentials, 'same-origin'); assert.equal(call.options.cache, 'no-store');
    assert.equal(call.options.redirect, 'error');
    assert.deepEqual(call.options.headers, { 'Content-Type': 'application/json', 'X-Claudlobby-Owner': '1' });
  }
  assert.deepEqual(h.timeouts, [8000, 8000, 45000, 8000]);
  h.controls.dispose();
});

test('context is unavailable before session readiness and refuses unsupported authority', async () => {
  const checking = deferred();
  const h = harness([() => checking.promise, ok({ ...context, actions: ['feedback'] }), ok(context)]);
  assert.equal(await h.api.interactionContext('synthetic'), null);
  assert.equal(h.calls.length, 1);
  checking.resolve(ready); await h.controls.ready;
  assert.equal(await h.api.interactionContext('all'), null);
  assert.equal(await h.api.interactionContext('synthetic'), null);
  assert.deepEqual(await h.api.interactionContext('synthetic'), context);
  await assert.rejects(h.api.sendAction({ ...action, kind: 'feedback' }), /Unsupported/);
  await assert.rejects(h.api.sendAction({ ...action, target: { ...action.target, task_id: 'task-example' } }), /Unsupported/);
  assert.equal(h.calls.length, 3);
  h.controls.dispose();
});

test('context refusal checks current cookie once and returns null without replay', async () => {
  const h = harness([ready, denied, ready]); await h.controls.ready;
  assert.equal(await h.api.interactionContext('synthetic'), null);
  assert.equal(h.calls.filter(c => c.url.endsWith('/context')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 2);
  assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('read-only owner context403 does not loop through lifecycle resume or disable the board', async () => {
  const contexts = [], board = { state: 'ok', data: { tasks: [] } };
  const h = harness([ready, denied, ready, ok(board)], {
    onResume(api) { contexts.push(api.interactionContext('synthetic')); },
  });
  await h.controls.ready; await flush();
  assert.deepEqual(await Promise.all(contexts), [null]);
  assert.equal(h.calls.filter(c => c.url.endsWith('/context')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 2);
  assert.equal(h.resumed, 1); assert.equal(h.paused, 1);
  assert.deepEqual(h.replacements, []);
  assert.equal(h.node('renew').disabled, false);
  assert.deepEqual(await h.api.jget('/api/tasks'), board);
  h.controls.dispose();
});

test('revoked message grant retains the original UUID and active private read stream', async () => {
  const { ActionState } = await load(await readFile(new URL('../claudlobby/plane/ui/action-state.js', import.meta.url), 'utf8'));
  const values = new Map(), storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  const state = new ActionState(storage);
  const request = state.begin(context, 'message', action.target, action.body, action.request_id);
  const board = { state: 'ok', data: { tasks: [] } };
  const h = harness([ready, denied, ready, ok(board)]); await h.controls.ready;
  h.api.createEventSource('/api/stream');
  await assert.rejects(h.api.sendAction(request), /outcome unknown/);
  assert.equal(new ActionState(storage).pending[0].request_id, request.request_id);
  assert.equal(h.natives[0].closed, undefined);
  assert.equal(h.paused, 1); assert.equal(h.resumed, 1);
  assert.deepEqual(h.replacements, []);
  assert.deepEqual(await h.api.jget('/api/tasks'), board);
  assert.equal(h.calls.filter(c => c.url.endsWith('/send')).length, 1);
  h.controls.dispose();
});

test('action-only503, malformed JSON and network failure preserve read access and saved requests', async () => {
  const { ActionState } = await load(await readFile(new URL('../claudlobby/plane/ui/action-state.js', import.meta.url), 'utf8'));
  for (const failure of [{ status: 503, data: { state: 'unavailable' } },
    ok(new Error('private JSON error')), new Error('private network error')]) {
    const values = new Map(), storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
    const state = new ActionState(storage);
    const request = state.begin(context, 'message', action.target, action.body, action.request_id);
    const board = { state: 'ok', data: { tasks: [] } };
    const h = harness([ready, failure, failure, ok(board)]); await h.controls.ready;
    h.api.createEventSource('/api/stream');
    assert.equal(await h.api.interactionContext('synthetic'), null);
    await assert.rejects(h.api.sendAction(request), /outcome unknown/);
    assert.equal(new ActionState(storage).pending[0].request_id, request.request_id);
    assert.equal(h.natives[0].closed, undefined);
    assert.equal(h.paused, 1); assert.equal(h.resumed, 1);
    assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 1);
    assert.deepEqual(await h.api.jget('/api/tasks'), board);
    assert.deepEqual(h.replacements, []);
    h.controls.dispose();
  }
});

test('concurrent action403s share a direct status probe and genuine expiry still stops reads', async () => {
  const status = deferred();
  const h = harness([ready, denied, denied, () => status.promise]); await h.controls.ready;
  const send = assert.rejects(h.api.sendAction(action), /outcome unknown/);
  const lookup = assert.rejects(h.api.actionReceipt(action), /outcome unknown/);
  await flush(); assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 2);
  status.resolve(ready); await Promise.all([send, lookup]);
  assert.equal(h.paused, 1); assert.equal(h.resumed, 1); assert.deepEqual(h.replacements, []);
  h.controls.dispose();
  const expired = harness([ready, denied, ok({ state: 'sign_in_required' })]); await expired.controls.ready;
  expired.api.createEventSource('/api/stream');
  await assert.rejects(expired.api.sendAction(action), /outcome unknown/);
  assert.deepEqual(expired.replacements, ['/owner']);
  assert.equal(expired.natives[0].closed, true);
  assert.equal(await expired.api.jget('/api/tasks'), null);
  expired.controls.dispose();
});

test('a lost send reply retains metadata and original UUID across reload, with no replay', async () => {
  const { ActionState } = await load(await readFile(new URL('../claudlobby/plane/ui/action-state.js', import.meta.url), 'utf8'));
  const values = new Map(), storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  const state = new ActionState(storage);
  const request = state.begin(context, 'message', action.target, action.body, action.request_id);
  const h = harness([ready, new Error('private transport detail'), ready, ok(receipt(request))]); await h.controls.ready;
  await assert.rejects(h.api.sendAction(request), error => /outcome unknown/.test(error.message) && !/private/.test(error.message));
  assert.equal(h.calls.filter(c => c.url.endsWith('/send')).length, 1);
  const reloaded = new ActionState(storage);
  assert.equal(reloaded.pending[0].request_id, action.request_id);
  assert.equal(JSON.stringify([...values.values()]).includes(action.body), false);
  await h.click('check');
  reloaded.accept(reloaded.pending[0], await h.api.actionReceipt(reloaded.pending[0]));
  assert.equal(reloaded.pending.length, 0);
  assert.equal(h.calls.filter(c => c.url.endsWith('/send')).length, 1);
  h.controls.dispose();
});

test('send timeout is bounded past lifecycle timeout and rejects a late body without resending', async () => {
  const late = deferred(); const h = harness([ready, () => late.promise]); await h.controls.ready;
  const send = assert.rejects(h.api.sendAction(action), /outcome unknown/);
  assert.equal(h.timeouts.at(-1), 45000);
  [...h.timers][0](); assert.equal(h.calls[1].options.signal.aborted, true);
  late.resolve(ok(receipt(action))); await send;
  assert.equal(h.calls.length, 2);
  assert.equal(h.node('renew').disabled, false);
  h.controls.dispose();
});

test('renewal fences a delayed action403, preserving renewed cookie and allowing receipt-only recovery', async () => {
  const late = deferred(); const h = harness([ready, () => late.promise, ready, ok(receipt(action))]);
  await h.controls.ready;
  const send = assert.rejects(h.api.sendAction(action), /outcome unknown/);
  await h.click('renew'); assert.equal(h.calls[1].options.signal.aborted, true);
  late.resolve(denied); await send;
  assert.deepEqual(h.replacements, []);
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 1);
  assert.equal((await h.api.actionReceipt(action)).status, 'delivered');
  assert.equal(h.calls.filter(c => c.url.endsWith('/send')).length, 1);
  h.controls.dispose();
});

test('logout fences delayed action success and prohibits subsequent sends or receipt reads', async () => {
  const late = deferred(); const h = harness([ready, () => late.promise, ok({ state: 'signed_out' })]);
  await h.controls.ready;
  const send = assert.rejects(h.api.sendAction(action), /outcome unknown/);
  await h.click('logout'); late.resolve(ok(receipt(action))); await send;
  await assert.rejects(h.api.sendAction(action), /outcome unknown/);
  await assert.rejects(h.api.actionReceipt(action), /outcome unknown/);
  assert.deepEqual(h.replacements, ['/owner']);
  assert.equal(h.calls.length, 3);
  h.controls.dispose();
});

test('send403 checks current cookie once and reports unknown even when another tab renewed', async () => {
  const h = harness([ready, denied, ready]); await h.controls.ready;
  await assert.rejects(h.api.sendAction(action), /outcome unknown/);
  assert.equal(h.calls.filter(c => c.url.endsWith('/send')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 2);
  assert.deepEqual(h.replacements, []);
  assert.equal(h.node('renew').disabled, false);
  h.controls.dispose();
});

for (const renewal of ['same scope', 'new viewer', 'removed recipient', 'no message capability'])
test(`app renewal restores worker selection and draft only with fresh matching authority: ${renewal}`, async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const pause = app.match(/onPause\(\) \{([\s\S]*?)\n\s*\},\n\s*onResume/)[1];
  const sync = app.slice(app.indexOf('function syncWorkRoom()'), app.indexOf('const ownerSession ='));
  const controller = (await readFile(new URL('../claudlobby/plane/ui/work-loop.js', import.meta.url), 'utf8'))
    .replaceAll('from "/action-state.js"', `from "${new URL('../claudlobby/plane/ui/action-state.js', import.meta.url)}"`)
    .replaceAll('from "/panel-state.js"', `from "${new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url)}"`);
  const { mountWorkLoop } = await load(controller);
  const { ActionState } = await load(await readFile(new URL('../claudlobby/plane/ui/action-state.js', import.meta.url), 'utf8'));
  const elements = new Map(), values = new Map();
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { value: '', hidden: false, disabled: false,
      innerHTML: '', querySelectorAll: () => [], addEventListener(name, callback) { this[name] = callback; } });
    return elements.get(id);
  } };
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  // Keep one earlier uncertain request for another recipient while editing an unsent draft.
  const state = new ActionState(storage);
  let access = { ...context, recipients: [...context.recipients,
    { id: 'worker-bot', label: 'Worker' }, { id: 'other-bot', label: 'Other bot' }] };
  state.begin(access, 'message', { recipient: 'other-bot', task_id: null }, 'Earlier uncertain message', action.request_id);
  const oldDocument = globalThis.document, oldStorage = globalThis.sessionStorage;
  globalThis.document = document; globalThis.sessionStorage = storage;
  try {
    let paused = false; const rooms = [];
    const workLoop = mountWorkLoop({ api: {
      interactionContext(room) { rooms.push(room); return paused ? null : access; },
      sendAction() { throw Error('No sends expected'); }, actionReceipt() { throw Error('No receipt reads expected'); },
    }, renderThread() {}, refresh() {} });
    workLoop.setRoom('synthetic'); await flush();
    const recipient = document.getElementById('work-recipient');
    recipient.value = 'worker-bot'; recipient.onchange();
    const body = document.getElementById('work-body'); body.value = 'Keep this unsent draft'; body.input();
    const before = storage.getItem('plane.pending-actions.v1');
    assert.equal(document.getElementById('work-send').disabled, false);
    const bindings = { workLoop, workRoom: 'synthetic', currentFleet: 'synthetic', fleets: [],
      sessionPaused: false, sessionEpoch: 1, generation: 1, trustGen: 1,
      refreshTimer: null, safetyTimer: null, searchTimer: null,
      clearTimeout() {}, $: id => document.getElementById(id), showLoading() {} };
    paused = true; runInNewContext(`(function() { ${pause} })()`, bindings);
    assert.equal(bindings.workRoom, undefined);
    assert.equal(document.getElementById('work-send').disabled, true);
    assert.equal(document.getElementById('work-form').hidden, true);
    await flush();
    if (renewal === 'new viewer') access = { ...access, scope: { ...access.scope, viewer: 'new-authority-viewer' } };
    if (renewal === 'removed recipient') access = { ...access, recipients: access.recipients.filter(r => r.id !== 'worker-bot') };
    if (renewal === 'no message capability') access = { ...access, actions: [] };
    paused = false; runInNewContext(`${sync}\nsyncWorkRoom()`, bindings); await flush();
    assert.deepEqual(rooms, ['synthetic', 'synthetic']);
    assert.doesNotMatch(document.getElementById('work-notice').textContent, /host has not enabled/);
    assert.equal(recipient.value, renewal === 'same scope' ? 'worker-bot' : 'bot-example');
    assert.equal(body.value, renewal === 'same scope' ? 'Keep this unsent draft' : '');
    assert.equal(document.getElementById('work-send').disabled, renewal === 'no message capability');
    assert.equal(storage.getItem('plane.pending-actions.v1'), before);
  } finally { await flush(); globalThis.document = oldDocument; globalThis.sessionStorage = oldStorage; }
});


test('explicit submission refusal preserves read session and invalidates action capability without recreating grants', async () => {
  let invalidated = 0;
  const h = harness([ready, {status:403,data:{state:'denied',effect:'not_started'}}, ready],
    {onActionPause(){ invalidated++; }});
  await h.controls.ready;
  await assert.rejects(h.api.sendAction(action), e => e.effect === 'not_started');
  assert.equal(invalidated, 1);
  assert.equal(h.paused, 1); assert.equal(h.resumed, 1);
  assert.deepEqual(h.calls.map(c=>c.url), ['/api/owner/status','/api/owner/actions/send','/api/owner/status']);
  assert.deepEqual(h.replacements, []);
});

test('not_started is per send invocation only; receipt refusal preserves original uncertainty', async () => {
  const h = harness([ready, {status:503,data:{state:'unavailable',effect:'not_started'}},
    {status:403,data:{state:'denied',effect:'not_started'}}, ready]);
  await h.controls.ready;
  await assert.rejects(h.api.sendAction(action), e => e.effect === 'not_started');
  await assert.rejects(h.api.actionReceipt(action), e => !e.effect && /outcome unknown/.test(e.message));
  assert.equal(h.calls.filter(c=>c.url.endsWith('/send')).length, 1);
});


for (const lifecycle of ['status','renew','logout']) test(`aborted403 ${lifecycle} body is unavailable and never replayed`, async () => {
  const body = deferred(), started = deferred(), timers = new Set(), calls = [], replacements = [];
  const nodes = new Map();
  const doc = {getElementById(id){if(!nodes.has(id)) nodes.set(id,{disabled:false,addEventListener(n,f){this[n]=f;}});return nodes.get(id);}};
  const api = createOwnerTransport({location:{replace(p){replacements.push(p);}},
    setTimeout(f){timers.add(f);return f;},clearTimeout(f){timers.delete(f);},
    async fetch(url){
      calls.push(url);
      if(lifecycle !== 'status' && calls.length === 1) return {status:200,json:async()=>ready.data};
      return {status:403,json:async()=>{started.resolve();return body.promise;}};
    }});
  const controls = api.mountSessionControls({document:doc,element:{}});
  if(lifecycle !== 'status') {
    await controls.ready;
    doc.getElementById(`owner-session-${lifecycle}`).click();
  }
  await started.promise;
  for(const timeout of [...timers]) timeout();
  body.resolve({state:'denied'});
  await controls.ready; await flush();
  assert.match(doc.getElementById('owner-session-status').textContent,/Session state is unknown/);
  assert.deepEqual(replacements,[]);
  assert.equal(calls.filter(c=>c.endsWith('/'+lifecycle)).length,1);
  api.dispose?.();
});

test('initial ready session paints boards before an existing stream opens', async () => {
  const status = deferred(), h = harness([() => status.promise]);
  h.api.createEventSource('/api/stream');
  assert.equal(h.resumed, 0); assert.equal(h.natives.length, 0);
  status.resolve(ready); await h.controls.ready;
  assert.equal(h.resumed, 1); assert.equal(h.natives.length, 1);
  h.natives[0].onopen({}); assert.equal(h.resumed, 2);
  h.controls.dispose();
});

test('panel500,422,404 and malformed JSON do not interrupt healthy reads or SSE', async () => {
  const board = { state: 'ok', data: ['board'] };
  const h = harness([ready, { status: 500, data: new SyntaxError('HTML response') },
    { status: 422, data: {} }, { status: 404, data: {} },
    { status: 200, data: new SyntaxError('bad JSON') }, { status: 200, data: board }]);
  await h.controls.ready; h.api.createEventSource('/api/stream');
  for (let i = 0; i < 4; i++) assert.equal(await h.api.jget('/api/search'), null);
  assert.deepEqual(await h.api.jget('/api/tasks'), board);
  assert.equal(h.natives[0].closed, undefined);
  assert.equal(h.paused, 1); assert.equal(h.resumed, 1);
  assert.equal(h.timers.size, 0); assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('network failure permits one automatic status GET, with no repeated recovery or mutation replay', async () => {
  const h = harness([ready, new Error('network detail'), ready, { status: 503, data: {} }]);
  await h.controls.ready; h.api.createEventSource('/api/stream');
  assert.equal(await h.api.jget('/api/tasks'), null);
  assert.equal(h.natives[0].closed, true); assert.equal(h.timers.size, 1);
  const recovery = [...h.timers][0]; h.timers.delete(recovery); recovery(); await flush();
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 2);
  assert.equal(h.resumed, 2); assert.equal(h.natives.length, 2);
  assert.equal(await h.api.jget('/api/tasks'), null);
  assert.equal(h.timers.size, 0); // ready status alone never rearms another recovery
  assert.equal(h.calls.some(c => c.options.method !== 'GET'), false);
  assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('disposed unavailable session cancels its automatic status probe', async () => {
  const h = harness([ready, { status: 503, data: {} }]); await h.controls.ready;
  await h.api.jget('/api/tasks'); const recovery = [...h.timers][0];
  h.controls.dispose(); assert.equal(h.timers.size, 0);
  recovery(); await flush(); assert.equal(h.calls.length, 2);
});

test('refused logout with a still-ready cookie explicitly reports incomplete sign-out', async () => {
  const h = harness([ready, denied, ready]); await h.controls.ready;
  h.api.createEventSource('/api/stream'); await h.click('logout');
  assert.match(h.node('status').textContent, /Sign-out did not complete.*still signed in/);
  assert.equal(h.node('renew').disabled, false);
  assert.equal(h.calls.filter(c => c.url === '/api/owner/logout').length, 1);
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 2);
  assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('app resume restarts current fleet and search immediately', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const helper = app.slice(app.indexOf('function resumeOwnerReads()'), app.indexOf('// Bootstrap LAST'));
  const calls = [], search = { value: ' active query ', dispatchEvent(event) { calls.push(['search', event.type]); } };
  const state = { sessionPaused: true, currentView: 'fleet',
    refreshBoards() { calls.push(['boards']); }, pollFleet() { calls.push(['fleet']); },
    $: () => search, Event: class { constructor(type) { this.type = type; } } };
  runInNewContext(`${helper}\nresumeOwnerReads()`, state);
  assert.equal(state.sessionPaused, false);
  assert.deepEqual(calls, [['boards'], ['fleet'], ['search', 'input']]);
});

test('aborted inventory from an earlier session cannot overwrite the resumed panel', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const poll = app.slice(app.indexOf('async function pollFleet()'), app.indexOf('function orgNode('));
  const inventory = deferred(), rendered = [];
  const state = { sessionPaused: false, sessionEpoch: 1, inventoryGeneration: 0, equipmentGeneration: 0, inventoryAliases: new Set(), currentFleet: 'synthetic', currentView: 'fleet',
    equipmentAlias: null, equipmentSelection: null, rememberEquipmentFocus() {},
    fleetQuery: () => '?fleet=synthetic', $: () => ({}), renderState() {}, renderInventory(value) { rendered.push(value); },
    jget: () => inventory.promise };
  const pending = runInNewContext(`${poll}\npollFleet()`, state);
  state.sessionEpoch++; inventory.resolve(null); await pending;
  assert.deepEqual(rendered, []);
});

test('successful board reads and open/error cycles cannot rearm failed-stream recovery', async () => {
  const board = { state: 'ok', data: ['current board'] };
  const h = harness([ready, ready, { status: 200, data: board }]); await h.controls.ready;
  h.api.createEventSource('/api/stream');
  h.natives[0].onerror({}); await flush();
  assert.deepEqual(await h.api.jget('/api/tasks'), board);
  h.natives[1].onopen({}); // A proxy can open then immediately fail repeatedly.
  h.natives[1].onerror({}); await flush();
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 2);
  assert.equal(h.natives.length, 2); assert.equal(h.natives[1].closed, true);
  assert.match(h.node('status').textContent, /unknown/);
  h.controls.dispose();
});

test('admitted stream messages rearm stream recovery without replaying session mutations', async () => {
  const h = harness([ready, ready, ready]); await h.controls.ready;
  h.api.createEventSource('/api/stream');
  h.natives[0].onerror({}); await flush();
  h.natives[1].onmessage({ data: '{"rows":[]}' });
  h.natives[1].onerror({}); await flush();
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 3);
  assert.equal(h.calls.every(c => c.options.method === 'GET'), true);
  h.controls.dispose();
});

test('both resume callbacks preserve equipment through loading DOM replacement and out-of-order inventory', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const helper = app.slice(app.indexOf('function resumeOwnerReads()'), app.indexOf('// Bootstrap LAST'));
  const poll = app.slice(app.indexOf('async function pollFleet()'), app.indexOf('function orgNode('));
  for (const order of [[0, 1], [1, 0]]) {
    const requests = [deferred(), deferred()], rendered = [], opened = [], room = {};
    let detail = { hidden: false, alias: 'synthetic-worker' }, index = 0;
    const state = { sessionPaused: true, sessionEpoch: 4, inventoryGeneration: 0, equipmentGeneration: 0, inventoryAliases: new Set(),
      currentView: 'fleet', currentFleet: 'synthetic', equipmentAlias: 'synthetic-worker', equipmentSelection: null, rememberEquipmentFocus() {},
      fleetQuery: () => '?fleet=synthetic', refreshBoards() {},
      $: id => id === 'fleet-room' ? room : id === 'equip-detail' ? detail : { value: '' },
      renderState() { detail = null; }, // Actual loading behavior removes the detail node.
      renderInventory(inv) { rendered.push(inv.data.version); detail = { hidden: true }; },
      openEquipment(alias) { opened.push(alias); detail.hidden = false; detail.alias = alias; },
      jget(url) { return url.startsWith('/api/inventory') ? requests[index++].promise : Promise.resolve(null); },
    };
    runInNewContext(`${poll}\n${helper}\nresumeOwnerReads(); resumeOwnerReads();`, state);
    assert.equal(detail, null); assert.equal(index, 2);
    for (const i of order) { requests[i].resolve({ state: 'ok', data: { version: i } }); await flush(); }
    assert.deepEqual(rendered, [1]); assert.deepEqual(opened, ['synthetic-worker']);
    assert.equal(detail.hidden, false); assert.equal(detail.alias, 'synthetic-worker');
  }
});

test('grid half-completed reads never paint across pause, and paused polls issue no requests', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const poll = app.slice(app.indexOf('async function pollGrid()'), app.indexOf('const PRESENCE_ORDER'));
  for (const first of ['grid', 'presence']) {
    const grid = deferred(), presence = deferred(), rendered = [], calls = [];
    const state = { sessionPaused: false, sessionEpoch: 1, currentView: 'grid', currentFleet: 'synthetic',
      fleetQuery: () => '?fleet=synthetic', renderGrid: value => rendered.push(value),
      renderPresenceStrip: value => rendered.push(value),
      jget(url) { calls.push(url); return url.startsWith('/api/grid') ? grid.promise : presence.promise; },
    };
    const pending = runInNewContext(`${poll}\npollGrid()`, state);
    (first === 'grid' ? grid : presence).resolve({ state: 'ok', data: first === 'grid'
      ? { panes: [{ fleet: 'synthetic', bot: 'worker', lines: ['old private frame'] }] }
      : { bots: [], counts: { working: 1 } } });
    await flush(); state.sessionPaused = true; state.sessionEpoch++;
    (first === 'grid' ? presence : grid).resolve(null); await pending;
    assert.deepEqual(rendered, []);
    await runInNewContext(`${poll}\npollGrid()`, state);
    assert.equal(calls.length, 2);
  }
});

test('quiet stream recovery respects the 30 second monotonic boundary and never rearms from board reads', async () => {
  for (const duration of [29999, 30000]) {
    let clock = 0;
    const h = harness([ready, ready, { status: 200, data: ['healthy board'] }, ready], { monotonicNow: () => clock });
    await h.controls.ready; h.api.createEventSource('/api/stream');
    h.natives[0].onerror({}); await flush();
    h.natives[1].onopen({}); // Timestamp0 must count as an actual open.
    assert.deepEqual(await h.api.jget('/api/tasks'), ['healthy board']);
    clock = duration; h.natives[1].onerror({}); await flush();
    assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, duration < 30000 ? 2 : 3);
    if (duration >= 30000) {
      assert.equal(h.node('renew').disabled, false);
      h.natives[2].onopen({}); h.natives[2].onerror({}); await flush();
      assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 3);
    }
    assert.match(h.node('status').textContent, /unknown/);
    assert.equal(h.calls.every(c => c.options.method === 'GET'), true);
    h.controls.dispose();
  }
});

test('a stream that never opened cannot earn recovery by elapsed wall time', async () => {
  let clock = 0;
  const h = harness([ready, ready], { monotonicNow: () => clock }); await h.controls.ready;
  h.api.createEventSource('/api/stream'); h.natives[0].onerror({}); await flush();
  clock = 60000; h.natives[1].onerror({}); await flush();
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 2);
  assert.match(h.node('status').textContent, /unknown/);
  h.controls.dispose();
});

test('a quiet stable stream checks expired current cookie and redirects without any mutation replay', async () => {
  let clock = 0;
  const h = harness([ready, ready, { status: 200, data: { state: 'sign_in_required' } }], { monotonicNow: () => clock });
  await h.controls.ready; h.api.createEventSource('/api/stream');
  h.natives[0].onerror({}); await flush(); h.natives[1].onopen({});
  clock = 30000; h.natives[1].onerror({}); await flush();
  assert.equal(h.calls.filter(c => c.url === '/api/owner/status').length, 3);
  assert.deepEqual(h.replacements, ['/owner']);
  assert.equal(h.natives[1].closed, true);
  assert.equal(h.calls.every(c => c.options.method === 'GET'), true);
  h.controls.dispose();
});


for (const change of ['other room', 'new grant scope', 'current scope'])
test(`late action403 invalidates only its originating composer: ${change}`, async () => {
  const text = (await readFile(new URL('../claudlobby/plane/ui/work-loop.js', import.meta.url), 'utf8'))
    .replaceAll('from "/action-state.js"', `from "${new URL('../claudlobby/plane/ui/action-state.js', import.meta.url)}"`)
    .replaceAll('from "/panel-state.js"', `from "${new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url)}"`);
  const { mountWorkLoop } = await load(text);
  const elements = new Map(), values = new Map();
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { value: '', hidden: false, disabled: false,
      innerHTML: '', querySelectorAll: () => [], addEventListener(name, callback) { this[name] = callback; } });
    return elements.get(id);
  } };
  const oldDocument = globalThis.document, oldStorage = globalThis.sessionStorage;
  globalThis.document = document;
  globalThis.sessionStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  const late = deferred(); let loop;
  const fresh = change === 'other room' ? { ...context, room: 'second-team', scope: { ...context.scope, fleet: 'second-team' } }
    : change === 'new grant scope' ? { ...context, scope: { ...context.scope, viewer: 'new-generation-viewer' } } : context;
  const h = harness([ready, ok(context), ok(null), ok(null), () => late.promise, ok(fresh), ok(null), ok(null), ready, ok(['board'])], {
    onActionPause(api, scope, kind, recipient) { loop.invalidate(undefined, scope, kind, recipient); }
  });
  try {
    await h.controls.ready;
    loop = mountWorkLoop({ api: h.api, renderThread() {}, refresh() {} });
    loop.setRoom(context.room); await flush();
    const rejected = assert.rejects(h.api.sendAction(action), /outcome unknown/);
    loop.setRoom(fresh.room); await flush();
    const draft = document.getElementById('work-body'); draft.value = 'Newly selected draft'; draft.input();
    late.resolve(denied); await rejected;
    assert.equal(document.getElementById('work-form').hidden, change === 'current scope');
    assert.equal(draft.value, change === 'current scope' ? '' : 'Newly selected draft');
    assert.equal(document.getElementById('work-send').disabled, change === 'current scope');
    assert.deepEqual(await h.api.jget('/api/tasks'), ['board']);
    assert.equal(h.node('renew').disabled, false); assert.deepEqual(h.replacements, []);
    assert.equal(h.calls.filter(c => c.url.endsWith('/send')).length, 1);
  } finally { h.controls.dispose(); globalThis.document = oldDocument; globalThis.sessionStorage = oldStorage; }
});

test('late context403 leaves context epoch ownership with the controller', async () => {
  let invalidated = 0;
  const late = deferred(), fresh = { ...context, room: 'second-team', scope: { ...context.scope, fleet: 'second-team' } };
  const h = harness([ready, () => late.promise, ok(fresh), ready], { onActionPause() { invalidated++; } });
  await h.controls.ready;
  const old = h.api.interactionContext(context.room);
  assert.deepEqual(await h.api.interactionContext(fresh.room), fresh);
  late.resolve(denied); assert.equal(await old, null);
  assert.equal(invalidated, 0); assert.equal(h.node('renew').disabled, false);
  assert.deepEqual(h.replacements, []); h.controls.dispose();
});


const nudgeContext = {version:2,room:'synthetic',simulation:false,scope:{...context.scope,viewer:'nudge-generation'},
  recipients:[{id:'actor_'+ 'a'.repeat(32),label:'Team lead',lead:true}],actions:['nudge'],release_id:'r-'+ 'b'.repeat(64)};
const nudgeMetadata = {version:2,request_id:'11111111-1111-4111-8111-111111111111',kind:'nudge',scope:nudgeContext.scope,
  target:{recipient:nudgeContext.recipients[0].id,task_id:'wi_'+ 'c'.repeat(32),assignment_id:null,release_id:nudgeContext.release_id},
  submitted_at:'2026-10-10T12:00:00.000Z',semantic_sha256:'d'.repeat(64)};
test('owner nudge capability, prepare, send and receipt use exact independent v2 bodies and bounded protected requests',async()=>{
  const {semantic_sha256,...pre}=nudgeMetadata, response={...nudgeMetadata,status:'recorded'};
  const h=harness([ready,ok(context),ok(nudgeContext),ok(nudgeMetadata),ok(response),ok(response)]);await h.controls.ready;
  assert.deepEqual(await h.api.interactionContext('synthetic'),context);assert.deepEqual(await h.api.nudgeContext('synthetic'),nudgeContext);
  assert.deepEqual(await h.api.prepareAction({...pre,body:'Exact reason',unexpected:'removed'}),nudgeMetadata);
  await h.api.sendAction({...nudgeMetadata,body:'Exact reason'});await h.api.actionReceipt({...nudgeMetadata,body:'must not repeat'});
  const bodies=h.calls.slice(1).map(call=>JSON.parse(call.options.body));
  assert.deepEqual(bodies,[{room:'synthetic'},{room:'synthetic',kind:'nudge'},{...pre,body:'Exact reason'},
    {...nudgeMetadata,body:'Exact reason'},nudgeMetadata]);
  assert.deepEqual(h.calls.slice(1).map(call=>call.url),['/api/owner/actions/context','/api/owner/actions/context','/api/owner/actions/prepare','/api/owner/actions/send','/api/owner/actions/receipt']);
  for(const call of h.calls.slice(1)) {assert.equal(call.options.credentials,'same-origin');assert.equal(call.options.cache,'no-store');assert.equal(call.options.redirect,'error');assert.equal(call.options.headers['X-Claudlobby-Owner'],'1');}
  assert.deepEqual(h.timeouts,[8000,8000,8000,8000,45000,8000]);
});
test('nudge context refusal does one direct status check while message/read access remains ready',async()=>{
  const h=harness([ready,denied,ready,ok(context),ok({state:'ok',data:{tasks:[]}})]);await h.controls.ready;
  assert.equal(await h.api.nudgeContext('synthetic'),null);assert.equal(h.resumed,1);assert.equal(h.replacements.length,0);
  assert.deepEqual(await h.api.interactionContext('synthetic'),context);assert.equal((await h.api.jget('/api/tasks')).state,'ok');
  assert.equal(h.calls.filter(call=>call.url==='/api/owner/status').length,2);
});
test('nudge mutation refusal identifies only original nudge scope and never replays prepare or send',async()=>{
  const pauses=[],h=harness([ready,denied,ready,ok({state:'ok'})],{onActionPause(_api,scope,kind,recipient){pauses.push({scope,kind,recipient});}});await h.controls.ready;
  await assert.rejects(h.api.sendAction({...nudgeMetadata,body:'Reason'}),/unknown/);
  assert.deepEqual(pauses,[{scope:nudgeMetadata.scope,kind:'nudge',recipient:nudgeMetadata.target.recipient}]);assert.equal(h.resumed,1);assert.equal(h.replacements.length,0);
  assert.equal((await h.api.jget('/api/tasks')).state,'ok');assert.equal(h.calls.filter(call=>call.url.endsWith('/send')).length,1);
  assert.ok(!h.calls.some(call=>call.url.endsWith('/prepare')));
});
for(const lifecycle of ['renew','logout'])
test(`session ${lifecycle} fences late nudge prepare and send results without mutation replay`,async()=>{
  const {semantic_sha256,...pre}=nudgeMetadata;
  for(const path of ['prepare','send']) {
    const late=deferred(),h=harness([ready,()=>late.promise,ok(lifecycle==='renew'?ready.data:{state:'signed_out'})]);await h.controls.ready;
    const operation=path==='prepare'?h.api.prepareAction({...pre,body:'Reason'}):h.api.sendAction({...nudgeMetadata,body:'Reason'});
    const rejected=assert.rejects(operation,/unknown/);await h.click(lifecycle);
    late.resolve(ok(path==='prepare'?nudgeMetadata:{...nudgeMetadata,status:'delivered'}));await rejected;
    assert.equal(h.calls.filter(call=>call.url.endsWith('/'+path)).length,1);
    assert.equal(h.calls.filter(call=>call.url.endsWith('/prepare')).length,path==='prepare'?1:0);
  }
});
test('owner rejects missing assignment and legacy synthetic nudges before any network operation',async()=>{
  const h=harness([ready]);await h.controls.ready;
  for(const changed of [{version:1},{version:undefined},{target:{...nudgeMetadata.target,assignment_id:''}},
    {target:{recipient:nudgeMetadata.target.recipient,task_id:nudgeMetadata.target.task_id,release_id:nudgeMetadata.target.release_id}}])
    await assert.rejects(h.api.sendAction({...nudgeMetadata,...changed,body:'Reason'}),/Unsupported/);
  assert.equal(h.calls.length,1);
});


test('app forwards capability kind and original scope without globally pausing task reads',async()=>{
  const app=await readFile(new URL('../claudlobby/plane/ui/app.js',import.meta.url),'utf8');
  const body=app.match(/onActionPause\(scope, kind, recipient\) \{([^}]+)\}/)[1],calls=[];
  runInNewContext(`(function(scope,kind,recipient){${body}})(scope,kind,recipient)`,{scope:nudgeMetadata.scope,kind:'nudge',recipient:nudgeMetadata.target.recipient,workLoop:{invalidate(...args){calls.push(args);}}});
  assert.deepEqual(calls,[[undefined,nudgeMetadata.scope,'nudge',nudgeMetadata.target.recipient]]);
});


const feedbackContext={...nudgeContext,scope:{...nudgeContext.scope,viewer:'feedback-generation'},actions:['feedback']};
const feedbackMetadata={...nudgeMetadata,kind:'feedback',scope:feedbackContext.scope};
test('owner feedback wire is exact v2, independent of messages/nudges; prepared body and receipt remain distinct',async()=>{
 const {semantic_sha256,...pre}=feedbackMetadata,response={...feedbackMetadata,status:'recorded'};
 const h=harness([ready,ok(feedbackContext),ok(feedbackMetadata),ok(response),ok(response)]);await h.controls.ready;
 assert.deepEqual(await h.api.feedbackContext('synthetic'),feedbackContext);await h.api.prepareAction({...pre,body:'Exact comment'});await h.api.sendAction({...feedbackMetadata,body:'Exact comment'});await h.api.actionReceipt({...feedbackMetadata,body:'never repeat'});
 assert.deepEqual(h.calls.slice(1).map(c=>JSON.parse(c.options.body)),[{room:'synthetic',kind:'feedback'},{...pre,body:'Exact comment'},{...feedbackMetadata,body:'Exact comment'},feedbackMetadata]);
 assert.deepEqual(h.timeouts,[8000,8000,8000,45000,8000]);h.controls.dispose();
});
test('feedback refusal reports only its original scope/kind/lead and keeps read session usable without replay',async()=>{
 const pauses=[],h=harness([ready,denied,ready,ok({state:'ok'})],{onActionPause(_api,scope,kind,recipient){pauses.push({scope,kind,recipient});}});await h.controls.ready;
 await assert.rejects(h.api.sendAction({...feedbackMetadata,body:'Comment'}),/unknown/);assert.deepEqual(pauses,[{scope:feedbackMetadata.scope,kind:'feedback',recipient:feedbackMetadata.target.recipient}]);
 assert.equal((await h.api.jget('/api/tasks')).state,'ok');assert.equal(h.calls.filter(c=>c.url.endsWith('/send')).length,1);assert.ok(!h.calls.some(c=>c.url.endsWith('/prepare')));assert.deepEqual(h.replacements,[]);h.controls.dispose();
});
for(const lifecycle of ['renew','logout'])test(`session ${lifecycle} fences late feedback prepare/send without replay`,async()=>{
 const {semantic_sha256,...pre}=feedbackMetadata;
 for(const path of ['prepare','send']){const held=deferred(),h=harness([ready,()=>held.promise,ok(lifecycle==='renew'?ready.data:{state:'signed_out'})]);await h.controls.ready;
 const action=path==='prepare'?h.api.prepareAction({...pre,body:'Comment'}):h.api.sendAction({...feedbackMetadata,body:'Comment'});const rejection=assert.rejects(action,/unknown/);await h.click(lifecycle);held.resolve(ok(path==='prepare'?feedbackMetadata:{...feedbackMetadata,status:'delivered'}));await rejection;assert.equal(h.calls.filter(c=>c.url.endsWith('/'+path)).length,1);h.controls.dispose();}
});

const inspectionApp = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
const inspectionHelpers = inspectionApp.slice(inspectionApp.indexOf('function closeEquipment()'));
const rosterHelpers = inspectionApp.slice(inspectionApp.indexOf('function railRow('), inspectionApp.indexOf('// The header in the operator'));
const botIdentity = { uid: 'actor_' + '4'.repeat(32), alias: 'bot:team/with/slash/worker',
  fleet: 'team/with/slash', fleet_uid: 'fleet_' + '5'.repeat(32), provisional: false };
const equipmentEnvelope = alias => ({ state: 'ok', data: { alias, short: 'Worker', equipment: {}, posture: {},
  changes: [], versions: 1, composed_hashes: {}, org: {} } });
function inspectionHarness() {
  const reads = [], selections = [], buttons = new Map(), cards = [];
  const document = { activeElement: null, body: { isConnected: true } };
  let box;
  function detail() {
    return { hidden: true, content: '', scrolls: 0,
      set innerHTML(value) {
        for (const button of buttons.values()) {
          if (document.activeElement === button) document.activeElement = document.body;
          button.isConnected = false;
        }
        buttons.clear(); this.content = value;
        for (const name of ['ed-close', 'ed-message', 'ed-message-note']) if (value.includes(name)) {
          buttons.set('.' + name, { isConnected: true, disabled: true, textContent: '', focusCalls: 0,
            focus() { this.focusCalls++; document.activeElement = this; },
            matches(selector) { return selector === '.' + name; },
            addEventListener(event, listener) { this[event] = listener; } });
        }
      }, get innerHTML() { return this.content; },
      querySelector(selector) { return buttons.get(selector) || null; },
      contains(node) { return !!node && [...buttons.values()].includes(node); },
      scrollIntoView() { this.scrolls++; } };
  }
  box = detail();
  const state = { sessionPaused: false, sessionEpoch: 1, currentFleet: botIdentity.fleet, currentView: 'fleet',
    inventoryGeneration: 1, rosterGeneration: 1, equipmentGeneration: 0, equipmentAlias: null, equipmentSelection: botIdentity, equipmentFocus: null,
    fleets: [{ alias: botIdentity.fleet, uid: botIdentity.fleet_uid }],
    inventoryAliases: new Set([botIdentity.alias]), rosterIdentities: new Map([[botIdentity.uid, botIdentity]]),
    EQUIP_ORDER: ['skills'], esc: value => String(value), ago: () => '',
    document, $: id => id === 'equip-detail' ? box : id === 'fleet-room' ? { querySelectorAll: () => cards } : { scrollIntoView() {} },
    stateBlock: (state, provenance, text) => text || state,
    renderState(node) { node.innerHTML = 'loading'; },
    jget(url) { const pending = deferred(); reads.push({ url, ...pending }); return pending.promise; },
    workLoop: { canSelectMessageRecipient(identity) { return !!identity && !identity.provisional; },
      selectMessageRecipient(identity) { if (!this.canSelectMessageRecipient(identity)) return false; selections.push(identity); return true; } },
  };
  runInNewContext(inspectionHelpers, state);
  return { state, reads, selections, buttons, cards, document, get box() { return box; }, replaceBox() { box = detail(); } };
}

test('bot roster names are native inspect buttons bound to full server fleet and UID, including twins', () => {
  const opens = [], nodes = [], document = { activeElement: null };
  const rail = { html: '', set innerHTML(value) { this.html = value; for (const node of nodes) node.isConnected = false; nodes.length = 0;
    for (const match of value.matchAll(/data-bot-inspect="([^"]+)"/g)) nodes.push({ dataset: { botInspect: match[1] }, isConnected: true, focus() { document.activeElement = this; },
      addEventListener(event, listener) { this[event] = listener; } }); },
    get innerHTML() { return this.html; }, querySelectorAll() { return nodes; } };
  const twin = { ...botIdentity, uid: 'actor_' + '6'.repeat(32), alias: 'bot:other/worker', fleet: 'other', fleet_uid: 'fleet_' + '7'.repeat(32) };
  const state = { document, rosterIdentities: new Map(), rosterGeneration: 0, sessionEpoch: 2, sessionPaused: false,
    currentView: 'channel', equipmentAlias: null, equipmentFocus: null, fleets: [botIdentity, twin].map(b => ({ alias: b.fleet, uid: b.fleet_uid })),
    $: () => rail, esc: value => String(value), ago: () => '', renderState: () => false,
    refreshEquipmentAction() {}, inspectBot(identity) { opens.push(identity); },
    groupBy(rows, key) { return [...Map.groupBy(rows, key).values()]; } };
  runInNewContext(rosterHelpers, state);
  const rows = [botIdentity, twin].map(b => ({ ...b, kind: 'actor', short: 'Worker' }));
  state.renderFleet({ state: 'ok', data: { identities: rows } });
  assert.match(rail.innerHTML, /<button class="bot-inspect" type="button"/);
  assert.match(rail.innerHTML, /aria-label="Inspect Worker in team\/with\/slash"/);
  nodes[1].click(); assert.equal(opens[0].uid, twin.uid); assert.equal(opens[0].alias, twin.alias); assert.equal(opens[0].fleet_uid, twin.fleet_uid);
  nodes[1].focus(); const prior = document.activeElement, version = state.rosterGeneration;
  state.renderFleet({ state: 'ok', data: { identities: rows } });
  assert.notEqual(document.activeElement, prior); assert.equal(document.activeElement.dataset.botInspect, twin.uid);
  assert.equal(state.rosterGeneration, version);
  const stale = nodes[0]; state.rosterGeneration++; stale.click(); assert.equal(opens.length, 1);
});

for (const change of ['session', 'pause', 'fleet', 'view', 'inventory', 'roster', 'close', 'DOM', 'selection'])
test(`equipment response is fenced after ${change} changes`, async () => {
  const h = inspectionHarness(), loading = h.state.openEquipment(botIdentity.alias);
  if (change === 'session') h.state.sessionEpoch++;
  if (change === 'pause') h.state.sessionPaused = true;
  if (change === 'fleet') h.state.currentFleet = 'other';
  if (change === 'view') h.state.currentView = 'channel';
  if (change === 'inventory') h.state.inventoryGeneration++;
  if (change === 'roster') h.state.rosterGeneration++;
  if (change === 'close') h.state.closeEquipment();
  if (change === 'DOM') h.replaceBox();
  if (change === 'selection') h.state.equipmentSelection = { ...botIdentity };
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await loading;
  assert.equal(h.buttons.has('.ed-message'), false); assert.deepEqual(h.selections, []);
});

test('equipment same-alias close/reopen and out-of-order requests cannot restore an old panel', async () => {
  const h = inspectionHarness(), old = h.state.openEquipment(botIdentity.alias);
  h.state.closeEquipment(); h.state.equipmentSelection = botIdentity;
  const fresh = h.state.openEquipment(botIdentity.alias);
  h.reads[1].resolve({ ...equipmentEnvelope(botIdentity.alias), data: { ...equipmentEnvelope(botIdentity.alias).data, model: 'New model' } }); await fresh;
  h.reads[0].resolve({ ...equipmentEnvelope(botIdentity.alias), data: { ...equipmentEnvelope(botIdentity.alias).data, model: 'Old model' } }); await old;
  assert.match(h.box.innerHTML, /New model/); assert.doesNotMatch(h.box.innerHTML, /Old model/);
});

test('equipment must match the exact requested alias even when a twin has the same display name', async () => {
  const h = inspectionHarness(), pending = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope('bot:other/worker')); await pending;
  assert.match(h.box.innerHTML, /did not match the selected bot/); assert.equal(h.buttons.has('.ed-message'), false);
  assert.equal(h.reads[0].url, '/api/equipment?alias=' + encodeURIComponent(botIdentity.alias));
});

test('equipment action checks current exact identity and authority, then only selects the recipient', async () => {
  const h = inspectionHarness(), pending = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  const message = h.buttons.get('.ed-message'); assert.equal(message.disabled, false);
  h.state.inventoryAliases.clear(); h.state.refreshEquipmentAction(); assert.equal(message.disabled, true);
  message.click(); assert.deepEqual(h.selections, []);
  h.state.inventoryAliases.add(botIdentity.alias);
  h.state.rosterIdentities.set(botIdentity.uid, { ...botIdentity, alias: 'bot:other/worker' });
  h.state.refreshEquipmentAction(); assert.equal(message.disabled, true); message.click(); assert.deepEqual(h.selections, []);
  h.state.rosterIdentities.set(botIdentity.uid, botIdentity);
  h.state.workLoop.canSelectMessageRecipient = () => false;
  h.state.refreshEquipmentAction(); assert.equal(message.disabled, true); message.click(); assert.deepEqual(h.selections, []);
  h.state.workLoop.canSelectMessageRecipient = identity => !!identity;
  h.state.refreshEquipmentAction(); message.click();
  assert.equal(h.selections.length, 1); assert.equal(h.selections[0].uid, botIdentity.uid); assert.equal(h.selections[0].alias, botIdentity.alias);
  assert.equal(h.selections[0].fleet, botIdentity.fleet); assert.equal(h.selections[0].fleet_uid, botIdentity.fleet_uid);
  assert.equal(h.box.hidden, true); assert.equal(h.reads.length, 1);
  message.click(); assert.equal(h.selections.length, 1);
});


test('initial fleet discovery cannot change a newer room or resumed session', async () => {
  const refresh = inspectionApp.slice(inspectionApp.indexOf('async function refreshBoards()'), inspectionApp.indexOf('function scheduleRefresh()'));
  for (const reason of ['room', 'session']) {
    const reply = deferred(), adopted = [];
    const state = { sessionPaused: false, generation: 0, fleetsSeen: false,
      jget: () => reply.promise, adoptFleets(value) { adopted.push(value); } };
    const pending = runInNewContext(`${refresh}\nrefreshBoards()`, state);
    state.generation++; if (reason === 'session') state.sessionPaused = true;
    reply.resolve({ state: 'ok', data: { default: 'old room' } }); await pending;
    assert.deepEqual(adopted, []);
  }
});

test('unchanged roster keeps equipment open without a refetch; changed identity fences old equipment', () => {
  const opened = [];
  const state = { rosterIdentities: new Map([[botIdentity.uid, botIdentity]]), rosterGeneration: 3,
    equipmentAlias: botIdentity.alias, currentView: 'fleet', refreshEquipmentAction() {},
    openEquipment(alias) { opened.push(alias); } };
  runInNewContext(rosterHelpers, state);
  state.adoptRoster(new Map([[botIdentity.uid, { ...botIdentity }]]));
  assert.equal(state.rosterGeneration, 3); assert.deepEqual(opened, []);
  state.adoptRoster(new Map([[botIdentity.uid, { ...botIdentity, provisional: true }]]));
  assert.equal(state.rosterGeneration, 4); assert.deepEqual(opened, [botIdentity.alias]);
});

test('closing equipment restores focus to its exact bot card, never a same-name twin', async () => {
  const h = inspectionHarness();
  const twin = { dataset: { alias: 'bot:other/worker' }, focus() { h.document.activeElement = this; } };
  const current = { dataset: { alias: botIdentity.alias }, focus() { h.document.activeElement = this; } };
  h.cards.push(twin, current);
  const pending = h.state.openEquipment(botIdentity.alias); h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  const close = h.buttons.get('.ed-close'); close.click();
  assert.equal(h.box.hidden, true); assert.equal(h.document.activeElement, current);
  h.document.activeElement = twin; close.click(); assert.equal(h.document.activeElement, twin);
});

test('visible close works during a paused session without moving focus into resumed inventory', async () => {
  const h = inspectionHarness(), current = { dataset: { alias: botIdentity.alias }, focus() { h.document.activeElement = this; } };
  h.cards.push(current);
  const pending = h.state.openEquipment(botIdentity.alias); h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  h.state.sessionPaused = true; h.state.sessionEpoch++; h.buttons.get('.ed-close').click();
  assert.equal(h.document.activeElement, null); assert.equal(h.box.hidden, true);
});

test('inspect switching fleets from the Team view uses its one existing inventory poll', () => {
  const h = inspectionHarness(); let polls = 0;
  h.state.currentFleet = 'other'; h.state.currentView = 'fleet';
  h.state.syncWorkRoom = () => {}; h.state.savePick = () => {}; h.state.renderFleetTabs = () => {};
  h.state.refreshBoards = () => {}; h.state.pollFleet = () => { polls++; };
  h.state.setView = view => { h.state.currentView = view; if (view === 'fleet') h.state.pollFleet(); };
  const get = h.state.$; h.state.$ = id => id === 'search-results' ? { hidden: true } : get(id);
  const pick = inspectionApp.slice(inspectionApp.indexOf('function pickFleet('), inspectionApp.indexOf('const STATUS_DOT'));
  const room = inspectionApp.slice(inspectionApp.indexOf('function activeRoom()'), inspectionApp.indexOf('const ownerSession ='));
  runInNewContext(`${pick}\n${room}`, h.state);
  h.state.inspectBot(botIdentity);
  assert.equal(polls, 1); assert.equal(h.state.currentFleet, botIdentity.fleet);
  assert.equal(h.state.equipmentSelection, botIdentity); assert.equal(h.state.equipmentAlias, botIdentity.alias);
});

test('equipment capability callback safely disables an existing panel during composer initialization', async () => {
  const h = inspectionHarness(), pending = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  h.state.workLoop = null;
  h.state.refreshEquipmentAction();
  assert.equal(h.buttons.get('.ed-message').disabled, true);
});

test('human actors remain readable without a bot-inspection button, even with slash-bearing human aliases', () => {
  const state = { esc: value => String(value), ago: () => '' };
  runInNewContext(rosterHelpers, state);
  const html = state.railRow({ kind: 'actor', uid: 'actor_' + '8'.repeat(32), alias: 'human:team/with/slash', short: 'Human', fleet: null });
  assert.match(html, /Human/); assert.doesNotMatch(html, /data-bot-inspect|<button/);
});

test('explicit inspection focuses the completed panel once; background equipment refresh never steals focus', async () => {
  const h = inspectionHarness();
  h.state.equipmentFocus = { alias: botIdentity.alias, selection: botIdentity, epoch: h.state.sessionEpoch };
  const explicit = h.state.openEquipment(botIdentity.alias);
  assert.equal(h.document.activeElement, null);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await explicit;
  assert.equal(h.document.activeElement, h.buttons.get('.ed-close'));
  assert.equal(h.buttons.get('.ed-close').focusCalls, 1); assert.equal(h.state.equipmentFocus, null);
  const otherControl = {}; h.document.activeElement = otherControl;
  const refresh = h.state.openEquipment(botIdentity.alias); h.reads[1].resolve(equipmentEnvelope(botIdentity.alias)); await refresh;
  assert.equal(h.document.activeElement, otherControl); assert.equal(h.buttons.get('.ed-close').focusCalls, 0);
});

for (const moveToComposer of [false, true]) test(`cross-fleet inspection preserves ${moveToComposer ? 'new composer focus' : 'its focus request'} through inventory and late boards`, async () => {
  const h = inspectionHarness(), pick = inspectionApp.slice(inspectionApp.indexOf('function pickFleet('), inspectionApp.indexOf('const STATUS_DOT'));
  const room = inspectionApp.slice(inspectionApp.indexOf('function activeRoom()'), inspectionApp.indexOf('const ownerSession ='));
  const poll = inspectionApp.slice(inspectionApp.indexOf('async function pollFleet()'), inspectionApp.indexOf('function orgNode('));
  h.state.currentFleet = 'all'; h.state.currentView = 'fleet';
  const origin = { isConnected: true, dataset: { botInspect: botIdentity.uid } };
  h.document.activeElement = origin;
  h.state.syncWorkRoom = () => { h.document.activeElement = null; };
  h.state.savePick = () => {}; h.state.renderFleetTabs = () => {}; h.state.refreshBoards = () => {};
  h.state.fleetQuery = () => '?fleet=' + encodeURIComponent(h.state.currentFleet);
  h.state.renderInventory = () => {};
  h.state.setView = () => { throw Error('Team is already visible; a second poll is unnecessary'); };
  const get = h.state.$; h.state.$ = id => id === 'search-results' ? { hidden: true } : get(id);
  runInNewContext(`${pick}\n${room}\n${poll}`, h.state);
  runInNewContext(rosterHelpers, h.state);
  const twin = { ...botIdentity, uid: 'actor_' + '6'.repeat(32), fleet: 'other', alias: 'bot:other/worker' };
  h.state.rosterIdentities.set(twin.uid, twin);
  const originalPoll = h.state.pollFleet; let pendingPoll;
  h.state.pollFleet = () => { pendingPoll = originalPoll(); return pendingPoll; };
  h.state.inspectBot(botIdentity);
  assert.equal(h.document.activeElement, null); assert.equal(h.reads.length, 3);
  assert.equal(h.state.equipmentFocus.origin, origin);
  const composer = { isConnected: true }; if (moveToComposer) h.document.activeElement = composer;
  h.reads[0].resolve({ state: 'ok', data: { bots: [{ alias: botIdentity.alias }] } });
  h.reads[1].resolve(null); h.reads[2].resolve(null); await pendingPoll;
  assert.equal(h.reads.length, 4); assert.match(h.reads[3].url, /^\/api\/equipment/);
  h.reads[3].resolve(equipmentEnvelope(botIdentity.alias)); await flush();
  assert.equal(h.document.activeElement, moveToComposer ? composer : h.buttons.get('.ed-close'));
  assert.equal(h.state.equipmentFocus, null);
  // The fleet-filtered board can arrive after inventory and equipment.
  h.state.adoptRoster(new Map([[botIdentity.uid, botIdentity]]));
  assert.equal(h.document.activeElement, moveToComposer ? composer : h.document.body);
  assert.equal(h.reads.length, 5);
  h.reads[4].resolve(equipmentEnvelope(botIdentity.alias)); await flush();
  assert.equal(h.document.activeElement, moveToComposer ? composer : h.buttons.get('.ed-close'));
});

test('visible close remains usable during an inventory generation change without stale focus hand-back', async () => {
  const h = inspectionHarness(), current = { dataset: { alias: botIdentity.alias }, focus() { h.document.activeElement = this; } };
  h.cards.push(current);
  const pending = h.state.openEquipment(botIdentity.alias); h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  h.state.inventoryGeneration++; h.state.equipmentGeneration++;
  h.buttons.get('.ed-close').click();
  assert.equal(h.box.hidden, true); assert.equal(h.state.equipmentAlias, null); assert.equal(h.document.activeElement, null);
});

test('detached close from a prior equipment render cannot close its replacement', async () => {
  const h = inspectionHarness(), first = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await first;
  const oldClose = h.buttons.get('.ed-close');
  const next = h.state.openEquipment(botIdentity.alias); h.reads[1].resolve(equipmentEnvelope(botIdentity.alias)); await next;
  oldClose.click();
  assert.equal(h.box.hidden, false); assert.equal(h.state.equipmentAlias, botIdentity.alias);
  assert.equal(oldClose.isConnected, false);
});

test('changed or unavailable roster identity explains disabled messaging until explicit reinspection', async () => {
  const h = inspectionHarness(), pending = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  h.state.rosterIdentities.set(botIdentity.uid, { ...botIdentity, provisional: true });
  h.state.refreshEquipmentAction();
  assert.equal(h.buttons.get('.ed-message').disabled, true);
  assert.match(h.buttons.get('.ed-message-note').textContent, /Select it again after the team refreshes/);
  assert.equal(h.state.equipmentSelection, botIdentity); assert.deepEqual(h.selections, []);
  h.state.equipmentSelection = h.state.rosterIdentities.get(botIdentity.uid);
  h.state.refreshEquipmentAction();
  assert.match(h.buttons.get('.ed-message-note').textContent, /unconfirmed.*Inspect it again/);
});

test('explicit inspection of unavailable historical equipment still focuses a visible close control', async () => {
  const h = inspectionHarness();
  h.state.equipmentFocus = { alias: botIdentity.alias, selection: botIdentity, epoch: h.state.sessionEpoch };
  const pending = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve({ state: 'idle' }); await pending;
  assert.equal(h.document.activeElement, h.buttons.get('.ed-close')); assert.equal(h.buttons.has('.ed-message'), false);
  h.buttons.get('.ed-close').click(); assert.equal(h.box.hidden, true);
});

for (const selector of ['.ed-close', '.ed-message']) test(`background roster refresh restores focused ${selector} after real child removal`, async () => {
  const h = inspectionHarness(); runInNewContext(rosterHelpers, h.state);
  const first = h.state.openEquipment(botIdentity.alias); h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await first;
  const origin = h.buttons.get(selector); origin.focus();
  h.state.adoptRoster(new Map([...h.state.rosterIdentities, ['actor_' + '8'.repeat(32), { ...botIdentity, uid: 'actor_' + '8'.repeat(32), alias: 'bot:team/with/slash/other' }]]));
  assert.equal(origin.isConnected, false); assert.equal(h.document.activeElement, h.document.body);
  h.reads[1].resolve(equipmentEnvelope(botIdentity.alias)); await flush();
  assert.equal(h.document.activeElement, h.buttons.get(selector));
});

test('background equipment refresh falls back to close if the former message control becomes disabled', async () => {
  const h = inspectionHarness(), first = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await first;
  h.buttons.get('.ed-message').focus();
  const refresh = h.state.openEquipment(botIdentity.alias); h.state.workLoop.canSelectMessageRecipient = () => false;
  h.reads[1].resolve(equipmentEnvelope(botIdentity.alias)); await refresh;
  assert.equal(h.buttons.get('.ed-message').disabled, true); assert.equal(h.document.activeElement, h.buttons.get('.ed-close'));
});

for (const background of [false, true]) test(`${background ? 'background' : 'explicit'} inspection response never takes focus from a live composer`, async () => {
  const h = inspectionHarness(), origin = { isConnected: true };
  if (background) {
    const first = h.state.openEquipment(botIdentity.alias); h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await first;
    h.buttons.get('.ed-message').focus();
  } else {
    h.document.activeElement = origin;
    h.state.equipmentFocus = { alias: botIdentity.alias, selection: botIdentity, epoch: h.state.sessionEpoch, origin };
  }
  const pending = h.state.openEquipment(botIdentity.alias), composer = { isConnected: true };
  h.document.activeElement = composer;
  h.reads.at(-1).resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  assert.equal(h.document.activeElement, composer); assert.equal(h.state.equipmentFocus, null);
  assert.equal(h.buttons.get('.ed-close').focusCalls, 0);
});

test('roster refresh before the first equipment response retains explicit focus and fences the original response', async () => {
  const h = inspectionHarness(); runInNewContext(rosterHelpers, h.state);
  const origin = { isConnected: false };
  h.state.equipmentFocus = { alias: botIdentity.alias, selection: botIdentity, epoch: h.state.sessionEpoch, origin };
  const first = h.state.openEquipment(botIdentity.alias);
  h.state.adoptRoster(new Map([...h.state.rosterIdentities, ['other', { ...botIdentity, uid: 'other' }]]));
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await first;
  assert.equal(h.document.activeElement, null);
  h.reads[1].resolve(equipmentEnvelope(botIdentity.alias)); await flush();
  assert.equal(h.document.activeElement, h.buttons.get('.ed-close')); assert.equal(h.state.equipmentFocus, null);
});

test('disabled message explanation distinguishes session pause, absent inventory, and unavailable current context', async () => {
  const h = inspectionHarness(), pending = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  h.state.sessionPaused = true; h.state.refreshEquipmentAction();
  assert.match(h.buttons.get('.ed-message-note').textContent, /Messaging is paused/);
  h.state.sessionPaused = false; h.state.inventoryAliases.clear(); h.state.refreshEquipmentAction();
  assert.match(h.buttons.get('.ed-message-note').textContent, /not in the current team/);
  assert.doesNotMatch(h.buttons.get('.ed-message-note').textContent, /Inspect.*again/);
  h.state.inventoryAliases.add(botIdentity.alias); h.state.workLoop.canSelectMessageRecipient = () => false; h.state.refreshEquipmentAction();
  assert.match(h.buttons.get('.ed-message-note').textContent, /Messaging is currently unavailable/);
  assert.doesNotMatch(h.buttons.get('.ed-message-note').textContent, /your current access/);
});

test('inventory reload preserves panel focus through replacement of the entire detail node', async () => {
  const h = inspectionHarness(), first = h.state.openEquipment(botIdentity.alias);
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await first;
  h.buttons.get('.ed-message').focus();
  const room = {}, get = h.state.$;
  h.state.$ = id => id === 'fleet-room' ? room : get(id);
  h.state.renderState = node => { if (node === room) { h.box.innerHTML = ''; h.replaceBox(); } else node.innerHTML = 'loading'; };
  h.state.renderInventory = () => {}; h.state.fleetQuery = () => '?fleet=' + botIdentity.fleet;
  const poll = inspectionApp.slice(inspectionApp.indexOf('async function pollFleet()'), inspectionApp.indexOf('function orgNode('));
  runInNewContext(poll, h.state);
  const loading = h.state.pollFleet();
  assert.equal(h.document.activeElement, h.document.body);
  h.reads[1].resolve({ state: 'ok', data: { bots: [{ alias: botIdentity.alias }] } });
  h.reads[2].resolve(null); h.reads[3].resolve(null); await loading;
  h.reads[4].resolve(equipmentEnvelope(botIdentity.alias)); await flush();
  assert.equal(h.document.activeElement, h.buttons.get('.ed-message'));
});

for (const focus of ['origin', 'composer', 'separately focused replacement'])
test(`same-room roster replacement preserves pending inspection focus intent: ${focus}`, async () => {
  const h = inspectionHarness(), nodes = [];
  const rail = {
    set innerHTML(value) {
      for (const node of nodes) {
        if (h.document.activeElement === node) h.document.activeElement = h.document.body;
        node.isConnected = false;
      }
      nodes.length = 0;
      for (const match of value.matchAll(/data-bot-inspect="([^"]+)"/g)) nodes.push({
        dataset: { botInspect: match[1] }, isConnected: true,
        focus() { h.document.activeElement = this; },
        addEventListener(event, listener) { this[event] = listener; },
      });
    }, querySelectorAll() { return nodes; },
  };
  const get = h.state.$; h.state.$ = id => id === 'fleet' ? rail : get(id);
  h.state.renderState = node => { if (node === rail) return false; node.innerHTML = 'loading'; };
  h.state.activeRoom = () => botIdentity.fleet;
  let pending; h.state.setView = () => { pending = h.state.openEquipment(h.state.equipmentAlias); };
  runInNewContext(rosterHelpers, h.state);
  const board = { state: 'ok', data: { identities: [{ ...botIdentity, kind: 'actor', short: 'Worker' }] } };
  h.state.renderFleet(board); const origin = nodes[0]; origin.focus(); origin.click();
  assert.equal(h.state.equipmentFocus.origin, origin); assert.equal(h.reads.length, 1);
  const composer = { isConnected: true };
  if (focus !== 'origin') h.document.activeElement = composer;
  h.state.renderFleet(board); const replacement = nodes[0];
  assert.equal(origin.isConnected, false); assert.notEqual(replacement, origin); assert.equal(h.reads.length, 1);
  if (focus === 'origin') {
    assert.equal(h.document.activeElement, replacement); assert.equal(h.state.equipmentFocus.origin, replacement);
  } else {
    assert.equal(h.document.activeElement, composer); assert.equal(h.state.equipmentFocus.origin, origin);
    if (focus === 'separately focused replacement') replacement.focus();
  }
  h.reads[0].resolve(equipmentEnvelope(botIdentity.alias)); await pending;
  assert.equal(h.document.activeElement, focus === 'origin' ? h.buttons.get('.ed-close') : focus === 'composer' ? composer : replacement);
  assert.equal(h.state.equipmentFocus, null); assert.deepEqual(h.selections, []);
});


test('read profile admission refuses legacy, malformed and unsupported profiles before any private work', async () => {
  const invalid = [undefined, null, [], {}, { ...readProfile, version: 2 },
    { ...readProfile, profile: 'cross-origin' }, { ...readProfile, host_uid: 'host_bad' },
    { ...readProfile, host_uid: readProfile.host_uid + '\n' }, { ...readProfile, actions: ['message'] }];
  for (const profile of invalid) {
    const h = harness([{ status: 200, data: { state: 'ready', read_profile: profile } }]);
    await h.controls.ready;
    h.api.createEventSource('/api/stream');
    assert.equal(await h.api.jget('/api/tasks'), null);
    assert.equal(await h.api.interactionContext('engineering'), null);
    await assert.rejects(h.api.sendAction(action), /unknown/);
    assert.equal(h.resumed, 0);
    assert.equal(h.natives.length, 0);
    assert.equal(h.calls.length, 1);
    assert.match(h.node('status').textContent, /compatibility.*Update both.*reload/);
    assert.deepEqual(h.replacements, []);
    h.controls.dispose();
  }
});

test('read profile pins the host through renewal and permits only same-host explicit recovery', async () => {
  const switched = { ...ready.data, read_profile: { ...readProfile, host_uid: 'host_' + '2'.repeat(32) } };
  const h = harness([ready, ok(switched), ok(switched), ready, ok(['board'])]);
  await h.controls.ready;
  h.api.createEventSource('/api/stream');
  await h.click('renew');
  assert.equal(h.natives[0].closed, true);
  assert.equal(await h.api.jget('/api/tasks'), null);
  await assert.rejects(h.api.actionReceipt(action), /unknown/);
  assert.equal(h.resumed, 1);
  assert.match(h.node('status').textContent, /host changed.*Reopen Plane/);
  await h.click('check');
  assert.equal(h.resumed, 1);
  await h.click('check');
  assert.equal(h.resumed, 2);
  assert.deepEqual(await h.api.jget('/api/tasks'), ['board']);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1); // one explicit renewal, no mutation replay
  h.controls.dispose();
});

test('read profile recovery rejects an incompatible action-refusal status without signing out or replay', async () => {
  const h = harness([ready, denied, { status: 200, data: { state: 'ready' } }]);
  await h.controls.ready;
  h.api.createEventSource('/api/stream');
  await assert.rejects(h.api.sendAction(action), /unknown/);
  assert.equal(h.natives[0].closed, true);
  assert.equal(await h.api.jget('/api/tasks'), null);
  assert.equal(h.calls.length, 3);
  assert.match(h.node('status').textContent, /Update both/);
  assert.deepEqual(h.replacements, []);
  h.controls.dispose();
});

test('read profile missing on renewal cannot reopen an admitted stream', async () => {
  const h = harness([ready, { status: 200, data: { state: 'ready', expires_at: 123 } }]);
  await h.controls.ready; h.api.createEventSource('/api/stream');
  await h.click('renew');
  assert.equal(h.natives[0].closed, true);
  assert.equal(h.natives.length, 1);
  assert.equal(h.resumed, 1);
  assert.equal(await h.api.jget('/api/tasks'), null);
  assert.match(h.node('status').textContent, /compatibility/);
  h.controls.dispose();
});
