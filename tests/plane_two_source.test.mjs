import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
const load = text => import(`data:text/javascript;base64,${Buffer.from(text).toString('base64')}`);
const ownerText = await readFile(new URL('../claudlobby/plane/ui/owner-api-client.js', import.meta.url), 'utf8');
const ownerURL = `data:text/javascript;base64,${Buffer.from(ownerText).toString('base64')}`;
const { createOwnerReadHandle } = await import(ownerURL);
const adapterText = await readFile(new URL('../claudlobby/plane/ui/two-source-read.js', import.meta.url), 'utf8');
const { createTwoSourceReadTransport } = await load(adapterText.replace('"/owner-api-client.js"', JSON.stringify(ownerURL)));
const wi = 'wi_' + '1'.repeat(32), fleet = 'fleet_' + '2'.repeat(32), actor = 'actor_' + '3'.repeat(32);
const ok = data => ({ state: 'ok', provenance: { last_ingest_at: '2026-01-01' }, data });
function deferred() { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; }
async function source(key, n) {
  const host = 'host_' + n.repeat(32), calls = [], streams = [], routes = new Map();
  routes.set('/api/fleets', ok({ fleets: [{ alias: 'team', uid: fleet }], default: 'team' }));
  const thread = { key: wi, work_item_id: wi, task_link: { task_id: wi, fleet: 'team', fleet_uid: fleet, host_uid: host }, task_events: [], messages: [{ msg_id: 'msg_' + '4'.repeat(32), sender_alias: 'team/lead', recipient_alias: null, sender_short: 'lead', body: '<literal>', work_item_id: wi, tx: [] }] };
  routes.set('/api/channel?limit=120', ok({ threads: [thread], lineage: { unresolved_threads: 1 } }));
  routes.set('/api/channel?limit=120&fleet=team', routes.get('/api/channel?limit=120'));
  routes.set('/api/tasks', ok({ tasks: [], issues: [], issue_count: 0, attention_count: 0, truncated: false, limit: 200 }));
  routes.set(`/api/tasks/${wi}?fleet=team`, ok({ task: { task_id: wi, fleet: 'team', fleet_uid: fleet, title: 'outside board', body: 'literal', current_assignment: null, history: [] } }));
  routes.set('/api/overview', ok({ fleets: [], host: { rows: 12, daemon_serving: true, spool_files: 0 }, totals: { fleets: 1, bots: 1, provisional: 0, working: 0, attention: 0, overdue: 0, live_poll: 'ok', recorder_gaps: [] } }));
  routes.set('/api/summary', ok({ daemon_serving: true }));
  class Stream { constructor(url) { this.url = url; this.listeners = {}; streams.push(this); } addEventListener(k, cb) { this.listeners[k] = cb; } close() { this.closed = true; } }
  let status = 'ready';
  const reader = createOwnerReadHandle({ EventSource: Stream, location: { replace() { throw new Error('Global redirect forbidden'); } }, async fetch(url, opts) {
    calls.push({ url, opts });
    let data = url === '/api/owner/status' ? { state: status, ...(status === 'ready' ? { read_profile: { version: 1, profile: 'direct-owner-read-v1', host_uid: host } } : {}) } : routes.get(url);
    if (data instanceof Promise) data = await data;
    return { status: 200, json: async () => data };
  } });
  await reader.start();
  return { key, label: key, reader, host, calls, streams, routes, setStatus(value) { status = value; } };
}
async function pair() { const a = await source('a', 'a'), b = await source('b', 'b'); const api = createTwoSourceReadTransport({ sources: [a, b] }); await api.jget('/api/fleets'); return { a, b, api }; }

