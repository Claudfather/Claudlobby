import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
const load = text => import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const ownerText = await readFile(new URL('../claudlobby/plane/ui/owner-api-client.js', import.meta.url), 'utf8');
const ownerURL = `data:text/javascript;base64,${Buffer.from(ownerText).toString('base64')}`;
const { isOwnerReadHandle } = await import(ownerURL);
const actionsURL = `data:text/javascript;base64,${Buffer.from((await readFile(new URL('../claudlobby/plane/ui/connected-actions.js', import.meta.url), 'utf8')).replace('"./action-state.js"', JSON.stringify(new URL('../claudlobby/plane/ui/action-state.js',import.meta.url).href))).toString('base64')}`;
const clientText = await readFile(new URL('../claudlobby/plane/ui/connected-read-client.js', import.meta.url), 'utf8');
const { createConnectedReadClient } = await load(clientText.replace('"./owner-api-client.js"', JSON.stringify(ownerURL)).replace('"./connected-actions.js"',JSON.stringify(actionsURL)));
const uuid = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const HOST = 'host_' + 'a'.repeat(32), NONCE = 'A'.repeat(43), TOKEN = 'B'.repeat(42) + 'A', ROTATED = 'C'.repeat(42) + 'A';
const ref = { reference_id: uuid(4), endpoint_origin: 'https://host.example.test', revision: 1 };
const doc = { issuer: 'https://accounts.example.test', operational_origin: 'https://plane.example.test',
  subject: uuid(1), workspace_id: uuid(2), context_generation: uuid(3) };
const binding = { binding_revision: 1, binding_generation: uuid(5) };
const sessionContext = { issuer: doc.issuer, subject: doc.subject, workspace_id: doc.workspace_id,
  operational_origin: doc.operational_origin, ...binding };
const challenge = { version: 1, state: 'awaiting_session_assertion', nonce: NONCE, issuer: doc.issuer,
  host_uid: HOST, operational_origin: doc.operational_origin, ...binding, expires_at: 1800000120 };
const ready = token => ({ version: 1, state: 'ready', expires_in: 900,
  read_profile: { version: 1, profile: 'direct-owner-read-v1', host_uid: HOST }, session_context: { ...sessionContext },
  ...(token ? { token } : {}) });
const reply = (status, data) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
function deferred() { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; }
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
function harness(options = {}) {
  const calls = [], signings = [], timers = [], streams = [], overrides = new Map();
  let time = 0, serverToken = null;
  let context = { version: 2, user: { id: doc.subject }, workspace: { id: doc.workspace_id, name: 'Example' },
    context_generation: doc.context_generation, references: [{ ...ref }] };
  const dependencies = { reference: ref, documentContext: doc, documentOrigin: doc.operational_origin,
    currentContext: () => context, monotonicNow: () => time,
    setTimeout(fn, ms) { const t = { fn, ms, active: true }; timers.push(t); return t; },
    clearTimeout(t) { if (t) t.active = false; },
    async obtainSessionAssertion(input, { signal }) {
      signings.push({ input, signal });
      return { version: 1, assertion: 'e30.e30.AAAA', expires_at: 1800000120 };
    },
    async fetch(url, init) {
      calls.push({ url, init });
      const path = new URL(url).pathname.replace('/api/owner/connected/', '');
      if (overrides.has(path)) return overrides.get(path)(init);
      if (path === 'challenge') return reply(202, challenge);
      if (path === 'issue') { serverToken = TOKEN; return reply(200, ready(TOKEN)); }
      if (init.headers.Authorization !== 'Bearer ' + serverToken) return reply(403, { state: 'denied' });
      if (path === 'status') return reply(200, ready());
      if (path === 'renew') { serverToken = ROTATED; return reply(200, ready(ROTATED)); }
      if (path === 'logout') { serverToken = null; return reply(200, { version: 1, state: 'signed_out' }); }
      if (path === 'read/stream') {
        let control;
        const body = new ReadableStream({ start(c) { control = c; } });
        const entry = { control, init }; streams.push(entry);
        init.signal.addEventListener('abort', () => { try { control.error(new Error('aborted')); } catch {} }, { once: true });
        return new Response(body, { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } });
      }
      return reply(200, { state: 'ok', data: { path } });
    }, ...options };
  const api = createConnectedReadClient(dependencies);
  return { api, calls, signings, timers, streams, overrides, dependencies,
    get context() { return context; }, set context(v) { context = v; },
    get time() { return time; }, set time(v) { time = v; },
    runTimer(ms) { const t = timers.find(t => t.active && t.ms === ms); assert.ok(t, `missing timer ${ms}`); t.active = false; t.fn(); },
    push(text, index = streams.length - 1) { streams[index].control.enqueue(typeof text === 'string' ? new TextEncoder().encode(text) : text); } };
}

