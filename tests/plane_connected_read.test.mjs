import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
const load = text => import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const ownerText = (await readFile(new URL('../claudlobby/plane/ui/owner-api-client.js', import.meta.url), 'utf8'))
  .replace('"./action-state.js"', JSON.stringify(new URL('../claudlobby/plane/ui/action-state.js', import.meta.url).href));
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
  let time = 0, serverToken = null, serverExpires = 0;
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
      if (path === 'issue') { serverToken = TOKEN; serverExpires = time + 900000; return reply(200, ready(TOKEN)); }
      if (init.headers.Authorization !== 'Bearer ' + serverToken) return reply(403, { state: 'denied' });
      if (path === 'status') return reply(200, {...ready(),expires_in:Math.floor((serverExpires-time)/1000)});
      if (path === 'renew') { serverToken = ROTATED; serverExpires = time + 900000; return reply(200, ready(ROTATED)); }
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

for (const stage of ['challenge', 'assertion', 'issue'])
  test(`failed ${stage} offers explicit Connect without claiming expiry or retrying`, async () => {
    let failing = true;
    const h = harness(stage === 'assertion' ? {
      obtainSessionAssertion: async () => {
        if (failing) throw new Error('Assertion unavailable');
        return { version: 1, assertion: 'e30.e30.AAAA', expires_at: 1800000120 };
      },
    } : {});
    if (stage !== 'assertion') h.overrides.set(stage, () => { throw new Error('Host unavailable'); });
    assert.equal(await h.api.connect(), false);
    assert.equal(h.api.snapshot().state, 'unavailable');
    assert.equal(h.api.continuity().connectRequired, true);
    assert.equal(h.api.continuity().reconnectRequired, false);
    assert.doesNotMatch(h.api.snapshot().remediation, /expired/i);
    const calls = h.calls.length;
    h.api.activity({ visible: true, userInput: true }); await flush();
    assert.equal(h.calls.length, calls, 'activity never retries an uncertain handshake');
    failing = false; h.overrides.delete(stage);
    assert.equal(await h.api.connect(), true);
    assert.equal(h.api.continuity().connectRequired, false);
    h.api.dispose();
  });

test('start and explicit check never sign in, synthesize pairing or replay a mutation', async () => {
  const h = harness(); await h.api.start();
  assert.equal(h.api.snapshot().state, 'denied');
  assert.equal(h.api.connectionState(), 'not_connected');
  assert.doesNotMatch(h.api.snapshot().remediation || '', /expired/i);
  await h.api.check();
  assert.equal(h.api.snapshot().state, 'denied');
  assert.equal(h.calls.length, 0); assert.equal(h.signings.length, 0);
  assert.notEqual(h.api.snapshot().state, 'ready'); h.api.dispose();
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
  assert.equal(h.api.continuity().connectRequired, false, 'terminal incompatibility cannot offer recovery');
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
  await h.api.renew(); assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 1);
  rotation.resolve(reply(200, ready(ROTATED))); await renewal;
  h.overrides.delete('read/tasks');
  // Default synthetic server learns the deliberately held successful rotation.
  h.overrides.set('read/summary', init => { assert.equal(init.headers.Authorization, 'Bearer ' + ROTATED); return reply(200, { state: 'ok' }); });
  late.resolve(reply(403, { state: 'denied' })); assert.equal(await read, null);
  assert.equal(h.api.snapshot().state, 'ready');
  assert.deepEqual(await h.api.readHandle.jget('/api/summary'), { state: 'ok' });
  assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 1); h.api.dispose();
});

test('disconnect during renewal signs out the rotated token without admitting a ready session',async()=>{
  const h=harness(),held=deferred();await h.api.connect();
  h.overrides.set('renew',()=>held.promise);
  h.overrides.set('logout',init=>{assert.equal(init.headers.Authorization,'Bearer '+ROTATED);return reply(200,{version:1,state:'signed_out'});});
  const states=[];h.api.subscribe(s=>states.push(s.state));
  const renewing=h.api.renew();await h.api.logout();
  held.resolve(reply(200,ready(ROTATED)));await renewing;await flush();
  assert.equal(h.api.snapshot().state,'signed_out');assert.ok(!states.includes('ready'));
  assert.equal(h.calls.filter(c=>c.url.endsWith('/logout')).length,1);
  h.api.activity({visible:true,userInput:true});await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);h.api.dispose();
});

test('recent visible input renews once at the warning boundary and installs the rotated lifetime', async () => {
  const h = harness(); await h.api.connect();
  h.time = 600000; h.api.activity({visible:true,userInput:true});
  h.time = 780000; h.runTimer(180000); await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);
  assert.equal(h.api.snapshot().state,'ready');
  assert.equal(h.api.continuity().remainingSeconds,900);
  assert.equal(h.api.continuity().warning,false);
  h.time = 900001;
  assert.ok(await h.api.readHandle.jget('/api/tasks'));
  assert.equal(h.calls.at(-1).init.headers.Authorization,'Bearer '+ROTATED);
  h.api.dispose();
});

test('polls never count as activity; hidden and idle sessions wait for visible input within the window', async () => {
  for (const hidden of [false,true]) {
    const h=harness();await h.api.connect();
    h.api.activity({visible:true,userInput:true});
    h.time=700000;await h.api.readHandle.jget('/api/tasks');await h.api.check();
    if(hidden)h.api.activity({visible:false,userInput:true});
    h.time=780000;h.runTimer(80000);await flush();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
    assert.equal(h.api.continuity().warning,true);
    h.api.activity({visible:true});await flush();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
    h.api.activity({visible:true,userInput:true});h.runTimer(0);await flush();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);
    h.api.dispose();
  }
});