test('only core admitted read handles; distinct hosts; no action or mutation capability', async () => {
  assert.throws(() => createTwoSourceReadTransport({ sources: [{ key: 'a', label: 'a', reader: { snapshot: () => ({ state: 'ready' }) } }, {}] }));
  const a = await source('a','a'), b = await source('b','a');
  assert.throws(() => createTwoSourceReadTransport({ sources: [a,b] }), /independently/);
  const { api, a: x } = await pair();
  assert.deepEqual(Object.keys(api).sort(), ['createEventSource','dispose','jget']);
  for (const path of ['/api/owner/actions/send','/api/tasks?fleet=unregistered','/api/channel?limit=999','/api/tasks?fleet=a%20%2F%20team&fleet=b','https://wrong/api/tasks','/api/inventory']) assert.notEqual((await api.jget(path)).state, 'ok');
  assert.ok(x.calls.every(c => !c.opts.method || c.opts.method === 'GET'));
});
test('colliding identities stay distinct; all and selected activity preserve canonical link and literal body', async () => {
  const { api, a, b } = await pair();
  const all = await api.jget('/api/channel?limit=120');
  assert.deepEqual(all.data.threads.map(t => t.work_item_id), ['a::'+wi,'b::'+wi]);
  assert.equal(all.data.threads[0].task_link.host_uid, 'a::'+a.host);
  assert.equal(all.data.threads[0].messages[0].body, '<literal>');
  assert.equal(all.data.lineage.unresolved_threads, 2);
  const selected = await api.jget('/api/channel?limit=120&fleet=a%20%2F%20team');
  assert.equal(selected.data.threads.length, 1);
  assert.equal(b.calls.filter(c => c.url.includes('&fleet=')).length, 0);
  a.routes.get('/api/channel?limit=120').data.threads[0].task_link.host_uid = b.host;
  assert.equal((await api.jget('/api/channel?limit=120')).data.threads[0].task_link, null);
});
test('outside-board detail routes by exact source/team and refuses mismatched ownership', async () => {
  const { api, a, b } = await pair();
  const url = `/api/tasks/${encodeURIComponent('a::'+wi)}?fleet=a%20%2F%20team`;
  const detail = await api.jget(url);
  assert.equal(detail.data.task.task_id, 'a::'+wi);
  assert.equal(detail.data.task.fleet_uid, 'a::'+fleet);
  assert.equal(b.calls.filter(c => c.url.startsWith('/api/tasks/')).length, 0);
  assert.equal((await api.jget(url.replace('a%20','b%20'))).state, 'invalid');
  a.routes.get(`/api/tasks/${wi}?fleet=team`).data.task.fleet_uid = 'fleet_'+'f'.repeat(32);
  assert.equal((await api.jget(url)).state, 'denied');
});
test('source refusal fences held detail without clearing other source, keeps admitted fleet labels and honest windows', async () => {
  const { api, a } = await pair();
  const hold = deferred(); a.routes.set(`/api/tasks/${wi}?fleet=team`, hold.promise);
  const pending = api.jget(`/api/tasks/${encodeURIComponent('a::'+wi)}?fleet=a%20%2F%20team`);
  await new Promise(r => setImmediate(r));
  a.setStatus('sign_in_required'); await a.reader.start();
  hold.resolve(ok({ task: { task_id: wi, fleet: 'team', fleet_uid: fleet, body: 'stale private body' } }));
  assert.notEqual((await pending).state, 'ok');
  const overview = await api.jget('/api/overview');
  assert.deepEqual(overview.data.coverage, { total: 2, reachable: 1, partial: true });
  assert.equal(overview.data.sources[0].host, null);
  assert.equal(overview.data.totals.bots, 1);
  assert.equal((await api.jget('/api/fleets')).data.fleets.length, 2);
  const tasks = await api.jget('/api/tasks'); assert.equal(tasks.data.windows[0].state, 'denied');
});
test('independent native streams preserve local cursors and source errors become scoped refresh, not global source events', async () => {
  const { api, a, b } = await pair(); const events = []; let globalLoss = 0;
  const stream = api.createEventSource('/api/stream'); stream.onmessage = e => events.push(JSON.parse(e.data)); stream.addEventListener('source', () => globalLoss++);
  a.streams[0].onmessage({ data: JSON.stringify({ cursor: 9, rows: [{ ingest_seq: 9 }] }) });
  b.streams[0].onmessage({ data: JSON.stringify({ cursor: 2, rows: [{ ingest_seq: 2 }] }) });
  assert.deepEqual(events.map(e => [e.source,e.cursor]), [['a',9],['b',2]]);
  a.streams[0].listeners.source({ data: JSON.stringify({ state: 'unreadable' }) });
  assert.equal(globalLoss, 0); assert.equal(events.at(-1).coverage_changed, true);
  stream.close(); assert.ok(a.streams[0].closed && b.streams[0].closed);
});

test('source window truncation and assignment/actor references remain source scoped', async () => {
  const { api, a, b } = await pair();
  const asg = 'asg_'+'5'.repeat(32);
  a.routes.set('/api/tasks', ok({ tasks: [{ task_id: wi, fleet_uid: fleet, fleet: 'team', current_assignment: { assignment_id: asg, assignee_uid: actor, assignee_alias: 'team/worker', assignee_short: actor, dispatch_message_id: 'msg_'+'6'.repeat(32), terminal_event: { actor_uid: actor, assignment_id: asg } }, delivery: { message_id: 'msg_'+'6'.repeat(32) } }], issues: [], issue_count: 0, attention_count: 0, truncated: true, limit: 200 }));
  const result = await api.jget('/api/tasks');
  assert.equal(result.data.truncated, true);
  assert.equal(result.data.windows.length, 2);
  const t = result.data.tasks[0];
  assert.equal(t.current_assignment.assignee_uid, 'a::'+actor);
  assert.equal(t.current_assignment.assignee_short, t.current_assignment.assignee_uid);
  assert.equal(t.current_assignment.terminal_event.assignment_id, 'a::'+asg);
  assert.equal(t.delivery.message_id, t.current_assignment.dispatch_message_id);
  b.routes.set('/api/identities', ok({ identities: [{ uid: actor, alias: 'team/worker', short: actor, fleet: 'team' }] }));
  a.routes.set('/api/identities', ok({ identities: [{ uid: actor, alias: 'human', short: 'Human', fleet: null }] }));
  const ids = (await api.jget('/api/identities')).data.identities;
  assert.equal(ids[0].fleet, null); assert.notEqual(ids[0].uid, ids[1].uid);
});