test('explicit real handshake yields one canonical minted handle, exact signer binding and omit-only read transport', async () => {
  const h = harness(); assert.equal(isOwnerReadHandle(h.api.readHandle), true);
  assert.equal(await h.api.connect(), true);
  assert.deepEqual(h.signings[0].input, { workspace_id: doc.workspace_id, host_uid: HOST, nonce: NONCE,
    operational_origin: doc.operational_origin, ...binding, reference_id: ref.reference_id,
    expected_context_generation: doc.context_generation });
  assert.deepEqual(await h.api.readHandle.jget('/api/tasks?fleet=data'), { state: 'ok', data: { path: 'read/tasks' } });
  assert.deepEqual(h.calls.map(c => new URL(c.url).pathname), ['/api/owner/connected/challenge', '/api/owner/connected/issue', '/api/owner/connected/read/tasks']);
  for (const { url, init } of h.calls) {
    assert.equal(init.credentials, 'omit'); assert.equal(init.redirect, 'error'); assert.equal(init.cache, 'no-store');
    assert.equal(new URL(url).origin, ref.endpoint_origin); assert.equal(init.headers.Cookie, undefined);
    assert.equal(init.headers.Origin, undefined); assert.ok(!url.includes(NONCE) && !url.includes(TOKEN));
  }
  assert.equal(h.calls[0].init.body, '{}');
  assert.equal(h.calls[1].init.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(h.calls[1].init.body), { nonce: NONCE, assertion: 'e30.e30.AAAA' });
  assert.equal(h.calls[2].init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.equal(h.api.readHandle.sendAction, undefined);
  assert.ok(!JSON.stringify(h.api.snapshot()).includes(TOKEN));
  h.api.dispose();
});

test('start and explicit check never sign in, synthesize pairing or replay a mutation', async () => {
  const h = harness(); await h.api.start();
  assert.equal(h.api.snapshot().state, 'denied');
  await h.api.check();assert.equal(h.api.snapshot().state, 'denied');
  assert.equal(h.calls.length, 0); assert.equal(h.signings.length, 0);h.api.dispose();
});

for (const change of [
  { session_context: { ...sessionContext, subject: uuid(9) } },
  { session_context: { ...sessionContext, workspace_id: uuid(9) } },
  { session_context: { ...sessionContext, operational_origin: 'https://other.example.test' } },
  { session_context: { ...sessionContext, binding_generation: uuid(9) } },
  { read_profile: { version: 1, profile: 'direct-owner-read-v1', host_uid: 'host_' + 'b'.repeat(32) } },
  { read_profile: { version: 2, profile: 'future', host_uid: HOST } },
  { read_profile: { host_uid: HOST } }, { token: 'secret-incompatible' }, { expires_in: 901 },
]) test(`issue refuses mismatched bound context or unsupported capability ${JSON.stringify(change)}`, async () => {
  const h = harness(); h.overrides.set('issue', () => reply(200, { ...ready(TOKEN), ...change }));
  assert.equal(await h.api.connect(), false); assert.notEqual(h.api.snapshot().state, 'ready');
  assert.equal(await h.api.readHandle.jget('/api/tasks'), null); assert.equal(h.calls.length, 2); h.api.dispose();
});

test('context and selected reference changes close reads and fence a held issuance', async () => {
  const done = deferred(), h = harness();
  h.overrides.set('issue', () => done.promise);
  const connect = h.api.connect(); await flush();
  h.context = { ...h.context, context_generation: uuid(9) };
  done.resolve(reply(200, ready(TOKEN)));
  assert.equal(await connect, false); assert.notEqual(h.api.snapshot().state, 'ready');
  assert.equal(await h.api.readHandle.jget('/api/tasks'), null);
  assert.equal(h.calls.filter(c => c.url.endsWith('/issue')).length, 1); h.api.dispose();
});

test('dispose during signer aborts it and cannot issue later; handle disposal also wipes carrier', async () => {
  const done = deferred(), h = harness({ obtainSessionAssertion: () => done.promise });
  const connection = h.api.connect(); await flush(); h.api.readHandle.dispose();
  done.resolve({ version: 1, assertion: 'e30.e30.AAAA', expires_at: 1800000120 });
  assert.equal(await connection, false); assert.equal(h.calls.length, 1);
  assert.equal(await h.api.connect(), false); assert.equal(await h.api.readHandle.jget('/api/tasks'), null);
});

test('renewal pauses reads, installs only admitted rotation and fences old403; mutations serialize', async () => {
  const h = harness(); await h.api.connect();
  const late = deferred(), rotation = deferred();
  h.overrides.set('read/tasks', () => late.promise); h.overrides.set('renew', () => rotation.promise);
  const read = h.api.readHandle.jget('/api/tasks'), renewal = h.api.renew();
  await h.api.logout(); assert.equal(h.calls.filter(c => c.url.endsWith('/logout')).length, 0);
  rotation.resolve(reply(200, ready(ROTATED))); await renewal;
  h.overrides.delete('read/tasks');
  // Default synthetic server learns the deliberately held successful rotation.
  h.overrides.set('read/summary', init => { assert.equal(init.headers.Authorization, 'Bearer ' + ROTATED); return reply(200, { state: 'ok' }); });
  late.resolve(reply(403, { state: 'denied' })); assert.equal(await read, null);
  assert.equal(h.api.snapshot().state, 'ready');
  assert.deepEqual(await h.api.readHandle.jget('/api/summary'), { state: 'ok' });
  assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 1); h.api.dispose();
});