test('unknown automatic renewal never retries on input, polling or timer callbacks; explicit check can recover',async()=>{
  const h=harness();await h.api.connect();h.time=780000;
  h.overrides.set('renew',()=>{throw new Error('lost renewal');});
  h.api.activity({visible:true,userInput:true});h.runTimer(0);await flush();
  assert.equal(h.api.snapshot().state,'unavailable');assert.equal(h.api.continuity().renewalUnknown,true);
  assert.equal(h.api.continuity().reconnectRequired,false,'uncertain renewal is not known expiry');
  h.api.activity({visible:true,userInput:true});await flush();
  assert.equal(h.timers.filter(t=>t.active&&t.ms===0).length,0);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);
  h.overrides.delete('renew');await h.api.check();h.runTimer(0);await flush();
  assert.equal(h.api.snapshot().state,'ready');assert.equal(h.api.continuity().renewalUnknown,false);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,2);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,1);
  h.api.dispose();
});

test('continuity says a renewal is under way until its outcome is recorded, and only a permanent end is ended',async()=>{
  const h=harness();await h.api.connect();
  assert.deepEqual([h.api.continuity().renewing,h.api.continuity().ended,h.api.continuity().changed],[false,false,false]);
  const held=deferred();h.overrides.set('renew',()=>held.promise);
  const renewal=h.api.renew();await flush();
  assert.equal(h.api.continuity().renewing,true);
  held.resolve(Promise.reject(new Error('lost')));await renewal;await flush();
  assert.deepEqual([h.api.continuity().renewing,h.api.continuity().renewalUnknown],[false,true]);
  h.time=900000;h.runTimer(900000);await flush();
  assert.equal(h.api.continuity().ended,false,'an expired session can Connect again');
  h.api.dispose();
});
// A renewal the computer refused is a known outcome: only a renewal no answer settled is unknown.
for(const [name,status,state,unknown] of [['refused',()=>reply(403,{state:'denied'}),'denied',false],
  ['lost',()=>{throw new Error('lost status');},'unavailable',true]])
  test(`a refused renewal whose status check is ${name} is ${unknown?'still unknown':'not unknown'} and reads as refused`,async()=>{
    const h=harness();await h.api.connect();
    h.overrides.set('renew',()=>reply(403,{state:'denied'}));h.overrides.set('status',status);
    assert.equal(await h.api.renew(),false);await flush();
    assert.equal(h.api.snapshot().state,state);assert.equal(h.api.continuity().renewalUnknown,unknown);
    assert.equal(h.api.connectionState(),'denied','the computer refused the renewal');
    h.api.dispose();
  });
// A well-formed challenge from another computer or binding at the same address ends this page's connection.
for(const [name,change] of [['another computer',{host_uid:'host_'+'d'.repeat(32)}],['a new binding',{binding_generation:uuid(8)}],
  ['a new binding revision',{binding_revision:2}]])
  test(`a Connect whose challenge comes from ${name} is final and sends no issue`,async()=>{
    const h=harness();await h.api.connect();h.time=900000;h.runTimer(900000);await flush();
    assert.equal(h.api.continuity().reconnectRequired,true);
    h.overrides.set('challenge',()=>reply(202,{...challenge,...change}));
    const issued=h.calls.filter(c=>c.url.endsWith('/issue')).length;
    assert.equal(await h.api.connect(),false);
    assert.equal(h.api.snapshot().state,'unavailable');
    assert.deepEqual([h.api.continuity().connectRequired,h.api.continuity().reconnectRequired],[false,false],'nothing to retry');
    assert.deepEqual([h.api.continuity().ended,h.api.continuity().changed],[true,true],'ended because the computer changed');
    assert.match(h.api.snapshot().remediation,/Reopen Plane/);
    assert.equal(h.signings.length,1,'no website assertion is asked for');
    assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,issued,'no session issue is sent');
    h.overrides.delete('challenge');
    assert.equal(await h.api.connect(),false,'and a later Connect in this page is refused');
    h.api.dispose();
  });

test('expired and explicitly disconnected sessions cannot renew or reconnect on later input',async()=>{
  for(const logout of [false,true]){
    const h=harness();await h.api.connect();h.api.activity({visible:true,userInput:true});
    if(logout)await h.api.logout();else{h.time=900000;h.runTimer(900000);}
    h.api.activity({visible:true,userInput:true});await flush();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
    assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,1);
    assert.equal(h.api.snapshot().state,logout?'signed_out':'unavailable');
    if(!logout)assert.match(h.api.snapshot().remediation,/Choose Connect again/);
    h.api.dispose();assert.equal(h.timers.filter(t=>t.active).length,0);
  }
});

test('automatic renewal waits for one in-flight action and never replays its send',async()=>{
  const h=harness(),held=deferred(),recipient='actor_'+'c'.repeat(32);
  const scope={workspace:'connected-test',host:HOST,fleet:'team',viewer:'message-grant1'};
  h.overrides.set('actions/context',()=>reply(200,{version:1,room:'team',scope,simulation:false,actions:['message'],recipients:[{id:recipient,label:'Lead'}]}));
  h.overrides.set('actions/send',()=>held.promise);
  const actions=h.api.createActions({key:'computer-1'});await h.api.connect();
  const context=await actions.interactionContext('team');
  const request={kind:'message',scope:context.scope,target:{recipient:context.recipients[0].id,task_id:null},request_id:uuid(99),submitted_at:'2026-10-20T00:00:00Z',body:'One explicit send'};
  const sending=actions.sendAction(request);await flush();
  h.time=780000;h.api.activity({visible:true,userInput:true});await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
  held.resolve(reply(200,{version:1,status:'delivered',...request,scope,target:{recipient,task_id:null},body:undefined}));
  await sending;h.runTimer(0);await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/send')).length,1);
  h.api.dispose();
});

