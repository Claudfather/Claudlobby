import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
const source = await readFile(new URL('../claudlobby/plane/ui/owner-api-client.js', import.meta.url), 'utf8');
const load = text => import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const { createOwnerTransport } = await load(source);
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
const ready = { status: 200, data: { state: 'ready' } };
const denied = { status: 403, data: { state: 'denied' } };
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
function harness(replies = [ready], hooks = {}) {
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
    onPause() { paused++; hooks.onPause?.(api); }, onResume() { resumed++; hooks.onResume?.(api); }, onActionPause() { hooks.onActionPause?.(api); } });
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
    assert.equal(normal.mountSessionControls, undefined);
    await normal.jget('/api/tasks');
    const synthetic = await load(await readFile(new URL('./fixtures/plane_work_loop/api-client.js', import.meta.url), 'utf8'));
    assert.equal(synthetic.mountSessionControls, undefined);
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
      sessionPaused: false, generation: 1, trustGen: 1, refreshTimer: null, safetyTimer: null,
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

test('app resume restarts current fleet, equipment and search; later pause fences equipment reopening', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const helper = app.slice(app.indexOf('function resumeOwnerReads()'), app.indexOf('// Bootstrap LAST'));
  for (const stale of [false, true]) {
    const inventory = deferred(), calls = [];
    const search = { value: ' active query ', dispatchEvent(event) { calls.push(['search', event.type]); } };
    const state = { sessionPaused: true, sessionEpoch: 2, currentView: 'fleet', equipmentAlias: 'synthetic-worker',
      refreshBoards() { calls.push(['boards']); }, pollFleet() { calls.push(['fleet']); return inventory.promise; },
      openEquipment(alias) { calls.push(['equipment', alias]); },
      $: id => id === 'equip-detail' ? { hidden: false } : search, Event: class { constructor(type) { this.type = type; } } };
    runInNewContext(`${helper}\nresumeOwnerReads()`, state);
    assert.equal(state.sessionPaused, false);
    assert.deepEqual(calls, [['boards'], ['fleet'], ['search', 'input']]);
    if (stale) { state.sessionPaused = true; state.sessionEpoch++; }
    inventory.resolve(); await flush();
    assert.equal(calls.filter(c => c[0] === 'equipment').length, stale ? 0 : 1);
  }
});

test('aborted inventory from an earlier session cannot overwrite the resumed panel', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const poll = app.slice(app.indexOf('async function pollFleet()'), app.indexOf('function orgNode('));
  const inventory = deferred(), rendered = [];
  const state = { sessionPaused: false, sessionEpoch: 1, currentFleet: 'synthetic', currentView: 'fleet',
    fleetQuery: () => '?fleet=synthetic', $: () => ({}), renderState() {}, renderInventory(value) { rendered.push(value); },
    jget: () => inventory.promise };
  const pending = runInNewContext(`${poll}\npollFleet()`, state);
  state.sessionEpoch++; inventory.resolve(null); await pending;
  assert.deepEqual(rendered, []);
});