test('lost renew and failed logout stay unknown, never replay, and status alone can recover retained token', async () => {
  const h = harness(); await h.api.connect();
  h.overrides.set('renew', () => { throw new Error('do not expose credentials'); });
  await h.api.renew(); assert.equal(h.api.snapshot().state, 'unavailable');
  assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 1);
  await h.api.check(); assert.equal(h.api.snapshot().state, 'ready');
  h.overrides.set('logout', () => reply(403, { state: 'denied' }));
  await h.api.logout(); assert.equal(h.api.snapshot().state, 'ready');
  assert.match(h.api.snapshot().remediation, /Sign-out did not complete/);
  assert.equal(h.calls.filter(c => c.url.endsWith('/logout')).length, 1); h.api.dispose();
});

test('expiry, monotonic rollback and nonfinite clock stop private reads without mutation replay', async () => {
  for (const value of [900001, -1, NaN, Infinity]) {
    const h = harness(); await h.api.connect(); h.time = value;
    assert.equal(await h.api.readHandle.jget('/api/tasks'), null);
    assert.equal(h.calls.length, 2); assert.notEqual(h.api.snapshot().state, 'ready'); h.api.dispose();
  }
});

test('finite route/query roster refuses actions, credential URLs and duplicate keys', async () => {
  const h = harness(); await h.api.connect();
  for (const path of ['/api/owner/actions/send', '/api/tasks?token=x', '/api/tasks?fleet=a&fleet=b',
    'https://other.example.test/api/tasks', '/api/tasks#token', '/api/tasks/wi_' + 'a'.repeat(32) + '/more'])
    assert.equal(await h.api.readHandle.jget(path), null);
  // Invalid local routes degrade the owner read controller but never reach a host.
  assert.equal(h.calls.length, 2); h.api.dispose();
});