test('lost renew and failed logout stay unknown, never replay, and status alone can recover retained token', async () => {
  const h = harness(); await h.api.connect();
  h.overrides.set('renew', () => { throw new Error('do not expose credentials'); });
  await h.api.renew(); assert.equal(h.api.snapshot().state, 'unavailable');
  assert.equal(h.api.continuity().connectRequired, false, 'retained sessions recover through Check');
  assert.equal(h.calls.filter(c => c.url.endsWith('/renew')).length, 1);
  await h.api.check(); assert.equal(h.api.snapshot().state, 'ready');
  h.overrides.set('logout', () => reply(403, { state: 'denied' }));
  await h.api.logout(); assert.equal(h.api.snapshot().state, 'unavailable');
  assert.match(h.api.snapshot().remediation, /Disconnected/);
  assert.equal(h.calls.filter(c => c.url.endsWith('/logout')).length, 1); h.api.dispose();
});

test('expiry, monotonic rollback and nonfinite clock stop private reads without mutation replay', async () => {
  for (const value of [900001, -1, NaN, Infinity]) {
    const h = harness(); await h.api.connect(); h.time = value;
    assert.equal(await h.api.readHandle.jget('/api/tasks'), null);
    assert.equal(h.calls.length, 2); assert.notEqual(h.api.snapshot().state, 'ready');
    assert.equal(h.api.continuity().reconnectRequired, value === 900001); h.api.dispose();
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

test('task history cursor stays on the admitted direct host read with duplicate keys refused',async()=>{
  const h=harness();await h.api.connect();
  assert.equal((await h.api.readHandle.jget('/api/tasks?fleet=data&cursor=opaque-page')).state,'ok');
  const url=new URL(h.calls.at(-1).url);
  assert.equal(url.pathname,'/api/owner/connected/read/tasks');
  assert.equal(url.search,'?fleet=data&cursor=opaque-page');
  const count=h.calls.length;
  assert.equal(await h.api.readHandle.jget('/api/tasks?cursor=a&cursor=b'),null);
  assert.equal(h.calls.length,count);h.api.dispose();
});

test('read endpoint500 stays local;503 checks current authority without disturbing either admitted host', async () => {
  const a = harness(), b = harness(); await a.api.connect(); await b.api.connect();
  a.overrides.set('read/tasks', () => reply(500, { state: 'unavailable' }));
  assert.equal(await a.api.readHandle.jget('/api/tasks'), null); assert.equal(a.api.snapshot().state, 'ready');
  a.overrides.set('read/tasks', () => reply(503, { state: 'unavailable' }));
  const epoch=a.api.snapshot().epoch;
  assert.equal(await a.api.readHandle.jget('/api/tasks'), null); assert.equal(a.api.snapshot().state, 'ready');
  assert.equal(a.api.snapshot().epoch,epoch);
  assert.equal((await b.api.readHandle.jget('/api/tasks')).state, 'ok');
  assert.equal(a.calls.filter(c=>c.url.endsWith('/status')).length,1);
  assert.equal(a.calls.filter(c => c.url.endsWith('/issue')).length, 1);
  a.api.dispose(); b.api.dispose();
});

test('one fresh status probe deduplicates concurrent failed reads while preserving healthy results and the stream',async()=>{
  const h=harness(),probe=deferred(),identity=deferred();await h.api.connect();
  h.api.readHandle.createEventSource('/api/stream');await flush();
  const epoch=h.api.snapshot().epoch;
  h.overrides.set('status',()=>probe.promise);
  h.overrides.set('read/overview',()=>reply(503,{state:'unavailable'}));
  h.overrides.set('read/channel',()=>reply(503,{state:'unavailable'}));
  h.overrides.set('read/identities',()=>identity.promise);
  const tasks=await h.api.readHandle.jget('/api/tasks');
  const identities=h.api.readHandle.jget('/api/identities');
  const failed=[h.api.readHandle.jget('/api/overview'),h.api.readHandle.jget('/api/channel?limit=120')];
  await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length,1);
  assert.equal(h.api.snapshot().epoch,epoch);assert.equal(h.streams[0].init.signal.aborted,false);
  probe.resolve(reply(200,ready()));await Promise.all(failed);
  identity.resolve(reply(200,{state:'ok',data:{identities:[{uid:'actor'}]}}));
  assert.equal((await identities).data.identities.length,1);assert.equal(tasks.state,'ok');
  assert.equal(h.api.snapshot().epoch,epoch);assert.equal(h.streams.length,1);
  // A previous success is not a lease: a later failure requires its own fresh probe.
  await h.api.readHandle.jget('/api/overview');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length,2);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,1);h.api.dispose();
});

for(const [label,response] of [
  ['unavailable',()=>reply(503,{state:'unavailable'})],
  ['revoked',()=>reply(403,{state:'denied'})],
  ['unauthorized',()=>reply(401,{state:'denied'})],
  ['malformed profile',()=>reply(200,{...ready(),read_profile:{host_uid:HOST}})],
  ['changed host',()=>reply(200,{...ready(),read_profile:{...ready().read_profile,host_uid:'host_'+'b'.repeat(32)}})],
  ['changed binding',()=>reply(200,{...ready(),session_context:{...sessionContext,binding_revision:2}})],
])test(`failed read status probe fences siblings and stream when ${label}`,async()=>{
  const h=harness(),held=deferred();await h.api.connect();h.api.readHandle.createEventSource('/api/stream');await flush();
  const epoch=h.api.snapshot().epoch;
  h.overrides.set('read/identities',()=>held.promise);
  const sibling=h.api.readHandle.jget('/api/identities');
  h.overrides.set('read/overview',()=>reply(503,{state:'unavailable'}));h.overrides.set('status',response);
  assert.equal(await h.api.readHandle.jget('/api/overview'),null);
  assert.notEqual(h.api.snapshot().state,'ready');assert.ok(h.api.snapshot().epoch>epoch);
  assert.equal(h.streams[0].init.signal.aborted,true);
  held.resolve(reply(200,{state:'ok',data:{identities:[{uid:'late'}]}}));assert.equal(await sibling,null);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length,1);h.api.dispose();
});