test('renderer shows per-host availability and partial totals without inventing recorder failure', async () => {
  const { runInNewContext } = await import('node:vm');
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const nodes = new Map(['fleet-totals','host-facts','beat','beat-label','overview'].map(k => [k, {innerHTML:'',textContent:'',className:''}]));
  const context = { $: id => nodes.get(id), fleets: [], currentFleet: null,
    esc: v => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
    ago: () => 'recently', stateBlock: () => 'unavailable' };
  runInNewContext(app.slice(app.indexOf('function renderHeader('), app.indexOf('function toggleMessageBody(')), context);
  const env = ok({ coverage: {total:2,reachable:1,partial:true}, fleets: [], totals:{fleets:1,bots:2,working:1,attention:0,overdue:0,live_poll:'ok'},
    sources:[{key:'a',label:'<Host A>',state:'denied',host:null,remediation:'Sign in to this source.'},{key:'b',label:'Host B',state:'ok',host:{rows:12,spool_files:0,daemon_serving:true}}] });
  context.renderOverview(env); context.renderHeader(env); context.renderHostFacts(env); context.renderSummary(env);
  assert.match(nodes.get('overview').innerHTML, /&lt;Host A&gt;/);
  assert.match(nodes.get('overview').innerHTML, /Sign in to this source/);
  assert.doesNotMatch(nodes.get('overview').innerHTML, /recorder DOWN/);
  assert.match(nodes.get('fleet-totals').innerHTML, /totals cover readable sources/);
  assert.match(nodes.get('beat-label').textContent, /1\/2 sources readable/);
  assert.match(nodes.get('host-facts').textContent, /<Host A>: denied/);
  env.state='unavailable'; env.data.fleets=[]; env.data.sources[1].state='unavailable'; env.data.sources[1].host=null;
  context.renderOverview(env); assert.match(nodes.get('overview').innerHTML, /Host B/);
});

test('initially unavailable source does not hide usable host; later admission recovers without invented profile', async () => {
  const a = await source('a','a'), b = await source('b','b');
  b.setStatus('sign_in_required'); await b.reader.start();
  const api = createTwoSourceReadTransport({sources:[a,b]});
  const first = await api.jget('/api/fleets');
  assert.deepEqual(first.data.coverage, {total:2,reachable:1,partial:true});
  assert.equal(first.data.fleets.length,1); assert.equal(first.data.sources[1].host_uid,null);
  assert.equal(b.calls.filter(c => c.url === '/api/fleets').length,0);
  b.setStatus('ready'); await b.reader.start();
  const next = await api.jget('/api/fleets');
  assert.equal(next.data.fleets.length,2); assert.equal(next.data.coverage.partial,false);
});

test('later duplicate-host admission is refused locally while original source stays readable', async () => {
  const a = await source('a','a'), b = await source('b','a');
  b.setStatus('sign_in_required'); await b.reader.start();
  const api = createTwoSourceReadTransport({sources:[a,b]});
  b.setStatus('ready'); await b.reader.start();
  const result = await api.jget('/api/fleets');
  assert.equal(result.data.coverage.reachable,1);
  assert.equal(b.calls.filter(c => c.url === '/api/fleets').length,0);
  assert.equal(result.data.fleets[0].alias,'a / team');
});

test('repeated unavailable source reads do not create an automatic refresh loop', async () => {
  const { api, a } = await pair();
  const facade = api.createEventSource('/api/stream'); let refreshes = 0;
  facade.onmessage = ev => { if (JSON.parse(ev.data).coverage_changed) refreshes++; };
  a.routes.set('/api/overview', {state:'unreadable',provenance:null});
  await api.jget('/api/overview'); await api.jget('/api/overview');
  assert.equal(refreshes,1); facade.close();
});

test('malformed projection is scoped to its source and cannot hide the other host', async () => {
  const { api, a } = await pair(); a.routes.set('/api/channel?limit=120', ok({threads:null}));
  const result = await api.jget('/api/channel?limit=120');
  assert.equal(result.state,'ok'); assert.equal(result.data.coverage.reachable,1);
  assert.equal(result.data.threads[0].work_item_id,'b::'+wi);
});