test('read endpoint500 stays local;503 recovery does not disturb independently admitted peer', async () => {
  const a = harness(), b = harness(); await a.api.connect(); await b.api.connect();
  a.overrides.set('read/tasks', () => reply(500, { state: 'unavailable' }));
  assert.equal(await a.api.readHandle.jget('/api/tasks'), null); assert.equal(a.api.snapshot().state, 'ready');
  a.overrides.set('read/tasks', () => reply(503, { state: 'unavailable' }));
  assert.equal(await a.api.readHandle.jget('/api/tasks'), null); assert.equal(a.api.snapshot().state, 'unavailable');
  assert.equal((await b.api.readHandle.jget('/api/tasks')).state, 'ok');
  a.runTimer(1000); await flush(); assert.equal(a.api.snapshot().state, 'ready');
  assert.equal(a.calls.filter(c => c.url.endsWith('/issue')).length, 1);
  a.api.dispose(); b.api.dispose();
});

test('fetch SSE survives split UTF8/CRLF, parses named source/comments and retains only source-local cursor', async () => {
  const h = harness(); await h.api.connect();
  const stream = h.api.readHandle.createEventSource('/api/stream'); const messages = [], sources = [];
  stream.onmessage = e => messages.push(e); stream.addEventListener('source', e => sources.push(e));
  await flush();
  const bytes = new TextEncoder().encode('retry: 3000\r\n\r\n: ping\r\n\r\nid: 12\r\ndata: {"text":"café"}\r\n\r\nevent: source\r\ndata: {"state":"unavailable"}\r\n\r\n');
  for (const byte of bytes) h.push(new Uint8Array([byte]));
  await flush();
  assert.equal(messages.length, 1); assert.equal(messages[0].data, '{"text":"café"}'); assert.equal(messages[0].lastEventId, '12');
  assert.equal(sources.length, 1); assert.equal(sources[0].type, 'source');
  assert.equal(h.streams[0].init.headers.Authorization, 'Bearer ' + TOKEN);
  await h.api.check(); await flush();
  assert.equal(h.streams.length, 2); assert.equal(h.streams[1].init.headers['Last-Event-ID'], '12');
  assert.equal(h.streams[1].init.credentials, 'omit'); h.api.dispose();
});

test('SSE generation fences late chunks across rotation and source failure recovery uses existing budget', async () => {
  const h = harness(); await h.api.connect();
  const stream = h.api.readHandle.createEventSource('/api/stream'); let count = 0;
  stream.onmessage = () => count++; await flush();
  const old = h.streams[0]; await h.api.renew(); await flush();
  assert.equal(old.init.signal.aborted, true); assert.equal(h.streams[1].init.headers.Authorization, 'Bearer ' + ROTATED);
  assert.equal(count, 0);
  h.streams[1].control.error(new Error()); await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 1);
  await h.api.readHandle.jget('/api/tasks');
  h.streams[2].control.error(new Error()); await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 1);
  assert.equal(h.api.snapshot().state, 'unavailable');
  assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 1); h.api.dispose();
});

test('SSE invalid UTF8/overflow/cursor rejects before publishing, without a separate retry loop', async () => {
  for (const payload of [new Uint8Array([255]), 'id: secret\ndata: never\n\n', 'data: ' + 'x'.repeat(8 * 1024 * 1024 + 1)]) {
    const h = harness(); await h.api.connect(); const stream = h.api.readHandle.createEventSource('/api/stream');
    let messages = 0; stream.onmessage = () => messages++; await flush(); h.push(payload); await flush();
    assert.equal(messages, 0); assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 1);
    h.api.dispose();
  }
});

test('constructor refuses untrusted document origin/credential URL/missing signer before any fetch', () => {
  const h = harness();
  for (const options of [{ documentOrigin: 'https://wrong.example.test' },
    { reference: { ...ref, endpoint_origin: 'https://*.example.test' } },
    { reference: { ...ref, endpoint_origin: 'https://user:pass@host.example.test' } },
    { obtainSessionAssertion: null }, { documentContext: { ...doc, read_profile: ready().read_profile } }])
    assert.throws(() => createConnectedReadClient({ ...h.dependencies, ...options }), /authenticated Plane document/);
  assert.equal(h.calls.length, 0); h.api.dispose();
});