for(const change of ['disconnect','context','expiry'])test(`held read recovery cannot restore authority after ${change}`,async()=>{
  const h=harness(),probe=deferred();await h.api.connect();
  h.overrides.set('read/overview',()=>reply(503,{state:'unavailable'}));h.overrides.set('status',()=>probe.promise);
  const reading=h.api.readHandle.jget('/api/overview');await flush();
  if(change==='disconnect')await h.api.logout();
  else if(change==='context')h.context={...h.context,context_generation:uuid(9)};
  else{h.time=900000;h.runTimer(900000);}
  probe.resolve(reply(200,ready()));assert.equal(await reading,null);
  assert.notEqual(h.api.snapshot().state,'ready');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,1);h.api.dispose();
});

test('a timed-out recovery probe closes the source instead of retrying or preserving sibling rows',async()=>{
  const h=harness();await h.api.connect();h.api.readHandle.createEventSource('/api/stream');await flush();
  h.overrides.set('read/overview',()=>reply(503,{state:'unavailable'}));
  h.overrides.set('status',init=>new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(new Error('probe deadline')),{once:true})));
  const reading=h.api.readHandle.jget('/api/overview');await flush();h.runTimer(15000);
  assert.equal(await reading,null);assert.equal(h.api.snapshot().state,'unavailable');
  assert.equal(h.streams[0].init.signal.aborted,true);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length,1);
  assert.equal(h.timers.filter(t=>t.active&&t.ms===1000).length,0);h.api.dispose();
});

test('authenticated stream source loss still fences held adapter rows during a successful read recovery probe',async()=>{
  const adapter=await readFile(new URL('../claudlobby/plane/ui/two-source-read.js',import.meta.url),'utf8');
  const {createWorkspaceReadTransport}=await load(adapter.replace('"./owner-api-client.js"',JSON.stringify(ownerURL)));
  const h=harness(),probe=deferred(),identities=deferred();await h.api.connect();
  const transport=createWorkspaceReadTransport({sources:[{key:'mini',label:'Mini',reader:h.api.readHandle}]});
  transport.createEventSource('/api/stream');await flush();
  h.overrides.set('read/identities',()=>identities.promise);
  h.overrides.set('read/overview',()=>reply(503,{state:'unavailable'}));h.overrides.set('status',()=>probe.promise);
  const rows=transport.jget('/api/identities'),overview=transport.jget('/api/overview');await flush();
  h.push('event: source\ndata: {"state":"unreadable"}\n\n');await flush();
  probe.resolve(reply(200,ready()));await overview;
  identities.resolve(reply(200,{state:'ok',data:{identities:[{uid:'late'}]}}));
  assert.equal((await rows).state,'unavailable');transport.dispose();h.api.dispose();
});

test('rotating the token while read recovery is held cannot admit its old-token result',async()=>{
  const h=harness(),probe=deferred();await h.api.connect();
  h.overrides.set('read/overview',()=>reply(503,{state:'unavailable'}));h.overrides.set('status',()=>probe.promise);
  const epoch=h.api.snapshot().epoch,reading=h.api.readHandle.jget('/api/overview');await flush();
  await h.api.renew();assert.equal(h.api.snapshot().state,'ready');
  probe.resolve(reply(200,ready()));assert.equal(await reading,null);
  assert.ok(h.api.snapshot().epoch>epoch);assert.equal(h.api.snapshot().state,'ready');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);h.api.dispose();
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

test('finite source gate shares two slots with distinct contexts and starts deadlines only on dispatch', async () => {
  const h = harness(); await h.api.connect();
  const first = deferred(), second = deferred();
  h.overrides.set('read/tasks', () => first.promise);
  h.overrides.set('read/channel', () => second.promise);
  const reads = [h.api.readHandle.jget('/api/tasks'), h.api.readHandle.jget('/api/channel')];
  await flush();
  const timerCount = h.timers.filter(t => t.active && t.ms === 15000).length;
  const actions = h.api.createActions({ key: 'pi' });
  reads.push(h.api.readHandle.jget('/api/summary'), actions.interactionContext('team'), actions.nudgeContext('team'));
  await flush();
  assert.equal(h.calls.filter(c => /\/read\/|\/actions\/context/.test(c.url)).length, 2);
  assert.equal(h.timers.filter(t => t.active && t.ms === 15000).length, timerCount, 'queued reads consume no network deadline');
  first.resolve(reply(200, { state: 'ok', data: {} })); await flush();
  assert.deepEqual(h.calls.filter(c => c.url.endsWith('/actions/context')).map(c => JSON.parse(c.init.body)),
    [{ room: 'team' }, { room: 'team', kind: 'nudge' }], 'distinct capabilities remain separate FIFO reads');
  second.resolve(reply(200, { state: 'ok', data: {} }));
  await Promise.all(reads); h.api.dispose();
});

test('finite source slots remain held through response bodies and do not block another host', async () => {
  const h = harness(), other = harness(); await h.api.connect(); await other.api.connect();
  const bodies = [];
  for (const path of ['read/tasks', 'read/channel']) h.overrides.set(path, () => new Response(new ReadableStream({ start(c) { bodies.push(c); } })));
  const reads = [h.api.readHandle.jget('/api/tasks'), h.api.readHandle.jget('/api/channel')]; await flush();
  reads.push(h.api.readHandle.jget('/api/summary')); await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/summary')).length, 0);
  assert.equal((await other.api.readHandle.jget('/api/summary')).state, 'ok');
  bodies[0].enqueue(new TextEncoder().encode(JSON.stringify({ state: 'ok', data: {} }))); bodies[0].close();
  await flush(); assert.equal(h.calls.filter(c => c.url.endsWith('/read/summary')).length, 1);
  bodies[1].enqueue(new TextEncoder().encode(JSON.stringify({ state: 'ok', data: {} }))); bodies[1].close();
  await Promise.all(reads); h.api.dispose(); other.api.dispose();
});

test('finite source queue is bounded and dispose aborts waiting reads before dispatch', async () => {
  const h = harness(); await h.api.connect();
  h.overrides.set('read/tasks', init => new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })));
  const reads = Array.from({ length: 11 }, () => h.api.readHandle.jget('/api/tasks'));
  await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/tasks')).length, 2);
  assert.equal(await reads[10], null, 'overflow settles instead of retaining an unbounded waiter');
  h.api.dispose(); assert.deepEqual(await Promise.all(reads), Array(11).fill(null));
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/tasks')).length, 2, 'no queued request escaped disposal');
});