test('signer and lost issue deadlines stop explicitly without retrying either mutation', async () => {
  const signing = harness({ obtainSessionAssertion: (_, { signal }) => new Promise((_, reject) =>
    signal.addEventListener('abort', () => reject(new Error('private signer failure')), { once: true })) });
  const attempt = signing.api.connect(); await flush(); signing.runTimer(15000);
  assert.equal(await attempt, false);
  assert.equal(signing.calls.filter(c => c.url.endsWith('/challenge')).length, 1);
  assert.equal(signing.calls.filter(c => c.url.endsWith('/issue')).length, 0);
  signing.api.dispose();

  const issued = harness(); issued.overrides.set('issue', init => new Promise((_, reject) =>
    init.signal.addEventListener('abort', () => reject(new Error('private issue failure')), { once: true })));
  const unknown = issued.api.connect(); await flush(); issued.runTimer(35000);
  assert.equal(await unknown, false); await issued.api.check();
  assert.equal(issued.calls.filter(c => c.url.endsWith('/issue')).length, 1);
  assert.equal(issued.calls.filter(c => c.url.endsWith('/challenge')).length, 1);
  assert.equal(issued.api.snapshot().state, 'unavailable'); issued.api.dispose();
});

test('read timeout covers a held response body after headers without publishing late rows', async () => {
  const h = harness(); assert.equal(await h.api.connect(), true);
  h.overrides.set('read/tasks', init => {
    let control;
    const body = new ReadableStream({ start(c) { control = c; } });
    init.signal.addEventListener('abort', () => control.error(new Error('private delayed body')), { once: true });
    return new Response(body, { headers: { 'Content-Type': 'application/json' } });
  });
  const read = h.api.readHandle.jget('/api/tasks'); await flush(); h.runTimer(15000);
  assert.equal(await read, null);
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/tasks')).length, 1);
  assert.notEqual(h.api.snapshot().state, 'ready'); h.api.dispose();
});

for (const change of ['close', 'check', 'renew']) test(`buffered SSE callback ${change} retains only the delivered cursor across replacement`, async () => {
  const h = harness(); await h.api.connect();
  const stream = h.api.readHandle.createEventSource('/api/stream'), messages = [];
  let replacement;
  stream.onmessage = event => {
    messages.push(event);
    if (change === 'close') stream.close();
    else replacement = h.api[change]();
  };
  await flush();
  h.push('id: 1\ndata: first\n\nid: 2\ndata: undelivered\n\nid: 3\n\n');
  await flush(); await replacement;
  assert.deepEqual(messages.map(e => [e.data, e.lastEventId]), [['first', '1']]);
  assert.equal(h.streams[0].init.signal.aborted, true);
  const resumed = change === 'close' ? h.api.readHandle.createEventSource('/api/stream') : stream;
  await flush();
  assert.equal(h.streams.length, 2);
  assert.equal(h.streams[1].init.headers['Last-Event-ID'], '1', 'later buffered data and id-only frames were not admitted');
  resumed.onmessage = e => messages.push(e);
  h.push('id: 2\ndata: undelivered\n\n'); await flush();
  assert.deepEqual(messages.map(e => e.data), ['first', 'undelivered']);
  h.api.dispose();
});

for (const change of ['expiry', 'context']) test(`buffered SSE ${change} loss after delivery fences later frames and id-only cursor`, async () => {
  const h = harness(); await h.api.connect();
  const stream = h.api.readHandle.createEventSource('/api/stream'), messages = [];
  stream.onmessage = e => {
    messages.push(e.data);
    if (change === 'expiry') h.time = 900001;
    else h.context = null;
  };
  await flush(); h.push('id: 1\ndata: first\n\nid: 2\ndata: undelivered\n\nid: 3\n\n'); await flush();
  assert.deepEqual(messages, ['first']);
  assert.equal(h.api.snapshot().state, 'unavailable');
  if (change === 'context') h.context = { version: 2, user: { id: doc.subject }, workspace: { id: doc.workspace_id, name: 'Example' },
    context_generation: doc.context_generation, references: [{ ...ref }] };
  assert.equal(await h.api.connect(), true);
  await flush();
  assert.equal(h.streams.at(-1).init.headers['Last-Event-ID'], '1');
  h.api.dispose();
});