test('finite source timeout cannot release a slot before the network attempt settles', async () => {
  const h = harness(); await h.api.connect();
  const held = [deferred(), deferred()]; let attempt = 0;
  h.overrides.set('read/tasks', () => held[attempt++].promise);
  const reads = [h.api.readHandle.jget('/api/tasks'), h.api.readHandle.jget('/api/tasks')]; await flush();
  reads.push(h.api.readHandle.jget('/api/summary')); await flush(); h.runTimer(15000); await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/summary')).length, 0, 'abort signal alone does not release the slot');
  held[0].resolve(reply(200, { state: 'ok', data: {} })); await flush();
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/summary')).length, 1);
  held[1].resolve(reply(200, { state: 'ok', data: {} })); await Promise.all(reads); h.api.dispose();
});

test('read timeout covers a held response body after headers without publishing late rows', async () => {
  const h = harness(); assert.equal(await h.api.connect(), true);
  h.api.readHandle.createEventSource('/api/stream');await flush();const epoch=h.api.snapshot().epoch;
  h.overrides.set('read/tasks', init => {
    let control;
    const body = new ReadableStream({ start(c) { control = c; } });
    init.signal.addEventListener('abort', () => control.error(new Error('private delayed body')), { once: true });
    return new Response(body, { headers: { 'Content-Type': 'application/json' } });
  });
  const read = h.api.readHandle.jget('/api/tasks'); await flush(); h.runTimer(15000);
  assert.equal(await read, null);
  assert.equal(h.api.readFailure('/api/tasks'), 'timed_out');
  assert.equal(h.api.connectionState(), 'ready');
  assert.equal(h.api.snapshot().state,'ready');assert.equal(h.api.snapshot().epoch,epoch);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length,1);
  assert.equal(h.streams.length,1);assert.equal(h.streams[0].init.signal.aborted,false);
  assert.equal(h.calls.filter(c => c.url.endsWith('/read/tasks')).length, 1);
  h.api.dispose();
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


test('late rejected status cannot relabel a disconnected newer epoch as access denied',async()=>{
 const h=harness(),late=deferred();await h.api.connect();
 h.overrides.set('status',()=>late.promise);
 const checking=h.api.check();await flush();await h.api.logout();
 const current=h.api.connectionState();assert.notEqual(current,'denied');
 late.resolve(reply(403,{state:'denied'}));await checking;
 assert.equal(h.api.connectionState(),current);
 assert.equal(await h.api.readHandle.jget('/api/tasks'),null);h.api.dispose();
});

test('setup requests wait 15 s, except Preview 95 s (past the listener\'s 90 s) and Launch 45 s', async () => {
  const h=harness(); const setup=token=>({version:1,state:'setup_only',expires_in:900,
    setup_profile:{version:2,profile:'first-team-setup-v2',host_uid:HOST},session_context:{...sessionContext},...(token?{token}:{})});
  h.overrides.set('issue',()=>reply(200,setup(TOKEN)));
  for (const operation of ['context','preview','launch','receipt']) h.overrides.set('setup/'+operation,()=>reply(200,{version:2}));
  assert.equal(await h.api.connect(),true);
  const waits={};
  for (const [operation,call] of [['context',()=>h.api.setupContext()],['preview',()=>h.api.setupPreview({})],
      ['launch',()=>h.api.setupLaunch({})],['receipt',()=>h.api.setupReceipt({})]]) {
    const before=h.timers.length; await call();
    waits[operation]=h.timers.slice(before).find(t=>[15000,45000,95000].includes(t.ms))?.ms;
  }
  assert.deepEqual(waits,{context:15000,preview:95000,launch:45000,receipt:15000});
  h.api.dispose();
});

test('setup-only identity never admits reads, supports original setup requests, and upgrades only after an actual read profile', async () => {
  const h=harness(); const setup=token=>({version:1,state:'setup_only',expires_in:900,
    setup_profile:{version:2,profile:'first-team-setup-v2',host_uid:HOST},session_context:{...sessionContext},...(token?{token}:{})});
  h.overrides.set('issue',()=>reply(200,setup(TOKEN)));
  h.overrides.set('status',()=>reply(200,setup()));
  h.overrides.set('setup/context',()=>reply(200,{version:2,request:null}));
  assert.equal(await h.api.connect(),true); assert.equal(h.api.snapshot().state,'setup_only');
  assert.notEqual(h.api.readHandle.snapshot().state,'ready');
  assert.equal(await h.api.readHandle.jget('/api/tasks'),null);
  assert.equal(h.calls.filter(c=>c.url.includes('/read/')).length,0);
  assert.equal((await h.api.setupContext()).version,2);
  assert.equal(h.calls.at(-1).init.credentials,'omit'); assert.equal(h.calls.at(-1).init.headers.Authorization,'Bearer '+TOKEN);
  await h.api.check(); assert.equal(h.api.snapshot().state,'setup_only');
  h.overrides.set('status',()=>reply(200,ready()));
  h.overrides.set('read/tasks',init=>{assert.equal(init.headers.Authorization,'Bearer '+TOKEN);return reply(200,{state:'ok',data:{path:'read/tasks'}});});
  await h.api.check(); assert.equal(h.api.snapshot().state,'ready');
  assert.deepEqual(await h.api.readHandle.jget('/api/tasks'),{state:'ok',data:{path:'read/tasks'}});
  h.api.dispose();
});

test('legacy setup-only profile is visibly incompatible and admits no reads or setup actions',async()=>{
  const h=harness();
  h.overrides.set('issue',()=>reply(200,{version:1,state:'setup_only',expires_in:900,token:TOKEN,
    setup_profile:{version:1,profile:'first-team-setup-v1',host_uid:HOST},session_context:{...sessionContext}}));
  assert.equal(await h.api.connect(),false);
  assert.notEqual(h.api.snapshot().state,'setup_only');
  assert.match(h.api.snapshot().remediation,/incompatible setup profile/);
  assert.equal(h.calls.filter(c=>c.url.includes('/read/')||c.url.includes('/setup/')).length,0);
  h.api.dispose();
});

test('ordinary lifecycle sends no mutations during held connect or an invalid document context',async()=>{
  const h=harness();await h.api.connect();
  const held=deferred();h.overrides.set('issue',()=>held.promise);
  const reconnect=h.api.connect();await flush();
  await h.api.renew();await h.api.logout();
  assert.equal(h.calls.filter(c=>/\/(renew|logout)$/.test(c.url)).length,0);
  held.resolve(reply(200,ready(TOKEN)));await reconnect;
  h.context={...h.context,context_generation:uuid(99)};
  await h.api.renew();await h.api.logout();
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

test('source-qualified context denials remain distinct and stale source denials are discarded',async()=>{
  const h=harness(),actions=h.api.createActions({key:'computer-1'});await h.api.connect();
  h.overrides.set('actions/context',()=>reply(403,{state:'denied'}));
  await assert.rejects(actions.interactionContext('team'),{code:'action_not_authorized'});
  assert.equal(h.api.snapshot().state,'ready');
  const pending=deferred();h.overrides.set('actions/context',()=>pending.promise);
  const old=actions.interactionContext('team');await flush();
  h.api.dispose();pending.resolve(reply(403,{state:'denied'}));
  assert.equal(await old,null);h.api.dispose();

});


test('current account metadata accepts eight exact references and retires a ninth before host traffic', async () => {
  const h=harness();
  h.context={...h.context,references:Array.from({length:8},(_,i)=>i?{reference_id:uuid(30+i),endpoint_origin:`https://host${i}.example.test`,revision:1}:{...ref})};
  assert.equal(await h.api.connect(),true);
  assert.equal(h.api.snapshot().state,'ready');
  h.context={...h.context,references:[...h.context.references,{reference_id:uuid(50),endpoint_origin:'https://ninth.example.test',revision:1}]};
  const before=h.calls.length;await h.api.check();
  assert.notEqual(h.api.snapshot().state,'ready');assert.equal(h.calls.length,before);
  h.api.dispose();
});

// A live provider sign-in is authorized by the bearer admitted at begin: no renewal path may rotate it.
const attemptInput = { generation: uuid(9), attempt_id: uuid(12) };
const signinBody = (state, extra = {}) => ({ ...attemptInput, state, reason: null, ...extra });
const renewals = h => h.calls.filter(c => c.url.endsWith('/renew')).length;
const signinCalls = (h, name) => h.calls.filter(c => c.url.endsWith('/setup/signin-' + name));
function signinHarness(state = 'waiting') {
  const h = harness();
  h.overrides.set('setup/signin-begin', () => reply(200, signinBody(state)));
  h.overrides.set('setup/signin-status', () => reply(200, signinBody('waiting')));
  return h;
}

test('a live sign-in defers explicit and automatic renewal until a terminal result, then renewal works', async () => {
  const h = signinHarness(); await h.api.connect();
  h.api.activity({ visible: true, userInput: true });
  assert.equal((await h.api.setupSignin('begin', attemptInput)).state, 'waiting');
  assert.equal(signinCalls(h, 'begin')[0].init.headers.Authorization, 'Bearer ' + TOKEN, 'begin used the issued bearer');
  assert.equal(await h.api.renew(), false); assert.equal(renewals(h), 0, 'explicit renewal is deferred');
  h.time = 780000; h.api.activity({ visible: true, userInput: true });
  await flush();
  assert.equal(renewals(h), 0, 'no automatic renewal while live and the one automatic attempt is not consumed');
  assert.equal((await h.api.setupSignin('status', attemptInput)).state, 'waiting');
  assert.equal(await h.api.renew(), false); assert.equal(renewals(h), 0, 'a nonterminal status releases nothing');
  h.overrides.set('setup/signin-status', () => reply(200, signinBody('connected')));
  assert.equal((await h.api.setupSignin('status', attemptInput)).state, 'connected');
  await h.api.renew(); assert.equal(renewals(h), 1, 'the terminal result releases the hold');
  h.api.dispose();
});

test('setup-only sessions hold lifecycle renewal the same way', async () => {
  const h = signinHarness(); const setup = token => ({ version: 1, state: 'setup_only', expires_in: 900,
    setup_profile: { version: 2, profile: 'first-team-setup-v2', host_uid: HOST }, session_context: { ...sessionContext }, ...(token ? { token } : {}) });
  h.overrides.set('issue', () => reply(200, setup(TOKEN))); h.overrides.set('status', () => reply(200, setup()));
  h.overrides.set('renew', () => reply(200, setup(ROTATED)));
  assert.equal(await h.api.connect(), true); assert.equal(h.api.snapshot().state, 'setup_only');
  await h.api.setupSignin('begin', attemptInput);
  assert.equal(await h.api.renew(), false); assert.equal(renewals(h), 0);
  h.overrides.set('setup/signin-status', () => reply(200, signinBody('cancelled')));
  await h.api.setupSignin('status', attemptInput);
  assert.equal(await h.api.renew(), true); assert.equal(renewals(h), 1);
  h.api.dispose();
});

test('an unknown begin result keeps the hold; only a completed refusal, disconnect or disposal ends it', async () => {
  const h = signinHarness(); await h.api.connect();
  h.overrides.set('setup/signin-begin', () => { throw new Error('lost'); });
  await assert.rejects(h.api.setupSignin('begin', attemptInput), error => error.code !== 'signin_not_started',
    'a sent begin is never reported as not started');
  assert.equal(signinCalls(h, 'begin').length, 1);
  assert.equal(await h.api.renew(), false); assert.equal(renewals(h), 0, 'a lost answer proves no cleanup');
  assert.equal(signinCalls(h, 'begin').length, 1, 'nothing replays or begins again');
  await h.api.logout();
  assert.equal(await h.api.connect(), true); await h.api.renew(); assert.equal(renewals(h), 1, 'disconnect ended the hold');
  h.api.dispose();
  const refused = signinHarness(); await refused.api.connect();
  refused.overrides.set('setup/signin-begin', () => reply(403, { state: 'denied' }));
  await assert.rejects(refused.api.setupSignin('begin', attemptInput));
  await refused.api.renew(); assert.equal(renewals(refused), 1, 'a completed refusal admitted no attempt');
  refused.api.dispose();
});

test('begin renews first unless the session covers a ten-minute attempt\'s conversation, and refuses if that fails', async () => {
  const h = signinHarness(); await h.api.connect();
  h.time = 600000; // 300 seconds left: not enough for a 630-second conversation
  await h.api.setupSignin('begin', attemptInput);
  const order = h.calls.map(c => new URL(c.url).pathname.split('/').pop()).filter(x => ['renew', 'signin-begin'].includes(x));
  assert.deepEqual(order, ['renew', 'signin-begin']);
  assert.equal(signinCalls(h, 'begin')[0].init.headers.Authorization, 'Bearer ' + ROTATED);
  assert.equal(h.api.continuity().remainingSeconds, 900, 'the host lifetime was not extended locally');
  await h.api.renew(); assert.equal(renewals(h), 1, 'no further rotation while live');
  h.api.dispose();
  // The session must also cover a ten-minute attempt's 630-second conversation, with a margin.
  for (const [left, renewed] of [[600, 1], [660, 0]]) { // seconds left of the 900-second session
    const s = signinHarness(); await s.api.connect(); s.time = (900 - left) * 1000;
    await s.api.setupSignin('begin', attemptInput);
    assert.equal(renewals(s), renewed, `${left} seconds left`);
    s.api.dispose();
  }
  const failed = signinHarness(); await failed.api.connect();
  failed.overrides.set('renew', () => { throw new Error('lost renewal'); });
  failed.time = 600000;
  await assert.rejects(failed.api.setupSignin('begin', attemptInput),
    error => /not started/.test(error.message) && error.code === 'signin_not_started');
  assert.equal(signinCalls(failed, 'begin').length, 0, 'no begin without a confirmed renewal');
  failed.api.dispose();
});

test('a redundant renewal neither erases nor replaces the admitted one; begin still waits for it', async () => {
  const h = signinHarness(); await h.api.connect();
  const held = deferred(); h.overrides.set('renew', () => held.promise);
  const first = h.api.renew(); await flush();
  assert.equal(await h.api.renew(), false, 'a redundant renewal is refused without touching the tracked one');
  const beginning = h.api.setupSignin('begin', attemptInput); await flush();
  assert.equal(signinCalls(h, 'begin').length, 0, 'begin does not run on the old token');
  held.resolve(reply(200, ready(ROTATED))); await first;
  assert.equal((await beginning).state, 'waiting');
  assert.equal(signinCalls(h, 'begin')[0].init.headers.Authorization, 'Bearer ' + ROTATED);
  assert.equal(renewals(h), 1, 'one renewal only'); h.api.dispose();
  const s = signinHarness(); const setup = token => ({ version: 1, state: 'setup_only', expires_in: 900,
    setup_profile: { version: 2, profile: 'first-team-setup-v2', host_uid: HOST }, session_context: { ...sessionContext }, ...(token ? { token } : {}) });
  s.overrides.set('issue', () => reply(200, setup(TOKEN))); s.overrides.set('status', () => reply(200, setup()));
  assert.equal(await s.api.connect(), true);
  const gate = deferred(); s.overrides.set('renew', () => gate.promise);
  const r1 = s.api.renew(); await flush(); assert.equal(await s.api.renew(), false);
  const b = s.api.setupSignin('begin', attemptInput); await flush();
  assert.equal(signinCalls(s, 'begin').length, 0, 'setup-only begin waits for the admitted renewal');
  gate.resolve(reply(200, setup(ROTATED))); await r1; await b;
  assert.equal(signinCalls(s, 'begin')[0].init.headers.Authorization, 'Bearer ' + ROTATED);
  s.api.dispose();
});

test('sign-in admission waits for a renewal already in flight and then uses the rotated bearer', async () => {
  const h = signinHarness(); await h.api.connect();
  const held = deferred(); h.overrides.set('renew', () => held.promise);
  const renewing = h.api.renew(); await flush();
  const beginning = h.api.setupSignin('begin', attemptInput); await flush();
  assert.equal(signinCalls(h, 'begin').length, 0, 'begin waits behind the renewal');
  assert.equal(await h.api.renew(), false, 'a second renewal cannot slip in while begin is admitting');
  held.resolve(reply(200, ready(ROTATED))); await renewing;
  assert.equal((await beginning).state, 'waiting');
  assert.equal(signinCalls(h, 'begin')[0].init.headers.Authorization, 'Bearer ' + ROTATED);
  assert.equal(renewals(h), 1); h.api.dispose();
});

// A synthetic first-team plan of the measured real shape (about 63 KB): 76 changes, 70 units without their
// working data, and the bot and fleet lists. The Context answer that repeats it carries it under `preview`.
const SETUP_LIMIT = 131072;
const setupRequest = { generation: uuid(9), request_id: uuid(10), config_fingerprint: 'a'.repeat(64), source_revision: 'b'.repeat(40) };
function realisticPlan() {
  const sha = 'c'.repeat(64);
  return { ...setupRequest, version: 2, expires_at: 1800000600, fleets: ['shop'],
    changes: Array.from({ length: 76 }, (_, i) => ({ target: `/home/example/fleets/shop/units/unit-${i}/config.toml`,
      before: { kind: 'file', mode: '0644', sha256: sha },
      after: { kind: 'directory', mode: '0755', files: Object.fromEntries(Array.from({ length: 4 }, (_, f) => [`file-${f}.toml`, sha])) } })),
    effects: { units: Array.from({ length: 70 }, (_, i) => ({ bot: `shop/bot-${i}`, enroll: false, fleet: 'shop', phase: 'start',
        platform: 'linux', scope: 'user', service: `claudlobby-bot-${i}.service`, sha256: sha, source: `/home/example/fleets/shop/units/unit-${i}` })),
      restart_bots: ['shop/a', 'shop/b', 'shop/c'], reload_supervision: true, fleet_sources: { shop: '/home/example/fleets/shop' },
      bot_paths: { a: '/home/example/a', b: '/home/example/b' }, coverage: { bots: 70, units: 70 },
      fleet_manifests: { shop: sha }, restart_reason: 'units changed' },
    dispatch: 'prepared', phase: 'quiesce' };
}
const readyContext = plan => ({ version: 2, template: 'revenue-web-app', fleet: 'shop', repository: 'example-org/shop',
  source_revision: setupRequest.source_revision, generation: setupRequest.generation, expires_at: 1800003600, request: { ...setupRequest },
  launch_reserved: false, provider_signin: true, signin_attempt: { attempt_id: uuid(11), generation: setupRequest.generation, state: 'connected', reason: null }, continuation: "'/example/cli' host update continue",
  preview_state: 'ready', preview: { ...plan, goal: 'Improve tests' } });
const bytesOf = value => new TextEncoder().encode(JSON.stringify(value)).length;
// The same answer padded (inside one string field) to exactly `size` bytes.
const padded = (value, size) => ({ ...value, pad: 'x'.repeat(size - bytesOf({ ...value, pad: '' })) });
async function setupOnlyHarness() {
  const h = harness(); const setup = token => ({ version: 1, state: 'setup_only', expires_in: 900,
    setup_profile: { version: 2, profile: 'first-team-setup-v2', host_uid: HOST }, session_context: { ...sessionContext }, ...(token ? { token } : {}) });
  h.overrides.set('issue', () => reply(200, setup(TOKEN))); h.overrides.set('status', () => reply(200, setup()));
  assert.equal(await h.api.connect(), true); return h;
}

test('a realistic first-team Preview answer and the Context answer that repeats it pass the setup ceiling; one byte over is refused', async () => {
  const plan = realisticPlan(), context = readyContext(plan);
  assert.ok(bytesOf(plan) > 55000 && bytesOf(plan) < 75000, String(bytesOf(plan)));
  assert.ok(bytesOf(context) > 55000 && bytesOf(context) < 75000, String(bytesOf(context)));
  for (const [operation, call, answer] of [['preview', h => h.api.setupPreview({ generation: setupRequest.generation, goal: 'Improve tests' }), plan],
      ['context', h => h.api.setupContext(), context]]) {
    const h = await setupOnlyHarness();
    h.overrides.set('setup/' + operation, () => reply(200, answer));
    assert.deepEqual(await call(h), answer, operation + ' at the intended size');
    h.overrides.set('setup/' + operation, () => reply(200, padded(answer, SETUP_LIMIT)));
    assert.equal(bytesOf(await call(h)), SETUP_LIMIT, operation + ' exactly at the limit');
    h.overrides.set('setup/' + operation, () => reply(200, padded(answer, SETUP_LIMIT + 1)));
    await assert.rejects(call(h), operation + ' one byte over the limit');
    h.api.dispose();
  }
});

test('the setup panel reaches the ready state from a realistic Context answer; one over the limit says it could not get an answer and offers Check preview', async () => {
  const { createGuidedSetup } = await import(new URL('../claudlobby/plane/ui/guided-setup.js', import.meta.url));
  const none = { ...readyContext(realisticPlan()), request: null, preview_state: 'none', preview: undefined };
  const panelFor = async (previewAnswer, laterContext) => {
    const h = await setupOnlyHarness(), saved = new Map(); let reads = 0;
    h.overrides.set('setup/context', () => reply(200, reads++ === 0 ? none : laterContext));
    h.overrides.set('setup/preview', () => reply(200, previewAnswer));
    const setup = createGuidedSetup({ client: h.api, storage: { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value) },
      storageKey: 'size', now: () => 1800000000 });
    await setup.run('context'); setup.setGoal('Improve tests'); await setup.run('preview'); await flush(); return { h, setup };
  };
  const plan = realisticPlan(), ready = await panelFor(plan, readyContext(plan));
  assert.equal(ready.setup.snapshot().plan.changes.length, 76); assert.deepEqual(ready.setup.snapshot().pending, setupRequest);
  ready.setup.dispose(); ready.h.api.dispose();
  const over = await panelFor(padded(plan, SETUP_LIMIT + 1), padded(readyContext(plan), SETUP_LIMIT + 1));
  assert.equal(over.setup.snapshot().plan, null);
  assert.equal(over.setup.snapshot().note, 'Could not get an answer from your computer about the preview. Choose Check preview to look again.');
  over.setup.dispose(); over.h.api.dispose();
});