test('live id-only SSE frames advance the source cursor without inventing a message', async () => {
  const h = harness(); await h.api.connect(); const stream = h.api.readHandle.createEventSource('/api/stream');
  let messages = 0; stream.onmessage = () => messages++; await flush();
  h.push('id: 7\n\n'); await flush(); await h.api.check(); await flush();
  assert.equal(messages, 0); assert.equal(h.streams[1].init.headers['Last-Event-ID'], '7'); h.api.dispose();
});

test('prototype read names fail locally without a bearer-carrying host request', async () => {
  const h = harness(); await h.api.connect(); const count = h.calls.length;
  for (const name of ['constructor', 'toString', 'valueOf', 'hasOwnProperty', '__proto__'])
    assert.equal(await h.api.readHandle.jget('/api/' + name), null);
  assert.equal(h.calls.length, count); h.api.dispose();
});

for (const stage of ['challenge', 'signer', 'issue']) for (const probe of ['check', 'start', 'readHandle'])
  test(`read-only ${probe} during held ${stage} cannot expire or abort explicit sign-in`, async () => {
    const done = deferred(), h = harness(stage === 'signer' ? {
      obtainSessionAssertion: (input, { signal }) => { h.signings.push({ input, signal }); return done.promise; },
    } : {});
    if (stage !== 'signer') h.overrides.set(stage, () => done.promise);
    const connect = h.api.connect(); await flush();
    const held = stage === 'signer' ? h.signings[0] : h.calls.find(c => c.url.endsWith('/' + stage));
    const signal = stage === 'signer' ? held.signal : held.init.signal;
    await (probe === 'readHandle' ? h.api.readHandle.start() : h.api[probe]());
    assert.equal(signal.aborted, false);
    assert.doesNotMatch(h.api.snapshot().remediation || '', /expired/i);
    assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 0);
    done.resolve(stage === 'signer' ? { version: 1, assertion: 'e30.e30.AAAA', expires_at: 1800000120 }
      : reply(stage === 'challenge' ? 202 : 200, stage === 'challenge' ? challenge : ready(TOKEN)));
    assert.equal(await connect, true);
    assert.equal(h.calls.filter(c => c.url.endsWith('/challenge')).length, 1);
    assert.equal(h.calls.filter(c => c.url.endsWith('/issue')).length, 1);
    assert.equal(h.api.snapshot().state, 'ready');
    h.api.dispose();
  });

test('a current SSE header timeout still reports loss through the existing read-only recovery', async () => {
  const h = harness(); await h.api.connect();
  h.overrides.set('read/stream', init => new Promise((_, reject) =>
    init.signal.addEventListener('abort', () => reject(new Error('held stream headers')), { once: true })));
  h.api.readHandle.createEventSource('/api/stream'); await flush(); h.runTimer(15000); await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/status')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/issue')).length, 1);
  assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 0);
  h.api.dispose();
});

test('an admitted source-loss frame fences a held action without replay or ending the read session',async()=>{
  const h=harness(),pending=deferred(),recipient='actor_'+'c'.repeat(32);
  const scope={workspace:'connected-test',host:HOST,fleet:'team',viewer:'message-grant1'};
  h.overrides.set('actions/context',()=>reply(200,{version:1,room:'team',scope,simulation:false,actions:['message'],recipients:[{id:recipient,label:'Lead'}]}));
  h.overrides.set('actions/send',()=>pending.promise);
  let losses=0;
  const actions=h.api.createActions({key:'computer-1',onSourceChange:()=>losses++});
  await h.api.connect();
  const context=await actions.interactionContext('team');
  const request={kind:'message',scope:context.scope,target:{recipient:context.recipients[0].id,task_id:null},request_id:uuid(99),submitted_at:'2026-10-10T00:00:00Z',body:'Keep this original request'};
  const sending=actions.sendAction(request);
  const refusal=assert.rejects(sending,/unknown/);
  const stream=h.api.readHandle.createEventSource('/api/stream');await flush();
  h.push('event: source\ndata: {"state":"unreadable"}\n\n');await flush();
  pending.resolve(reply(200,{version:1,status:'delivered',...request,scope,target:{recipient,task_id:null},body:undefined}));
  await refusal;
  assert.equal(losses,1);assert.equal(h.api.snapshot().state,'ready');
  assert.equal(h.calls.filter(c=>new URL(c.url).pathname.endsWith('/actions/send')).length,1);
  assert.equal(h.calls.filter(c=>new URL(c.url).pathname.endsWith('/status')).length,0);
  stream.close();h.api.dispose();assert.throws(()=>h.api.createActions({key:'computer-2'}),/unavailable/);
});

test('setup-only identity never admits reads, supports original setup requests, and upgrades only after an actual read profile', async () => {
  const h=harness(); const setup=token=>({version:1,state:'setup_only',expires_in:900,
    setup_profile:{version:1,profile:'first-team-setup-v1',host_uid:HOST},session_context:{...sessionContext},...(token?{token}:{})});
  h.overrides.set('issue',()=>reply(200,setup(TOKEN)));
  h.overrides.set('status',()=>reply(200,setup()));
  h.overrides.set('setup/context',()=>reply(200,{version:1,request:null}));
  assert.equal(await h.api.connect(),true); assert.equal(h.api.snapshot().state,'setup_only');
  assert.notEqual(h.api.readHandle.snapshot().state,'ready');
  assert.equal(await h.api.readHandle.jget('/api/tasks'),null);
  assert.equal(h.calls.filter(c=>c.url.includes('/read/')).length,0);
  assert.equal((await h.api.setupContext()).version,1);
  assert.equal(h.calls.at(-1).init.credentials,'omit'); assert.equal(h.calls.at(-1).init.headers.Authorization,'Bearer '+TOKEN);
  await h.api.check(); assert.equal(h.api.snapshot().state,'setup_only');
  h.overrides.set('status',()=>reply(200,ready()));
  h.overrides.set('read/tasks',init=>{assert.equal(init.headers.Authorization,'Bearer '+TOKEN);return reply(200,{state:'ok',data:{path:'read/tasks'}});});
  await h.api.check(); assert.equal(h.api.snapshot().state,'ready');
  assert.deepEqual(await h.api.readHandle.jget('/api/tasks'),{state:'ok',data:{path:'read/tasks'}});
  h.api.dispose();
});

test('ordinary lifecycle cannot overlap held connect or use an invalid document context',async()=>{
  const h=harness();await h.api.connect();
  const held=deferred();h.overrides.set('issue',()=>held.promise);
  const reconnect=h.api.connect();await flush();
  assert.equal(await h.api.renew(),false);assert.equal(await h.api.logout(),false);
  assert.equal(h.calls.filter(c=>/\/(renew|logout)$/.test(c.url)).length,0);
  held.resolve(reply(200,ready(TOKEN)));await reconnect;
  h.context={...h.context,context_generation:uuid(99)};
  assert.equal(await h.api.renew(),false);assert.equal(await h.api.logout(),false);
  assert.equal(h.calls.filter(c=>/\/(renew|logout)$/.test(c.url)).length,0);h.api.dispose();
});


test('ordinary check still fences retired document context without a network request',async()=>{
  const h=harness();await h.api.connect();const count=h.calls.length;
  h.context={...h.context,context_generation:uuid(99)};
  await h.api.check();
  assert.notEqual(h.api.snapshot().state,'ready');assert.equal(h.calls.length,count);
  assert.equal(await h.api.readHandle.jget('/api/summary'),null);
  assert.equal(h.calls.length,count);h.api.dispose();
});
