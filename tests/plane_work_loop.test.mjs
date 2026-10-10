import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { ActionState } from '../claudlobby/plane/ui/action-state.js';

// Exercise the real controller without a browser dependency. This small DOM
// double models only the IDs, delegated buttons, focus and dialog close events
// used here. Browser rendering and native keyboard behavior remain browser QA.
const source = await readFile(new URL('../claudlobby/plane/ui/work-loop.js', import.meta.url), 'utf8');
const controller = source.replaceAll('from "/action-state.js"', `from "${new URL('../claudlobby/plane/ui/action-state.js', import.meta.url)}"`)
  .replaceAll('from "/panel-state.js"', `from "${new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url)}"`);
const { mountWorkLoop } = await import(`data:text/javascript;base64,${Buffer.from(controller).toString('base64')}`);
const transportSource = await readFile(new URL('../claudlobby/plane/ui/owner-api-client.js', import.meta.url), 'utf8');
const { createOwnerTransport } = await import(`data:text/javascript;base64,${Buffer.from(transportSource).toString('base64')}`);
const context = { version: 1, room: 'web', simulation: true,
  scope: { workspace: 'example', host: 'workshop', fleet: 'web', viewer: 'owner' },
  recipients: [{ id: 'lead', label: 'Lead', lead: true }, { id: 'worker', label: 'Worker' }],
  actions: ['message', 'feedback', 'nudge'] };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
const receipt = (request, status = 'delivered') => { const {body,...metadata}=request; return request.version === 2 ? {...metadata,status} : {...request,version:1,status}; };
function store() { const values = new Map(); return { getItem: k => values.get(k) || null, setItem: (k, v) => values.set(k, v) }; }
function dom() {
  const elements = new Map();
  const document = { activeElement: null, selection: null, getSelection() { return this.selection; }, getElementById: id => elements.get(id) };
  class Element {
    constructor(id = '', dataset = {}) {
      this.id = id; this.dataset = dataset; this.isConnected = true;
      this.hidden = false; this.disabled = false; this.value = ''; this.children = []; this.listeners = new Map();
      this.className = ''; this.tagName = ''; this.open = false;
      if (id) elements.set(id, this);
    }
    set innerHTML(html) {
      this.html = html; this.children = [];
      for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
        const attributes = match[2], id = attributes.match(/\bid="([^"]+)"/)?.[1] || '';
        const dataset = {};
        for (const data of attributes.matchAll(/data-([\w-]+)="([^"]*)"/g))
          dataset[data[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = data[2];
        if (id || Object.keys(dataset).length) {
          const child = new Element(id, dataset); child.hidden = /\bhidden\b/.test(attributes); child.disabled = /\bdisabled\b/.test(attributes); this.children.push(child);
        }
      }
    }
    get innerHTML() { return this.html || ''; }
    querySelectorAll(selector) {
      const key = selector.match(/^\[data-([\w-]+)\]$/)?.[1]?.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      const matches = child => key ? key in child.dataset : selector === 'details' ? child.tagName === 'DETAILS'
        : selector === '.msg[data-msg-id]' ? child.className === 'msg' && 'msgId' in child.dataset : false;
      const descendants = children => children.flatMap(child => [child, ...descendants(child.children)]);
      return descendants(this.children).filter(child => child.isConnected && matches(child));
    }
    contains(element) { return !!element && (element === this || this.children.some(child => child.contains(element))); }
    replaceChildren(...children) { this.html = ''; this.children = children; }
    closest(selector) {
      if (selector === '#attention, #tasks') return this.panel || null;
      return this.querySelectorAll.call({ children: [this] }, selector)[0] || null;
    }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    emit(name, event = {}) { return this.listeners.get(name)?.(event); }
    append(element) { this.children.push(element); }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; queueMicrotask(() => this.emit('close')); }
  }
  for (const id of ['work-loop', 'task-detail', 'task-detail-content', 'task-detail-close', 'task-detail-refresh', 'rail-right', 'attention', 'tasks']) new Element(id);
  return { document, Element, get: id => elements.get(id) };
}
function harness(options = {}) {
  const ui = dom(), storage = options.storage || store();
  Object.defineProperty(globalThis, 'document', { configurable: true, value: ui.document });
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage });
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: options.crypto === undefined ? { randomUUID: () => 'new-request' } : options.crypto });
  const confirmations = [];
  globalThis.confirm = text => { confirmations.push(text); return options.confirm !== false; };
  const sends = [], lookups = [], preparations = [];
  const api = {
    interactionContext: options.interactionContext || (() => context),
    ...(options.feedbackContext ? {feedbackContext:options.feedbackContext} : {}),
    ...(options.nudgeContext || options.feedbackContext ? {...(options.nudgeContext ? {nudgeContext:options.nudgeContext} : {}),prepareAction:request=>{preparations.push(request);if(options.prepareAction)return options.prepareAction(request);const {body,...metadata}=request;return {...metadata,semantic_sha256:'d'.repeat(64)};}} : {}),
    ...(options.jget ? {jget:options.jget} : {}),
    ...(options.protected ? {mountSessionControls(){}} : {}),
    sendAction: request => { sends.push(request); return options.sendAction ? options.sendAction(request) : receipt(request); },
    actionReceipt: request => { lookups.push(request); return options.actionReceipt ? options.actionReceipt(request) : receipt(request); },
  };
  if (options.transport) Object.assign(api, options.transport);
  const loop = mountWorkLoop({ api, renderThread: options.renderThread ? thread => options.renderThread(thread, ui) : () => new ui.Element(), refresh: () => {} });
  const board = { state: 'ok', data: { tasks: ['task-a', 'task-b'].map(task_id => ({ task_id, fleet: 'web', title: task_id })) } };
  const update = (messages = { state: 'ok', data: { threads: [] } }, tasks = board) => loop.update(tasks, messages);
  const open = (id = 'task-a', fleet = 'web', panel = 'tasks') => {
    const button = new ui.Element('', { taskOpen: id, taskFleet: fleet });
    button.panel = ui.get(panel); button.panel.children = [button];
    ui.get('rail-right').children = [button];
    ui.get('rail-right').emit('click', { target: button }); return button;
  };
  const pendingClick = (id, discard = false) => ui.get('work-pending').onclick({ target: new ui.Element('', { [discard ? 'discard' : 'request']: id }) });
  const submit = () => { ui.get('work-body').value = 'Hello'; ui.get('work-body').emit('input'); return ui.get('work-form').onsubmit({ preventDefault() {} }); };
  return { ...ui, loop, api, storage, sends, lookups, preparations, confirmations, update, open, pendingClick, submit };
}
function saved(storage, task = null) {
  return new ActionState(storage).begin(context, 'message', { recipient: 'lead', task_id: task }, 'Private old message', 'saved-request');
}

test('modal returns focus to the refreshed card with the same task and fleet identity', async () => {
  const h = harness(); h.loop.setRoom('web'); await settle(); h.update();
  const old = h.open(); old.isConnected = false;
  const wrongFleet = new h.Element('', { taskOpen: 'task-a', taskFleet: 'other' });
  const replacement = new h.Element('', { taskOpen: 'task-a', taskFleet: 'web' });
  h.get('rail-right').children = [wrongFleet, replacement]; h.update();
  h.get('task-detail-close').onclick(); await settle();
  assert.equal(h.document.activeElement, replacement);
});
test('modal focus falls back to another card, or the task rail if the task disappeared', async () => {
  for (const remaining of [true, false]) {
    const h = harness(); h.loop.setRoom('web'); await settle(); h.update();
    const old = h.open(); old.isConnected = false;
    const other = new h.Element('', { taskOpen: 'task-b', taskFleet: 'web' });
    h.get('rail-right').children = remaining ? [other] : [];
    h.get('task-detail').close(); await settle();
    assert.equal(h.document.activeElement, remaining ? other : h.get('rail-right'));
  }
});
test('context settling after task open offers an explicit refresh and then enables task actions', async () => {
  const access = deferred(), h = harness({ interactionContext: () => access.promise });
  h.loop.setRoom('web'); h.update(); h.open();
  assert.ok(h.get('task-detail-content').querySelectorAll('[data-kind]').every(button => button.disabled));
  access.resolve(context); await settle();
  assert.equal(h.get('task-detail-refresh').hidden, false);
  h.get('task-detail-refresh').onclick();
  assert.ok(h.get('task-detail-content').querySelectorAll('[data-kind]').every(button => !button.disabled));
});
test('late receipt names its original recipient and preserves a newly selected recipient draft', async () => {
  const storage = store(), original = saved(storage), lookup = deferred();
  const h = harness({ storage, actionReceipt: () => lookup.promise }); h.loop.setRoom('web'); await settle();
  const checking = h.pendingClick(original.request_id);
  h.get('work-recipient').value = 'worker'; h.get('work-recipient').onchange();
  h.get('work-body').value = 'Worker draft'; h.get('work-body').emit('input');
  lookup.resolve(receipt(original)); await checking;
  assert.match(h.get('work-notice').textContent, /message to Lead:.*delivery confirmed/);
  assert.equal(h.get('work-body').value, 'Worker draft');
  assert.equal(new ActionState(storage).pending.length, 0);
});
test('receipt notice names its original task even if a different task was selected before checking', async () => {
  const storage = store(), original = saved(storage, 'task-a'), h = harness({ storage });
  h.loop.setRoom('web'); await settle(); h.update(); h.open('task-b');
  h.get('task-detail-content').querySelectorAll('[data-kind]')[0].onclick(); await settle();
  await h.pendingClick(original.request_id);
  assert.match(h.get('work-notice').textContent, /message to Lead · task task-a:/);
});
test('reload and explicit discard never resend; discard requires the uncertainty confirmation', async () => {
  const storage = store(), original = saved(storage), h = harness({ storage, confirm: false });
  h.loop.setRoom('web'); await settle();
  assert.equal(h.get('work-send').disabled, true); assert.equal(h.sends.length, 0);
  await h.pendingClick(original.request_id, true);
  assert.equal(new ActionState(storage).pending.length, 1);
  assert.match(h.confirmations[0], /may already have been delivered/);
  globalThis.confirm = () => true;
  await h.pendingClick(original.request_id, true);
  assert.equal(new ActionState(storage).pending.length, 0);
  assert.equal(h.get('work-send').disabled, false);
  assert.match(h.get('work-pending').innerHTML, /saved-request/);
  assert.equal(h.sends.length, 0); assert.equal(h.lookups.length, 0);
});
test('corrupt saved records clear only after confirmation, with no transport calls', async () => {
  const storage = store(); storage.setItem('plane.pending-actions.v1', '{');
  const h = harness({ storage, confirm: false }); h.loop.setRoom('web'); await settle();
  assert.equal(h.get('work-clear-saved').hidden, false);
  h.get('work-clear-saved').onclick(); assert.equal(storage.getItem('plane.pending-actions.v1'), '{');
  assert.match(h.confirmations[0], /may include requests already delivered/);
  globalThis.confirm = () => true; h.get('work-clear-saved').onclick();
  assert.equal(storage.getItem('plane.pending-actions.v1'), '[]');
  assert.equal(h.get('work-clear-saved').hidden, true); assert.equal(h.get('work-send').disabled, false);
  assert.equal(h.sends.length, 0); assert.equal(h.lookups.length, 0);
});
test('missing UUID capability and storage write failure use plain nothing-sent notices', async () => {
  const failures = [{ crypto: {} }, { crypto: { randomUUID: () => { throw Error('raw UUID error'); } } },
    { storage: { getItem: () => null, setItem: () => { throw Error('raw quota error'); } } }];
  for (const options of failures) {
    const h = harness(options); h.loop.setRoom('web'); await settle(); await h.submit();
    assert.match(h.get('work-notice').textContent, /Nothing was sent/);
    assert.doesNotMatch(h.get('work-notice').textContent, /raw|randomUUID|quota/);
    assert.equal(h.sends.length, 0);
  }
});
test('late access and receipt responses cannot leak across room changes', async () => {
  const access = deferred(), h = harness({ interactionContext: room => room === 'web' ? access.promise : null });
  h.loop.setRoom('web'); await settle(); h.loop.setRoom('other'); access.resolve(context); await settle();
  assert.equal(h.get('work-form').hidden, true); assert.equal(h.get('work-scope').textContent, 'other');
  const storage = store(), original = saved(storage), lookup = deferred();
  const h2 = harness({ storage, actionReceipt: () => lookup.promise }); h2.loop.setRoom('web'); await settle();
  const checking = h2.pendingClick(original.request_id); h2.loop.setRoom('other'); await settle();
  lookup.resolve(receipt(original)); await checking;
  assert.equal(h2.get('work-form').hidden, true);
  assert.doesNotMatch(h2.get('work-notice').textContent, /delivery confirmed/);
  assert.equal(h2.get('work-pending').innerHTML, '');
});
test('one pending send attempts transport once and its late result preserves another recipient draft', async () => {
  const sending = deferred(), h = harness({ sendAction: () => sending.promise });
  h.loop.setRoom('web'); await settle();
  const first = h.submit(); await settle(); await h.submit();
  assert.equal(h.sends.length, 1);
  h.get('work-recipient').value = 'worker'; h.get('work-recipient').onchange();
  h.get('work-body').value = 'Keep worker draft'; h.get('work-body').emit('input');
  sending.resolve(receipt(h.sends[0])); await first;
  assert.equal(h.get('work-body').value, 'Keep worker draft');
  assert.match(h.get('work-notice').textContent, /message to Lead:.*delivery confirmed/);
});

test('modal focus prefers the original panel when refreshed cards duplicate task and fleet', async () => {
  const h = harness(); h.loop.setRoom('web'); await settle(); h.update();
  const old = h.open('task-a', 'web', 'tasks'); old.isConnected = false;
  const attention = new h.Element('', {taskOpen:'task-a',taskFleet:'web'});
  const task = new h.Element('', {taskOpen:'task-a',taskFleet:'web'});
  h.get('attention').children = [attention]; h.get('tasks').children = [task];
  h.get('rail-right').children = [attention, task]; h.update();
  h.get('task-detail-close').onclick(); await settle();
  assert.equal(h.document.activeElement, task);
});
test('in-flight sends cannot discard their saved ID until recorded or failed transport settles', async () => {
  for (const outcome of ['recorded', 'failed']) {
    const transport = deferred(), h = harness({sendAction:()=>transport.promise});
    h.loop.setRoom('web'); await settle(); const sending = h.submit(); await settle();
    const discard = () => h.get('work-pending').querySelectorAll('[data-discard]')[0];
    assert.equal(discard().disabled, true);
    await h.pendingClick(h.sends[0].request_id, true);
    assert.equal(h.confirmations.length, 0);
    assert.equal(new ActionState(h.storage).pending.length, 1);
    if (outcome === 'recorded') transport.resolve(receipt(h.sends[0], 'recorded'));
    else transport.reject(Error('lost response'));
    await sending;
    assert.equal(discard().disabled, false);
    await h.pendingClick(h.sends[0].request_id, true);
    assert.equal(h.confirmations.length, 1);
    assert.equal(new ActionState(h.storage).pending.length, 0);
    assert.equal(h.sends.length, 1);
  }
});
test('readable IDs in a damaged saved array are visible before confirmed clearing', async () => {
  for (const extra of ['invalid', 'oversized']) {
    const storage = store(), original = saved(storage);
    const rows = extra === 'invalid' ? [original, {request_id:'broken'}]
      : Array.from({length:21}, (_,i)=>({...original, request_id:`readable-${i}`}));
    const raw = JSON.stringify(rows); storage.setItem('plane.pending-actions.v1', raw);
    const h = harness({storage,confirm:false}); h.loop.setRoom('web'); await settle();
    const id = extra === 'invalid' ? 'saved-request' : 'readable-20';
    assert.ok(h.get('work-recoverable').innerHTML.includes(id));
    assert.equal(h.get('work-send').disabled, true);
    h.get('work-clear-saved').onclick();
    assert.equal(storage.getItem('plane.pending-actions.v1'), raw);
    assert.match(h.confirmations[0], /Copy any readable IDs/);
    globalThis.confirm = () => true; h.get('work-clear-saved').onclick();
    assert.equal(h.get('work-recoverable').innerHTML, '');
    assert.equal(h.sends.length, 0);
  }
});
test('late refusal from send and receipt lookup remains visible against its original task', async () => {
  for (const operation of ['send', 'lookup']) {
    const transport = deferred(), storage = store();
    const original = operation === 'lookup' ? saved(storage, 'task-a') : null;
    const h = harness({storage, sendAction:()=>transport.promise, actionReceipt:()=>transport.promise});
    h.loop.setRoom('web'); await settle();
    const pending = operation === 'lookup' ? h.pendingClick(original.request_id) : h.submit();
    h.get('work-recipient').value = 'worker'; h.get('work-recipient').onchange();
    h.get('work-body').value = 'Keep the new draft'; h.get('work-body').emit('input');
    transport.resolve(receipt(original || h.sends[0], 'rejected')); await pending;
    assert.match(h.get('work-notice').textContent, /message to Lead.*request refused/);
    if (original) assert.match(h.get('work-notice').textContent, /task task-a/);
    assert.equal(h.get('work-body').value, 'Keep the new draft');
    assert.equal(new ActionState(storage).pending.length, 0);
  }
});

test('receipt checks wait for their in-flight send; failed sends retain the original ID for recovery', async () => {
  const transport = deferred(), h = harness({sendAction:()=>transport.promise});
  h.loop.setRoom('web'); await settle();
  const sending = h.submit(); await settle();
  const request = h.sends[0];
  const receiptButton = () => h.get('work-pending').querySelectorAll('[data-request]')[0];
  assert.equal(receiptButton().disabled, true);
  await h.pendingClick(request.request_id);
  assert.equal(h.lookups.length, 0);
  assert.equal(new ActionState(h.storage).pending[0].request_id, request.request_id);
  transport.reject(Error('lost send response')); await sending;
  assert.equal(receiptButton().disabled, false);
  assert.match(h.get('work-notice').textContent, /Outcome unknown/);
  assert.equal(new ActionState(h.storage).pending[0].request_id, request.request_id);
  await h.pendingClick(request.request_id);
  assert.equal(h.lookups.length, 1);
  assert.equal(h.lookups[0].request_id, request.request_id);
  assert.match(h.get('work-notice').textContent, /message to Lead:.*delivery confirmed/);
  assert.equal(new ActionState(h.storage).pending.length, 0);
  assert.equal(h.sends.length, 1);
});


const canonicalDetail = id => ({state:'ok',data:{task:{task_id:id,fleet:'web',title:'Full selected task',body:'<body>\nFull description',
  state:'completed',resolved:true,current_assignment:null,assignments:[],history:[{event:'completed',occurred_at:'2026-10-10T00:00:00Z',detail:'<recorded detail>'}],
  history_window:{shown:1,total:501,truncated:true},assignments_window:{shown:0,total:0,truncated:false},issues:[]}}});

test('selected canonical detail loads independently of capped board and escapes full body/history', async () => {
  const calls=[];
  const h=harness({jget(url){calls.push(url);return canonicalDetail('task-off-board');}});
  h.loop.setRoom('web');await settle();h.update();h.open('task-off-board');await settle();
  assert.deepEqual(calls,['/api/tasks/task-off-board?fleet=web']);
  assert.match(h.get('task-detail-content').innerHTML,/Full selected task/);
  assert.match(h.get('task-detail-content').innerHTML,/&lt;body&gt;/);
  assert.match(h.get('task-detail-content').innerHTML,/most recent of 501/);
  assert.match(h.get('task-detail-content').innerHTML,/recent channel window/);
  assert.doesNotMatch(h.get('task-detail-content').innerHTML,/<body>/);
});

test('late detail cannot replace another selected task or reopen a closed dialog', async () => {
  const first=deferred();
  const h=harness({jget(url){return url.includes('task-a')?first.promise:canonicalDetail('task-b');}});
  h.loop.setRoom('web');await settle();h.update();h.open('task-a');await settle();h.open('task-b');await settle();
  first.resolve(canonicalDetail('task-a'));await settle();
  assert.match(h.get('task-detail-content').innerHTML,/task-b/);
  assert.doesNotMatch(h.get('task-detail-content').innerHTML,/· task-a<\/p>/);
  const late=deferred();const closed=harness({jget(){return late.promise;}});
  closed.loop.setRoom('web');await settle();closed.open();await settle();closed.get('task-detail-close').onclick();await settle();
  late.resolve(canonicalDetail('task-a'));await settle();
  assert.equal(closed.get('task-detail').open,false);
  assert.doesNotMatch(closed.get('task-detail-content').innerHTML,/Full selected task/);
});

test('detail remains explicit snapshot until refresh and rejects a mismatched task', async () => {
  let calls=0;const h=harness({jget(){calls++;return canonicalDetail(calls===1?'task-a':'wrong-task');}});
  h.loop.setRoom('web');await settle();h.open();await settle();h.update();
  assert.equal(calls,1);assert.equal(h.get('task-detail-refresh').hidden,false);
  h.get('task-detail-refresh').onclick();await settle();
  assert.equal(calls,2);assert.match(h.get('task-detail-content').innerHTML,/unknown/);
  assert.doesNotMatch(h.get('task-detail-content').innerHTML,/Full selected task/);
});

test('unsupported synthetic detail retains labelled limited board snapshot; protected refusals do not', async () => {
  for(const guarded of [false,true]) {
    const h=harness({protected:guarded,jget(){return {state:'unavailable',remediation:'Unsupported detail route'};}});
    h.loop.setRoom('web');await settle();h.update();h.open();await settle();
    assert.match(h.get('task-detail-content').innerHTML,/unavailable/);
    if(guarded) assert.doesNotMatch(h.get('task-detail-content').innerHTML,/board snapshot/);
    else assert.match(h.get('task-detail-content').innerHTML,/limited board snapshot/);
  }
});

test('production scoped action refusal preserves readable details and disables only its capability', async () => {
  for (const field of ['workspace', 'host', 'fleet', 'viewer']) {
    const result = deferred();
    const h = harness({ jget() { return result.promise; } });
    h.loop.setRoom('web'); await settle(); h.open(); await settle();
    h.loop.invalidate(undefined, { ...context.scope, [field]: 'previous-scope' }, 'message', context.recipients[0].id);
    await settle();
    assert.equal(h.get('task-detail').open, true);
    assert.equal(h.get('work-form').hidden, false);
    result.resolve(canonicalDetail('task-a')); await settle();
    assert.match(h.get('task-detail-content').innerHTML, /Full selected task/);
  }
  const result = deferred();
  const h = harness({ jget() { return result.promise; } });
  h.loop.setRoom('web'); await settle(); h.open(); await settle();
  h.loop.invalidate(undefined, context.scope, 'message', context.recipients[0].id); await settle();
  assert.equal(h.get('task-detail').open, true);
  assert.equal(h.get('work-form').hidden, true);
  result.resolve(canonicalDetail('task-a')); await settle();
  assert.match(h.get('task-detail-content').innerHTML, /Full selected task/);
});

test('fresh submission refusal removes only owned pending row and keeps draft', async () => {
  const storage=store(); saved(storage);
  const refusal=Object.assign(Error('Refused'),{effect:'not_started'});
  const h=harness({storage,sendAction(){throw refusal;}});
  h.loop.setRoom('web'); await settle();
  h.get('work-recipient').value='worker'; h.get('work-recipient').onchange();
  await h.submit();
  const rows=JSON.parse(storage.getItem('plane.pending-actions.v1'));
  assert.deepEqual(rows.map(r=>r.request_id),['saved-request']);
  assert.equal(h.get('work-body').value,'Hello');
  assert.match(h.get('work-notice').textContent,/This submission was refused before delivery/);
  assert.equal(h.sends.length,1);
});

test('reused local pending UUID cannot start a send or clear its earlier row', async () => {
  const storage=store(); saved(storage);
  const h=harness({storage,crypto:{randomUUID:()=> 'saved-request'}});
  h.loop.setRoom('web'); await settle();
  h.get('work-recipient').value='worker'; h.get('work-recipient').onchange();
  await h.submit();
  assert.equal(h.sends.length,0);
  assert.equal(JSON.parse(storage.getItem('plane.pending-actions.v1'))[0].request_id,'saved-request');
});

test('paused action context preserves selected team and does not fetch grants', async () => {
  const rooms=[]; const h=harness({interactionContext(room){rooms.push(room);return context;}});
  h.loop.setRoom('web'); await settle();
  h.get('work-body').value='Kept';h.get('work-body').emit('input');
  h.loop.pause(); await settle();
  assert.deepEqual(rooms,['web']);
  assert.equal(h.get('work-scope').textContent,'web');
  assert.match(h.get('work-notice').textContent,/Session access is paused/);
  assert.equal(h.get('work-send').disabled,true);
  h.loop.setRoom('web');await settle();
  assert.equal(h.get('work-body').value,'Kept');
});


test('capability invalidation hides composer without hiding the refused submission notice', async () => {
  let h;
  const rooms=[];
  h=harness({interactionContext(room){rooms.push(room);return context;},sendAction(){
    h.loop.invalidate();
    throw Object.assign(Error('Refused'),{effect:'not_started'});
  }});
  h.loop.setRoom('web');await settle();await h.submit();
  assert.equal(h.get('work-form').hidden,true);
  assert.equal(h.get('work-send').disabled,true);
  assert.match(h.get('work-notice').textContent,/This submission was refused before delivery/);
  assert.deepEqual(rooms,['web']);
  assert.deepEqual(JSON.parse(h.storage.getItem('plane.pending-actions.v1')),[]);
});

test('refusal cleanup storage failure keeps original row and is handled without resend', async () => {
  const storage=store();
  const h=harness({storage,sendAction(){
    storage.setItem=()=>{throw Error('storage unavailable');};
    throw Object.assign(Error('Refused'),{effect:'not_started'});
  }});
  h.loop.setRoom('web');await settle();await h.submit();
  assert.equal(h.sends.length,1);
  assert.equal(JSON.parse(storage.getItem('plane.pending-actions.v1'))[0].request_id,'new-request');
  assert.match(h.get('work-notice').textContent,/saved row could not be updated/);
  assert.equal(h.get('work-send').disabled,true);
});


test('canonical attention question replaces stale board question and stays escaped', async () => {
  const value = canonicalDetail('task-a');
  value.data.task.attention_question = 'Fresh <question>\nChoose the next step.';
  value.data.task.attention_reason = ['escalated'];
  const h = harness({jget(){return value;}}); h.loop.setRoom('web'); await settle();
  h.loop.update({state:'ok',data:{tasks:[{task_id:'task-a',fleet:'web',attention_question:'Stale board question'}]}}, null);
  h.open(); await settle();
  const html = h.get('task-detail-content').innerHTML;
  assert.match(html,/Needs your input/); assert.match(html,/Fresh &lt;question&gt;/);
  assert.doesNotMatch(html,/Stale board question|Fresh <question>/);
});

test('history shows known prose and retains exact escaped JSON in closed disclosures', async () => {
  const value = canonicalDetail('task-a');
  const raw = JSON.stringify({summary:'Known <summary>\nSecond line',reason:'Known <reason>',question:'Known <question>',result:'Arbitrary result must stay raw'});
  value.data.task.history[0] = {...value.data.task.history[0],detail:raw,actor_alias:'bot:web/one',actor_short:'one',actor_uid:'actor-record-id',event_id:'event-record-id'};
  value.data.task.assignments = [{assignment_id:'assignment-record-id',state:'closed',assignee_alias:'bot:web/one',assignee_short:'one',assigned_by_alias:'bot:web/lead',assigned_by_short:'lead',history:[]}];
  const h = harness({jget(){return value;}}); h.loop.setRoom('web'); await settle(); h.open(); await settle();
  const html = h.get('task-detail-content').innerHTML;
  const primary = html.slice(html.indexOf('<h3>Task history</h3>'),html.indexOf('<summary>Recorded event details</summary>'));
  assert.match(primary,/Known &lt;summary&gt;\nSecond line/); assert.match(primary,/Known &lt;reason&gt;/);
  assert.match(primary,/Known &lt;question&gt;/); assert.doesNotMatch(primary,/Arbitrary result|actor-record-id|event-record-id/);
  assert.match(html,/&quot;result&quot;:&quot;Arbitrary result must stay raw&quot;/);
  assert.doesNotMatch(html,/<summary>[^<]*assignment-record-id|class="task-state"[^>]*>[^]*?task-a<\/p>/);
  assert.match(html,/<summary>one · closed<\/summary>/);
  assert.match(html,/<summary>Record identifiers<\/summary>/);
  assert.doesNotMatch(html,/<details[^>]*\bopen\b|<summary>Known/);
});

for (const raw of ['malformed <json>', '["<array>"]', 'null', '"<string>"'])
test(`malformed or non-object recorded detail stays raw without inferred prose: ${raw}`, async () => {
  const value = canonicalDetail('task-a'); value.data.task.history[0].detail = raw;
  const h = harness({jget(){return value;}}); h.loop.setRoom('web'); await settle(); h.open(); await settle();
  const html = h.get('task-detail-content').innerHTML;
  assert.match(html,/<summary>Recorded event details<\/summary>/);
  assert.doesNotMatch(html,/<p class="note">(?:Summary|Reason|Question)<\/p>|<json>|<array>|<string>/);
});

for (const close of ['button','escape','pause','source-loss'])
test(`closing private task detail erases its body/history and preserves refocus: ${close}`, async () => {
  const h = harness({jget(){return canonicalDetail('task-a');}}); h.loop.setRoom('web'); await settle();
  const opener = h.open(); await settle(); assert.match(h.get('task-detail-content').innerHTML,/Full description/);
  if(close === 'button') h.get('task-detail-close').onclick();
  else if(close === 'escape') h.get('task-detail').close();
  else if(close === 'pause') h.loop.pause();
  else h.loop.invalidate();
  await settle(); assert.equal(h.get('task-detail-content').innerHTML,'');
  assert.equal(h.document.activeElement,opener); assert.equal(h.get('task-detail').open,false);
});

test('unknown recorded team keeps only labelled readonly board snapshot, never protected fallback', async () => {
  for(const guarded of [false,true]) {
    let reads = 0;
    const h = harness({protected:guarded,jget(){reads++;return null;}}); h.loop.setRoom('web'); await settle();
    h.loop.update({state:'ok',data:{tasks:[{task_id:'task-a',resolved:false,issues:[{code:'unresolved_task'}]}]}},null);
    h.open('task-a',''); await settle();
    const html = h.get('task-detail-content').innerHTML;
    assert.equal(reads,0);
    if(guarded) { assert.match(html,/cannot be authorized/); assert.doesNotMatch(html,/board snapshot|Task history has unresolved/); }
    else { assert.match(html,/limited board snapshot/); assert.match(html,/Team not recorded|unresolved links/); }
  }
});

for (const actions of [[],['message'],['feedback'],['nudge'],['feedback','nudge'],['message','feedback','nudge']])
test(`task action note describes actual capabilities only: ${actions.join(',') || 'none'}`, async () => {
  const h = harness({interactionContext(){return {...context,actions};}}); h.loop.setRoom('web'); await settle(); h.update(); h.open(); await settle();
  const note = h.get('task-action-note').textContent;
  const buttons = h.get('task-detail-content').querySelectorAll('[data-kind]');
  for(const button of buttons) assert.equal(button.disabled,!actions.includes(button.dataset.kind));
  assert.equal(note.includes('send an ordinary message'),actions.includes('message'));
  if(!actions.includes('feedback') && !actions.includes('nudge')) assert.match(note,/feedback and nudges are unavailable/);
  if(actions.includes('feedback') && !actions.includes('nudge')) assert.match(note,/feedback goes.*Nudges are unavailable/);
  if(actions.includes('nudge') && !actions.includes('feedback')) assert.match(note,/nudges go.*Feedback is unavailable/);
  if(actions.includes('feedback') && actions.includes('nudge')) assert.match(note,/feedback and nudges go/);
});


const ownerNudge = {version:2,room:'web',simulation:false,scope:{...context.scope,viewer:'nudge-only-generation'},
  recipients:[{id:'actor_'+ 'a'.repeat(32),label:'Manager',lead:true}],actions:['nudge'],release_id:'r-'+ 'b'.repeat(64)};
const nudgeTaskId='wi_'+ 'c'.repeat(32), nudgeAssignment='asg_'+ 'e'.repeat(32);
const ownerDetail = (assignment=null) => ({state:'ok',data:{task:{task_id:nudgeTaskId,fleet:'web',title:'Review the selected task',
  body:'Complete canonical body',resolved:true,state:assignment===null?'queued':'active',current_assignment:assignment,
  assignments:[],history:[],issues:[]}}});
const nudgeOptions = extra => ({interactionContext:()=>null,nudgeContext:()=>ownerNudge,protected:true,jget:()=>ownerDetail(),
  crypto:{randomUUID:()=> '11111111-1111-4111-8111-111111111111'},...extra});
async function chooseNudge(h) {
  h.loop.setRoom('web');await settle();h.open(nudgeTaskId);await settle();
  const button=h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge');
  assert.equal(button.disabled,false);button.onclick();await settle();
}
for(const assignment of [null,{assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null}])
test(`real nudge-only access freezes canonical ${assignment===null?'queued-null':'assigned'} detail, prepares then saves metadata before send`,async()=>{
  const preparation=deferred(), sent=deferred();
  const h=harness(nudgeOptions({jget:()=>ownerDetail(assignment),prepareAction:()=>preparation.promise,sendAction:request=>{
    assert.equal(new ActionState(h.storage).pending[0].semantic_sha256,request.semantic_sha256);return sent.promise;
  }}));
  await chooseNudge(h);assert.equal(h.get('work-form').hidden,false);assert.match(h.get('work-label').textContent,/team lead.*not approval/);
  assert.match(h.get('work-task').textContent,/Review the selected task/);assert.equal(h.get('work-reset').hidden,true);
  const submitting=h.submit();await settle();
  assert.equal(h.preparations.length,1);assert.equal(h.sends.length,0);assert.equal(new ActionState(h.storage).pending.length,0);
  assert.deepEqual(h.preparations[0].target,{recipient:ownerNudge.recipients[0].id,task_id:nudgeTaskId,assignment_id:assignment?.assignment_id ?? null,release_id:ownerNudge.release_id});
  assert.equal(h.preparations[0].semantic_sha256,undefined);assert.equal(h.preparations[0].body,'Hello');
  await h.get('work-form').onsubmit({preventDefault(){}});assert.equal(h.preparations.length,1);
  const {body,...metadata}=h.preparations[0];preparation.resolve({...metadata,semantic_sha256:'d'.repeat(64)});await settle();
  assert.equal(h.sends.length,1);assert.ok(!h.storage.getItem('plane.pending-actions.v1').includes('Hello'));
  sent.resolve(receipt(h.sends[0]));await submitting;assert.equal(new ActionState(h.storage).pending.length,0);
});
for(const change of ['body','body-reverted','room','pause','different-task','grant'])
test(`late nudge preparation cannot send after ${change} changes`,async()=>{
  const prepare=deferred(),h=harness(nudgeOptions({prepareAction:()=>prepare.promise}));await chooseNudge(h);
  const submitting=h.submit();await settle();
  if(change.startsWith('body')) {h.get('work-body').value='Edited reason';h.get('work-body').emit('input');if(change==='body-reverted'){h.get('work-body').value='Hello';h.get('work-body').emit('input');}}
  if(change==='room')h.loop.setRoom('other');if(change==='pause')h.loop.pause();
  if(change==='different-task')h.open('wi_'+ 'f'.repeat(32));
  if(change==='grant')h.loop.invalidate(undefined,ownerNudge.scope,'nudge');
  const {body,...metadata}=h.preparations[0];prepare.resolve({...metadata,semantic_sha256:'d'.repeat(64)});await submitting;
  assert.equal(h.sends.length,0);assert.equal(new ActionState(h.storage).pending.length,0);
});
for(const [label,mutate] of [['missing assignment',task=>delete task.current_assignment],['undefined assignment',task=>{task.current_assignment=undefined;}],['terminal task',task=>{task.state='completed';}],
  ['unresolved task',task=>{task.resolved=false;}],['wrong assignment task',task=>{task.current_assignment={assignment_id:nudgeAssignment,task_id:'wi_'+ 'f'.repeat(32),state:'active',terminal_event:null};}],
  ['active without assignment',task=>{task.state='active';}],['malformed assignment',task=>{task.current_assignment={assignment_id:'invalid',task_id:nudgeTaskId,state:'active',terminal_event:null};}]])
test(`real nudge stays disabled for ${label}`,async()=>{
  const detail=ownerDetail();mutate(detail.data.task);const h=harness(nudgeOptions({jget:()=>detail}));
  h.loop.setRoom('web');await settle();h.open(nudgeTaskId);await settle();
  const button=h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge');
  assert.equal(button.disabled,true);button.onclick();assert.equal(h.get('work-form').hidden,true);assert.equal(h.preparations.length,0);
});
test('real nudge never selects from a legacy board snapshot',async()=>{
  const h=harness(nudgeOptions({jget:()=>null,protected:false}));h.loop.setRoom('web');await settle();
  h.loop.update({state:'ok',data:{tasks:[ownerDetail().data.task]}},null);h.open(nudgeTaskId);await settle();
  assert.match(h.get('task-detail-content').innerHTML,/limited board snapshot/);
  assert.equal(h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge').disabled,true);
});
test('message draft and receipt remain usable while composing nudge and after only nudge grant refusal',async()=>{
  const storage=store(),original=saved(storage),h=harness(nudgeOptions({storage,interactionContext:()=>context}));
  h.loop.setRoom('web');await settle();h.get('work-recipient').value='worker';h.get('work-recipient').onchange();
  h.get('work-body').value='Unsent worker message';h.get('work-body').emit('input');
  h.open(nudgeTaskId);await settle();h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge').onclick();await settle();
  h.get('work-body').value='Unsent nudge reason';h.get('work-body').emit('input');
  assert.match(h.get('work-pending').innerHTML,/saved-request/);await h.pendingClick(original.request_id);
  assert.equal(h.lookups.length,1);assert.equal(h.get('work-body').value,'Unsent nudge reason');
  h.open(nudgeTaskId);await settle();h.loop.invalidate(undefined,ownerNudge.scope,'nudge');
  assert.equal(h.get('task-detail').open,true);assert.match(h.get('task-detail-content').innerHTML,/Complete canonical body/);
  const buttons=h.get('task-detail-content').querySelectorAll('[data-kind]');assert.equal(buttons.find(b=>b.dataset.kind==='nudge').disabled,true);
  h.get('task-detail-close').onclick();await settle();
  // Resume with unchanged message authority restores its original recipient/body.
  h.loop.setRoom('web');await settle();assert.equal(h.get('work-recipient').value,'worker');assert.equal(h.get('work-body').value,'Unsent worker message');
});
test('message capability refusal preserves selected nudge reason and late old nudge scope cannot disable a new generation',async()=>{
  const h=harness(nudgeOptions({interactionContext:()=>context}));await chooseNudge(h);
  h.get('work-body').value='Keep nudge reason';h.get('work-body').emit('input');h.loop.invalidate(undefined,context.scope,'message');
  assert.equal(h.get('work-form').hidden,false);assert.equal(h.get('work-body').value,'Keep nudge reason');
  h.loop.invalidate(undefined,{...ownerNudge.scope,viewer:'old-generation'},'nudge');
  assert.equal(h.get('work-form').hidden,false);assert.equal(h.get('work-body').value,'Keep nudge reason');
});
test('lost nudge send reply reloads original metadata and receipt lookup ignores newer release/task state',async()=>{
  const storage=store(),h=harness(nudgeOptions({storage,sendAction:()=>Promise.reject(Error('lost reply'))}));await chooseNudge(h);await h.submit();
  const saved=new ActionState(storage).pending[0];assert.equal(saved.version,2);assert.equal(saved.target.assignment_id,null);
  assert.equal(h.sends.length,1);assert.match(h.get('work-notice').textContent,/Outcome unknown/);
  const newer={...ownerNudge,release_id:'r-'+ 'f'.repeat(64)},restored=harness(nudgeOptions({storage,nudgeContext:()=>newer,jget:()=>{throw Error('receipt must not read task');}}));
  restored.loop.setRoom('web');await settle();await restored.pendingClick(saved.request_id);
  assert.deepEqual(restored.lookups,[saved]);assert.equal(restored.preparations.length,0);assert.equal(restored.sends.length,0);
  assert.equal(new ActionState(storage).pending.length,0);
});
test('saved nudge logical task blocks a new send after assignment/release changes without retargeting recovery',async()=>{
  const storage=store(),h=harness(nudgeOptions({storage,sendAction:()=>Promise.reject(Error('lost'))}));await chooseNudge(h);await h.submit();
  const next={...ownerNudge,release_id:'r-'+ 'f'.repeat(64)},assignment={assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null};
  const restored=harness(nudgeOptions({storage,nudgeContext:()=>next,jget:()=>ownerDetail(assignment)}));await chooseNudge(restored);
  assert.equal(restored.get('work-send').disabled,true);await restored.submit();assert.equal(restored.preparations.length,0);assert.equal(restored.sends.length,0);
  assert.equal(new ActionState(storage).pending.length,1);
});


test('recorded nudge retains original UUID, says delivery unconfirmed and checks receipt without prepare/send replay',async()=>{
  const h=harness(nudgeOptions({sendAction:request=>receipt(request,'recorded')}));await chooseNudge(h);await h.submit();
  const original=new ActionState(h.storage).pending[0];assert.ok(original);assert.equal(h.sends.length,1);assert.equal(h.preparations.length,1);
  assert.match(h.get('work-notice').textContent,/task nudge recorded.*delivery to the lead is unconfirmed.*original receipt/);
  assert.equal(h.get('work-send').disabled,true);await h.pendingClick(original.request_id);
  assert.equal(h.lookups.length,1);assert.deepEqual(h.lookups[0],original);assert.equal(h.sends.length,1);assert.equal(h.preparations.length,1);
  assert.equal(new ActionState(h.storage).pending.length,0);assert.match(h.get('work-notice').textContent,/nudge received.*no task result or approval/);
});
for(const effect of ['not_started','unknown'])
test(`nudge ${effect} send result resolves only proven fresh refusal and preserves reason`,async()=>{
  const failure=Error('unavailable');if(effect==='not_started')failure.effect='not_started';
  const h=harness(nudgeOptions({sendAction:()=>Promise.reject(failure)}));await chooseNudge(h);await h.submit();
  assert.equal(h.sends.length,1);assert.equal(h.get('work-body').value,'Hello');
  assert.equal(new ActionState(h.storage).pending.length,effect==='unknown'?1:0);
  assert.match(h.get('work-notice').textContent,effect==='unknown'?/Outcome unknown/:/refused before delivery/);
});
for(const [messages,nudges] of [[false,false],[true,false],[false,true],[true,true]])
test(`independent owner capabilities enable message=${messages} and nudge=${nudges} only`,async()=>{
  const h=harness(nudgeOptions({interactionContext:()=>messages?{...context,simulation:false,actions:['message']}:null,nudgeContext:()=>nudges?ownerNudge:null}));
  h.loop.setRoom('web');await settle();assert.equal(h.get('work-form').hidden,!messages);
  h.open(nudgeTaskId);await settle();const buttons=h.get('task-detail-content').querySelectorAll('[data-kind]');
  assert.equal(buttons.find(b=>b.dataset.kind==='nudge').disabled,!nudges);assert.equal(buttons.find(b=>b.dataset.kind==='feedback').disabled,true);
  assert.equal(h.get('task-detail').open,true);
});


test('late old-manager denial preserves refreshed nudge context with unchanged scope',async()=>{
  let current=ownerNudge, contextReads=0;
  const late=deferred();
  const h=harness(nudgeOptions({nudgeContext:()=>{contextReads++;return current;},sendAction:()=>late.promise}));
  await chooseNudge(h);
  const submitting=h.submit();await settle();
  assert.equal(h.sends.length,1);
  const original=h.sends[0];
  current={...ownerNudge,recipients:[{id:'actor_'+ 'f'.repeat(32),label:'New lead',lead:true}]};
  await chooseNudge(h);h.get('work-body').value='New lead reason';h.get('work-body').emit('input');
  // The transport reports the originating recipient for every mutation refusal.
  h.loop.invalidate(undefined,original.scope,'nudge',original.target.recipient);await settle();
  assert.equal(contextReads,2); // The old manager cannot start recovery for the new capability.
  assert.equal(h.get('work-form').hidden,false);assert.equal(h.get('work-body').value,'New lead reason');
  assert.equal(h.get('work-recipient').value,current.recipients[0].id);
  late.resolve({...original,status:'unknown'});await submitting;
  assert.equal(new ActionState(h.storage).pending.length,1);assert.equal(h.sends.length,1);
  h.loop.invalidate(undefined,current.scope,'nudge',current.recipients[0].id);
  assert.equal(h.get('work-form').hidden,true);assert.equal(h.get('work-send').disabled,true);
});


async function ownerRecoveryHarness({ messages = false, recovery = 200, heldRecovery } = {}) {
  let session = 'ready', nudgeReads = 0, detailReads = 0, prepareCount = 0;
  const calls = [], redirects = [], timers = new Set();
  const newer = { ...ownerNudge, release_id: 'r-' + 'f'.repeat(64) };
  const transport = createOwnerTransport({
    EventSource: class { close() {} }, location: {replace(path) { redirects.push(path); }},
    setTimeout(fn) { timers.add(fn); return fn; }, clearTimeout(fn) { timers.delete(fn); },
    async fetch(url, options) {
      const body = options.body ? JSON.parse(options.body) : null; calls.push({url,body});
      let status = 200, data;
      if (url === '/api/owner/status') { status = session === 'ready' ? 200 : 503; data = {state:session}; }
      else if (url === '/api/owner/logout') data = {state:'signed_out'};
      else if (url === '/api/tasks?source-loss') { session='unavailable';status=503;data={state:'unavailable'}; }
      else if (url.startsWith('/api/tasks/')) {
        const assignment = ++detailReads > 1 ? {assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null} : null;
        data = ownerDetail(assignment);
      } else if (url.endsWith('/context')) {
        if (body.kind === 'nudge') {
          if (body.room === 'web' && ++nudgeReads === 2) {
            if (heldRecovery) await heldRecovery.promise;
            status = recovery; data = recovery === 200 ? newer : {state:'denied'};
          } else data = body.room === 'web' ? ownerNudge : {...newer,room:body.room,scope:{...newer.scope,fleet:body.room}};
        } else { status=messages?200:403;data=messages?{...context,simulation:false,actions:['message']}:{state:'denied'}; }
      } else if (url.endsWith('/prepare')) {
        if (++prepareCount === 1) { status=403;data={state:'denied'}; }
        else { const {body:reason,...metadata}=body;data={...metadata,semantic_sha256:'d'.repeat(64)}; }
      } else if (url.endsWith('/send')) { const {body:reason,...metadata}=body;data={...metadata,status:'delivered'}; }
      else throw Error('unexpected '+url);
      return {status,async json(){return data;}};
    },
  });
  const h = harness(nudgeOptions({transport}));
  for (const id of ['owner-session','owner-session-status','owner-session-renew','owner-session-logout','owner-session-check']) new h.Element(id);
  const controls = transport.mountSessionControls({document:h.document,element:h.get('owner-session'),
    onPause(){h.loop.pause();},onActionPause(scope,kind,recipient){h.loop.invalidate(undefined,scope,kind,recipient);}});
  await controls.ready;
  return {...h,calls,redirects,controls,get nudgeReads(){return nudgeReads;}};
}
const flushRecovery = async () => { for(let i=0;i<5;i++) await new Promise(resolve=>setImmediate(resolve)); };
for (const messages of [false,true]) for (const recovery of [200,403])
test(`real prepare403 performs one capability read without mutation replay: messages=${messages}, context=${recovery}`,async()=>{
  const h=await ownerRecoveryHarness({messages,recovery});
  try {
    h.loop.setRoom('web');await flushRecovery();
    if(messages) {
      h.get('work-recipient').value='worker';h.get('work-recipient').onchange();
      h.get('work-body').value='Ordinary worker draft';h.get('work-body').emit('input');
    }
    h.open(nudgeTaskId);await flushRecovery();
    h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge').onclick();await settle();
    h.get('work-body').value='Keep logical task reason';h.get('work-body').emit('input');
    await h.get('work-form').onsubmit({preventDefault(){}});await flushRecovery();
    assert.equal(h.nudgeReads,2);assert.equal(h.calls.filter(c=>c.url.endsWith('/prepare')).length,1);
    assert.equal(h.calls.filter(c=>c.url.endsWith('/send')).length,0);assert.equal(new ActionState(h.storage).pending.length,0);
    assert.equal(h.get('work-form').hidden,!messages);assert.deepEqual(h.redirects,[]);
    if(messages) {assert.equal(h.get('work-recipient').value,'worker');assert.equal(h.get('work-body').value,'Ordinary worker draft');}
    h.open(nudgeTaskId);await flushRecovery();
    const button=h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge');
    assert.equal(button.disabled,recovery!==200);assert.match(h.get('task-detail-content').innerHTML,/Complete canonical body/);
    if(recovery===200) {
      button.onclick();await settle();assert.equal(h.get('work-body').value,'Keep logical task reason');
      await h.get('work-form').onsubmit({preventDefault(){}});await flushRecovery();
      const sent=h.calls.filter(c=>c.url.endsWith('/send'));assert.equal(sent.length,1);
      assert.equal(sent[0].body.target.assignment_id,nudgeAssignment);assert.equal(sent[0].body.target.release_id,'r-'+'f'.repeat(64));
      assert.equal(sent[0].body.body,'Keep logical task reason');assert.equal(h.nudgeReads,2);
    }
  } finally {h.controls.dispose();}
});
for (const loss of ['room','session','source'])
test(`late capability refresh cannot restore nudge after ${loss} loss`,async()=>{
  const held=deferred(),h=await ownerRecoveryHarness({heldRecovery:held});
  try {
    await chooseNudge(h);await flushRecovery();
    const submitting=h.submit();await flushRecovery();assert.equal(h.nudgeReads,2);
    if(loss==='room')h.loop.setRoom('all');
    if(loss==='session')h.get('owner-session-logout').emit('click');
    if(loss==='source')await h.api.jget('/api/tasks?source-loss');
    await flushRecovery();held.resolve();await submitting;await flushRecovery();
    assert.equal(h.get('work-form').hidden,true);assert.equal(h.calls.filter(c=>c.url.endsWith('/send')).length,0);
    assert.equal(new ActionState(h.storage).pending.length,0);
    if(loss==='room')assert.equal(h.get('work-scope').textContent,'all');
    else {h.open(nudgeTaskId);await flushRecovery();assert.equal(h.get('task-detail-content').querySelectorAll('[data-kind]').some(b=>!b.disabled),false);}
  } finally {h.controls.dispose();}
});
test('capability recovery does not overwrite a message edited while its nudge context read is pending',async()=>{
  const held=deferred(),h=await ownerRecoveryHarness({messages:true,heldRecovery:held});
  try {
    await chooseNudge(h);await flushRecovery();const submitting=h.submit();await flushRecovery();
    assert.equal(h.get('work-form').hidden,false);h.get('work-recipient').value='worker';h.get('work-recipient').onchange();
    h.get('work-body').value='Message edited during recovery';h.get('work-body').emit('input');
    held.resolve();await submitting;await flushRecovery();
    assert.equal(h.get('work-body').value,'Message edited during recovery');assert.equal(h.get('work-recipient').value,'worker');
    assert.equal(h.nudgeReads,2);assert.equal(h.calls.filter(c=>c.url.endsWith('/send')).length,0);
  } finally {h.controls.dispose();}
});


const ownerFeedback = {...ownerNudge,scope:{...ownerNudge.scope,viewer:'feedback-grant'},actions:['feedback']};
const feedbackOptions = extra => ({interactionContext:()=>null,feedbackContext:()=>ownerFeedback,protected:true,jget:()=>ownerDetail(),
  crypto:{randomUUID:()=> '22222222-2222-4222-8222-222222222222'},...extra});
for(const transition of ['none','room','session'])
test(`feedback context waits for nudge and fences the originating epoch: ${transition}`,async()=>{
  const held=deferred(),reads=[];
  const h=harness(feedbackOptions({interactionContext:room=>{reads.push(['message',room]);return null;},
    nudgeContext:room=>{reads.push(['nudge',room]);return room==='web'?held.promise:null;},
    feedbackContext:room=>{reads.push(['feedback',room]);return room==='web'?ownerFeedback:null;}}));
  h.loop.setRoom('web');await settle();
  assert.deepEqual(reads,[['message','web'],['nudge','web']]);
  if(transition==='room')h.loop.setRoom('other');
  if(transition==='session')h.loop.pause();
  await settle();held.resolve(ownerNudge);await settle();
  assert.equal(reads.filter(([kind,room])=>kind==='feedback'&&room==='web').length,transition==='none'?1:0);
  if(transition==='room')assert.equal(reads.filter(([kind,room])=>kind==='feedback'&&room==='other').length,1);
  assert.equal(h.preparations.length,0);assert.equal(h.sends.length,0);
});
async function chooseFeedback(h) {
  h.loop.setRoom('web');await settle();h.open(nudgeTaskId);await settle();
  const button=h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback');
  assert.equal(button.disabled,false);button.onclick();await settle();
}
for(const state of ['queued','active','completed','failed','cancelled'])
test(`real feedback freezes canonical ${state} selection; terminal feedback uses explicit null`,async()=>{
  const assignment=state==='active'?{assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null}:null;
  const data=ownerDetail(assignment);data.data.task.state=state;
  const h=harness(feedbackOptions({jget:()=>data,nudgeContext:()=>ownerNudge}));
  h.loop.setRoom('web');await settle();h.open(nudgeTaskId);await settle();
  const buttons=h.get('task-detail-content').querySelectorAll('[data-kind]');
  assert.equal(buttons.find(b=>b.dataset.kind==='nudge').disabled,['completed','failed','cancelled'].includes(state));
  buttons.find(b=>b.dataset.kind==='feedback').onclick();await settle();
  assert.match(h.get('work-notice').textContent,/comment.*does not approve or change/);
  await h.submit();assert.equal(h.sends.length,1);assert.equal(h.preparations[0].kind,'feedback');
  assert.equal(h.sends[0].target.assignment_id,assignment?.assignment_id??null);
  assert.match(h.get('work-notice').textContent,/lead received this feedback/);
});
for(const messages of [false,true])for(const nudges of [false,true])for(const feedback of [false,true])
test(`independent owner capabilities: message=${messages},nudge=${nudges},feedback=${feedback}`,async()=>{
  const h=harness(feedbackOptions({interactionContext:()=>messages?{...context,simulation:false,actions:['message']}:null,
    nudgeContext:()=>nudges?ownerNudge:null,feedbackContext:()=>feedback?ownerFeedback:null}));
  h.loop.setRoom('web');await settle();assert.equal(h.get('work-form').hidden,!messages);
  h.open(nudgeTaskId);await settle();const buttons=h.get('task-detail-content').querySelectorAll('[data-kind]');
  assert.equal(buttons.find(b=>b.dataset.kind==='feedback').disabled,!feedback);
  assert.equal(buttons.find(b=>b.dataset.kind==='nudge').disabled,!nudges);
  assert.equal(h.sends.length,0);assert.equal(h.preparations.length,0);
});
for(const change of ['body','body-reverted','room','pause','different-task','grant'])
test(`late feedback prepare never sends after ${change}`,async()=>{
  const held=deferred(),h=harness(feedbackOptions({prepareAction:()=>held.promise}));await chooseFeedback(h);
  const submitting=h.submit();await settle();
  if(change.startsWith('body')){h.get('work-body').value='Edited comment';h.get('work-body').emit('input');if(change==='body-reverted'){h.get('work-body').value='Hello';h.get('work-body').emit('input');}}
  if(change==='room')h.loop.setRoom('other');if(change==='pause')h.loop.pause();if(change==='different-task')h.open('wi_'+'f'.repeat(32));
  if(change==='grant')h.loop.invalidate(undefined,ownerFeedback.scope,'feedback',ownerFeedback.recipients[0].id);
  const {body,...metadata}=h.preparations[0];held.resolve({...metadata,semantic_sha256:'d'.repeat(64)});await submitting;
  assert.equal(h.sends.length,0);assert.equal(new ActionState(h.storage).pending.length,0);
});
for(const [label,change] of [['missing current assignment',t=>delete t.current_assignment],['terminal with active assignment',t=>{t.state='completed';t.current_assignment={assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null};}],['unresolved',t=>{t.resolved=false;}],['historical assignment',t=>{t.state='completed';t.current_assignment={assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'completed',terminal_event:'completed'};}]])
test(`feedback refuses malformed selection: ${label}`,async()=>{
 const data=ownerDetail();change(data.data.task);const h=harness(feedbackOptions({jget:()=>data}));h.loop.setRoom('web');await settle();h.open(nudgeTaskId);await settle();
 const button=h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback');assert.equal(button.disabled,true);button.onclick();assert.equal(h.preparations.length,0);
});
test('feedback recorded and lost-response reload keep original metadata; inspection never prepares or sends',async()=>{
  for(const lost of [false,true]) {
    const storage=store(),h=harness(feedbackOptions({storage,sendAction:r=>{if(lost)throw Error('lost');return receipt(r,'recorded');}}));await chooseFeedback(h);await h.submit();
    const original=new ActionState(storage).pending[0];assert.equal(original.kind,'feedback');assert.ok(!storage.getItem('plane.pending-actions.v1').includes('Hello'));
    if(!lost)assert.match(h.get('work-notice').textContent,/recorded.*delivery.*unconfirmed/);
    const newer={...ownerFeedback,release_id:'r-'+'f'.repeat(64)},restored=harness(feedbackOptions({storage,feedbackContext:()=>newer,jget:()=>{throw Error('lookup must not select task');}}));
    restored.loop.setRoom('web');await settle();assert.equal(restored.sends.length,0);assert.equal(restored.preparations.length,0);
    await restored.pendingClick(original.request_id);assert.deepEqual(restored.lookups,[original]);assert.equal(restored.sends.length,0);assert.equal(restored.preparations.length,0);
  }
});
test('feedback reallow leaves old-generation request visible and prevents inspecting it under replacement grant',async()=>{
  const storage=store(),h=harness(feedbackOptions({storage,sendAction:()=>{throw Error('lost');}}));await chooseFeedback(h);await h.submit();const original=new ActionState(storage).pending[0];
  const newer={...ownerFeedback,scope:{...ownerFeedback.scope,viewer:'replacement-grant'}},restored=harness(feedbackOptions({storage,feedbackContext:()=>newer}));
  restored.loop.setRoom('web');await settle();assert.match(restored.get('work-pending').innerHTML,/Retained request from prior access/);assert.ok(restored.get('work-pending').innerHTML.includes(original.request_id));
  await restored.pendingClick(original.request_id);assert.equal(restored.lookups.length,0);assert.equal(new ActionState(storage).pending.length,1);
});
test('feedback refusal has one independent refresh and preserves message/nudge drafts without auto-selecting task',async()=>{
  let feedbackReads=0,nudgeReads=0;const held=deferred();
  const h=harness(feedbackOptions({interactionContext:()=>({...context,simulation:false,actions:['message']}),nudgeContext:()=>{nudgeReads++;return ownerNudge;},
    feedbackContext:()=>++feedbackReads===1?ownerFeedback:held.promise}));
  h.loop.setRoom('web');await settle();h.get('work-recipient').value='worker';h.get('work-recipient').onchange();h.get('work-body').value='Worker draft';h.get('work-body').emit('input');
  h.open(nudgeTaskId);await settle();h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge').onclick();await settle();
  h.get('work-body').value='Nudge reason';h.get('work-body').emit('input');h.open(nudgeTaskId);await settle();h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback').onclick();await settle();
  h.get('work-body').value='Feedback comment';h.get('work-body').emit('input');h.loop.invalidate(undefined,ownerFeedback.scope,'feedback',ownerFeedback.recipients[0].id);await settle();
  assert.equal(feedbackReads,2);assert.equal(nudgeReads,1);assert.equal(h.get('work-body').value,'Worker draft');assert.equal(h.get('work-recipient').value,'worker');
  h.get('work-body').value='New worker draft';h.get('work-body').emit('input');held.resolve(ownerFeedback);await settle();assert.equal(h.get('work-body').value,'New worker draft');
  h.open(nudgeTaskId);await settle();h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback').onclick();await settle();assert.equal(h.get('work-body').value,'Feedback comment');
  h.open(nudgeTaskId);await settle();h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='nudge').onclick();await settle();assert.equal(h.get('work-body').value,'Nudge reason');assert.equal(h.sends.length,0);
});
test('late old-manager feedback refusal cannot disable refreshed same-viewer capability',async()=>{
  let current=ownerFeedback;const h=harness(feedbackOptions({feedbackContext:()=>current}));await chooseFeedback(h);
  current={...ownerFeedback,recipients:[{id:'actor_'+'f'.repeat(32),label:'New lead',lead:true}]};h.loop.setRoom('web');await settle();
  h.loop.invalidate(undefined,ownerFeedback.scope,'feedback',ownerFeedback.recipients[0].id);h.open(nudgeTaskId);await settle();
  assert.equal(h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback').disabled,false);
});
test('feedback selection owns snapshots of context and current assignment, not transport response objects',async()=>{
  const capability=structuredClone(ownerFeedback),assignment={assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null};
  const data=ownerDetail(assignment),originalTarget={recipient:capability.recipients[0].id,task_id:nudgeTaskId,assignment_id:nudgeAssignment,release_id:capability.release_id};
  const h=harness(feedbackOptions({feedbackContext:()=>capability,jget:()=>data}));
  h.loop.setRoom('web');await settle();h.open(nudgeTaskId);await settle();
  capability.scope.viewer='changed-outside-controller';capability.recipients[0].id='actor_'+'f'.repeat(32);
  capability.release_id='r-'+'f'.repeat(64);capability.actions[0]='nudge';
  data.data.task.task_id='wi_'+'f'.repeat(32);data.data.task.state='completed';
  assignment.assignment_id='asg_'+'f'.repeat(32);assignment.terminal_event='completed';
  h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback').onclick();await settle();await h.submit();
  assert.equal(h.sends.length,1);assert.deepEqual(h.sends[0].target,originalTarget);
  assert.deepEqual(h.sends[0].scope,ownerFeedback.scope);assert.equal(h.sends[0].kind,'feedback');
});
test('nudge and feedback capability refreshes have independent generations and never replace the message draft',async()=>{
  const feedback=deferred(),nudge=deferred();let feedbackReads=0,nudgeReads=0;
  const h=harness(feedbackOptions({interactionContext:()=>({...context,simulation:false,actions:['message']}),
    feedbackContext:()=>++feedbackReads===1?ownerFeedback:feedback.promise,nudgeContext:()=>++nudgeReads===1?ownerNudge:nudge.promise}));
  h.loop.setRoom('web');await settle();h.get('work-body').value='Independent message draft';h.get('work-body').emit('input');
  h.loop.invalidate(undefined,ownerFeedback.scope,'feedback',ownerFeedback.recipients[0].id);
  h.loop.invalidate(undefined,ownerNudge.scope,'nudge',ownerNudge.recipients[0].id);await settle();
  assert.equal(feedbackReads,2);assert.equal(nudgeReads,2);nudge.resolve(ownerNudge);await settle();h.open(nudgeTaskId);await settle();
  let buttons=h.get('task-detail-content').querySelectorAll('[data-kind]');
  assert.equal(buttons.find(b=>b.dataset.kind==='feedback').disabled,true);assert.equal(buttons.find(b=>b.dataset.kind==='nudge').disabled,false);
  feedback.resolve(ownerFeedback);await settle();buttons=h.get('task-detail-content').querySelectorAll('[data-kind]');
  assert.equal(buttons.find(b=>b.dataset.kind==='feedback').disabled,false);assert.equal(h.get('work-body').value,'Independent message draft');
  assert.equal(h.preparations.length,0);assert.equal(h.sends.length,0);
});
for(const loss of ['room','pause'])
test(`late feedback capability refresh cannot restore access after ${loss}`,async()=>{
  const held=deferred();let reads=0;
  const h=harness(feedbackOptions({feedbackContext:()=>++reads===1?ownerFeedback:held.promise}));await chooseFeedback(h);
  h.loop.invalidate(undefined,ownerFeedback.scope,'feedback',ownerFeedback.recipients[0].id);await settle();assert.equal(reads,2);
  if(loss==='room')h.loop.setRoom('all');else h.loop.pause();
  held.resolve(ownerFeedback);await settle();h.open(nudgeTaskId);await settle();
  assert.equal(h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback').disabled,true);
  assert.equal(h.get('work-form').hidden,true);assert.equal(h.sends.length,0);assert.equal(h.preparations.length,0);
});

const inspectedWorker = { uid: 'actor_' + '2'.repeat(32), alias: 'bot:web/subteam/worker',
  fleet: 'web/subteam', fleet_uid: 'fleet_' + '3'.repeat(32), provisional: false, current: true };
const inspectionContext = { ...context, simulation: false, room: inspectedWorker.fleet,
  scope: { ...context.scope, fleet: inspectedWorker.fleet }, actions: ['message'],
  recipients: [{ id: 'actor_' + '1'.repeat(32), label: 'Worker', lead: true },
    { id: inspectedWorker.uid, label: 'Worker' }] };

test('bot selection uses the exact admitted UID and room, keeps recipient drafts and never calls transport', async () => {
  let contextReads = 0;
  const h = harness({ interactionContext: () => { contextReads++; return inspectionContext; } });
  h.loop.setRoom(inspectedWorker.fleet); await settle();
  const lead = { ...inspectedWorker, uid: inspectionContext.recipients[0].id, alias: 'bot:web/subteam/lead' };
  h.get('work-body').value = 'Draft for the lead'; h.get('work-body').emit('input');
  assert.equal(h.loop.selectMessageRecipient(inspectedWorker), true);
  assert.equal(h.get('work-recipient').value, inspectedWorker.uid);
  assert.equal(h.get('work-body').value, '');
  h.get('work-body').value = 'Draft for this worker'; h.get('work-body').emit('input');
  assert.equal(h.loop.selectMessageRecipient(lead), true);
  assert.equal(h.get('work-body').value, 'Draft for the lead');
  assert.equal(h.loop.selectMessageRecipient(inspectedWorker), true);
  assert.equal(h.get('work-body').value, 'Draft for this worker');
  assert.equal(h.document.activeElement, h.get('work-body'));
  assert.equal(contextReads, 1); assert.deepEqual(h.sends, []); assert.deepEqual(h.lookups, []); assert.deepEqual(h.preparations, []);
});

for (const patch of [{ uid: 'actor_' + '9'.repeat(32) }, { fleet: 'other/team' },
  { provisional: true }, { current: false }, { uid: 'Worker' }, { fleet_uid: '' }])
test(`bot selection refuses unavailable identity ${JSON.stringify(patch)} without lead fallback`, async () => {
  const h = harness({ interactionContext: () => inspectionContext }); h.loop.setRoom(inspectedWorker.fleet); await settle();
  h.get('work-body').value = 'Keep this draft'; h.get('work-body').emit('input');
  const previous = h.get('work-recipient').value;
  assert.equal(h.loop.canSelectMessageRecipient({ ...inspectedWorker, ...patch }), false);
  assert.equal(h.loop.selectMessageRecipient({ ...inspectedWorker, ...patch }), false);
  assert.equal(h.get('work-recipient').value, previous); assert.equal(h.get('work-body').value, 'Keep this draft');
  assert.deepEqual(h.sends, []); assert.deepEqual(h.lookups, []); assert.deepEqual(h.preparations, []);
});

test('unavailable inspection selection is never queued when later authority arrives; revocation refuses selection', async () => {
  const waiting = deferred(); let reads = 0;
  const h = harness({ interactionContext: () => { reads++; return waiting.promise; } });
  h.loop.setRoom(inspectedWorker.fleet); await settle();
  assert.equal(h.loop.selectMessageRecipient(inspectedWorker), false);
  waiting.resolve(inspectionContext); await settle();
  assert.equal(h.get('work-recipient').value, inspectionContext.recipients[0].id);
  h.loop.invalidate(undefined, inspectionContext.scope, 'message', inspectedWorker.uid);
  assert.equal(h.loop.selectMessageRecipient(inspectedWorker), false);
  assert.equal(reads, 1); assert.deepEqual(h.sends, []);
  const readonly = harness({ interactionContext: () => null }); readonly.loop.setRoom(inspectedWorker.fleet); await settle();
  assert.equal(readonly.loop.selectMessageRecipient(inspectedWorker), false);
});

test('bot selection preserves pending recipient UUIDs without receipt lookup or resend', async () => {
  const storage = store(); const state = new ActionState(storage);
  state.begin(inspectionContext, 'message', { recipient: inspectedWorker.uid, task_id: null }, 'Already submitted', 'original-worker-request');
  const h = harness({ storage, interactionContext: () => inspectionContext }); h.loop.setRoom(inspectedWorker.fleet); await settle();
  assert.equal(h.loop.selectMessageRecipient(inspectedWorker), true);
  assert.equal(h.get('work-send').disabled, true);
  assert.equal(new ActionState(storage).pending[0].request_id, 'original-worker-request');
  assert.deepEqual(h.sends, []); assert.deepEqual(h.lookups, []);
});

test('selecting an inspected bot resets task composition and fences a pending preparation', async () => {
  const prepare = deferred();
  const worker = { ...inspectedWorker, fleet: 'web', alias: 'bot:web/worker' };
  const message = { ...inspectionContext, room: 'web', scope: { ...inspectionContext.scope, fleet: 'web' } };
  const h = harness(nudgeOptions({ interactionContext: () => message, prepareAction: () => prepare.promise }));
  await chooseNudge(h); const submitting = h.submit(); await settle();
  assert.equal(h.preparations.length, 1); assert.equal(h.sends.length, 0);
  assert.equal(h.loop.selectMessageRecipient(worker), true);
  assert.equal(h.get('work-task').textContent, ''); assert.equal(h.get('work-label').textContent, 'Message');
  assert.equal(h.get('work-recipient').value, worker.uid); assert.equal(h.get('work-body').value, '');
  const { body, ...metadata } = h.preparations[0]; prepare.resolve({ ...metadata, semantic_sha256: 'd'.repeat(64) });
  await submitting;
  assert.equal(h.sends.length, 0); assert.equal(new ActionState(h.storage).pending.length, 0);
  assert.match(h.get('work-notice').textContent, /Task nudge was not sent\. Your reason is kept/);
  h.open(nudgeTaskId); await settle();
  h.get('task-detail-content').querySelectorAll('[data-kind]').find(b => b.dataset.kind === 'nudge').onclick();
  assert.equal(h.get('work-body').value, 'Hello');
});


test('late delivery for the prior recipient keeps the newly selected bot draft', async () => {
  const sent = deferred();
  const h = harness({ interactionContext: () => inspectionContext, sendAction: () => sent.promise });
  h.loop.setRoom(inspectedWorker.fleet); await settle();
  const submitting = h.submit(); await settle(); assert.equal(h.sends.length, 1);
  assert.equal(h.loop.selectMessageRecipient(inspectedWorker), true);
  h.get('work-body').value = 'New worker draft'; h.get('work-body').emit('input');
  sent.resolve(receipt(h.sends[0])); await submitting;
  assert.equal(h.get('work-recipient').value, inspectedWorker.uid);
  assert.equal(h.get('work-body').value, 'New worker draft');
  assert.equal(h.sends.length, 1); assert.deepEqual(h.lookups, []);
});

// Channel reads run app.js's own threadArticle and machineryBlock, so the
// message and disclosure markup the controller selects on is production's: a
// rename there fails these tests. Only the leaf formatters are stubbed. Reads
// never ask the action transport to prepare or send.
const appSource = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
function appSlice(start, end) {
  const from = appSource.indexOf(start), to = appSource.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `app.js no longer contains ${start} before ${end}`);
  return appSource.slice(from, to);
}
const conversationRead = (...threads) => ({ state: 'ok', data: { threads } });
const conversationThread = (id = 'task-a', messages = ['first'], extra = {}) => ({
  key: id, work_item_id: id, latest_seq: messages.length, task_events: [],
  messages: messages.map(msg_id => ({msg_id, body: msg_id, message_class: 'chat', sender_short: 'lead', recipient_short: 'owner'})), ...extra,
});
// The shared double keeps innerHTML flat; a rendered card needs its nesting.
function markupElement(ui) {
  const root = new ui.Element();
  Object.defineProperty(root, 'innerHTML', { get() { return this.html || ''; }, set(html) {
    this.html = html; this.children = [];
    const open = [this];
    for (const [, close, tag, attributes] of html.matchAll(/<(\/?)([a-z][\w-]*)\b([^>]*)>/g)) {
      if (close) { open.pop(); continue; }
      const dataset = {};
      for (const data of attributes.matchAll(/data-([\w-]+)="([^"]*)"/g))
        dataset[data[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = data[2];
      const child = new ui.Element('', dataset);
      child.tagName = tag.toUpperCase(); child.className = attributes.match(/\bclass="([^"]*)"/)?.[1] || '';
      open.at(-1).append(child); open.push(child);
    }
  } });
  return root;
}
function conversationRenderer(thread, ui) {
  if (!ui.threadArticle) {
    const bindings = { document: { createElement: () => markupElement(ui) }, currentFleet: 'web',
      esc: value => String(value ?? ''), ago: () => '', clip: text => text, latestTx: () => null, nudgeReason: () => null,
      deliveryLine: () => '', bodyBlock: message => `<div class="body">${message.body}</div>`,
      CLASS_TAGS: new Set(), THREAD_TERMINAL_STATUS: {} };
    runInNewContext(appSlice('function machineryBlock(', '// Keyed render:'), bindings);
    ui.threadArticle = bindings.threadArticle;
  }
  return ui.threadArticle(thread);
}
// Located by structure, not by the controller's selectors.
const nodesIn = (root, match) => root.children.flatMap(child => [...(match(child) ? [child] : []), ...nodesIn(child, match)]);
const messagesIn = article => nodesIn(article, node => 'msgId' in node.dataset);
const disclosuresIn = article => nodesIn(article, node => node.tagName === 'DETAILS');
// A Selection double with one Range: its endpoints and the nodes it spans.
const selectionOver = (anchorNode, focusNode, ...spanned) => ({ isCollapsed: false, anchorNode, focusNode, rangeCount: 1,
  getRangeAt: () => ({ collapsed: false, intersectsNode: node => spanned.some(root => root === node || root.contains(node) || node.contains(root)) }) });
async function conversationHarness(options = {}) {
  const reads = [];
  const h = harness({renderThread:conversationRenderer, jget(url) { reads.push(url); return canonicalDetail(url.includes('task-b') ? 'task-b' : 'task-a'); }, ...options});
  h.loop.setRoom('web'); await settle(); h.update(conversationRead(conversationThread())); h.open(); await settle();
  return {...h, reads};
}
test('same-task recent replies update only conversation, retaining disclosures and frozen action selection', async () => {
  const h = await conversationHarness(), content = h.get('task-detail-content'), snapshot = content.innerHTML;
  const first = h.get('task-reports').children[0];
  assert.equal(messagesIn(first).length, 1); assert.equal(disclosuresIn(first).length, 1); disclosuresIn(first)[0].open = true;
  h.get('work-recipient').value = 'worker'; h.get('work-recipient').onchange();
  h.get('work-body').value = 'Unsent worker draft'; h.get('work-body').emit('input');
  h.update(conversationRead(conversationThread('task-a', ['first', 'reply']), conversationThread('task-b', ['foreign'])),
    {state:'ok',data:{tasks:[{task_id:'task-a',fleet:'web',state:'active',current_assignment:{assignment_id:'new'}}]}});
  const article = h.get('task-reports').children[0];
  assert.deepEqual(messagesIn(article).map(message => message.dataset.msgId), ['first', 'reply']);
  assert.deepEqual(disclosuresIn(article).map(disclosure => disclosure.open), [true, false]);
  assert.equal(content.innerHTML, snapshot); assert.equal(h.reads.length, 1);
  assert.equal(h.get('work-recipient').value, 'worker'); assert.equal(h.get('work-body').value, 'Unsent worker draft');
  content.querySelectorAll('[data-kind]')[0].onclick();
  h.get('work-body').value = 'Task comment'; await h.get('work-form').onsubmit({preventDefault(){}});
  assert.equal(h.sends[0].target.task_id, 'task-a'); assert.equal(h.sends[0].kind, 'feedback');
  assert.equal(h.preparations.length, 0); assert.equal(h.lookups.length, 0);
});
test('unchanged conversation nodes remain and receipt-only evidence updates without a new read', async () => {
  const h = await conversationHarness(), first = h.get('task-reports').children[0];
  h.update(conversationRead(conversationThread())); assert.equal(h.get('task-reports').children[0], first);
  h.update(conversationRead(conversationThread('task-a', ['first'], {delivery:'received'})));
  assert.notEqual(h.get('task-reports').children[0], first); assert.equal(h.reads.length, 1);
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
});
test('conversation focus or text selection defers updates until explicit opt-in without replacing the task', async () => {
  // 'spanning' is select-all in the dialog: both endpoints lie outside the region.
  for (const interaction of ['focus', 'selection', 'spanning']) {
    const h = await conversationHarness(), reports = h.get('task-reports'), first = reports.children[0];
    const content = h.get('task-detail-content'), control = disclosuresIn(first)[0].children[0];
    const selection = interaction === 'selection' ? selectionOver(control, control, control)
      : interaction === 'spanning' ? selectionOver(content, content, content) : null;
    if (interaction === 'focus') control.focus(); else h.document.selection = selection;
    const focused = h.document.activeElement;
    h.update(conversationRead(conversationThread('task-a', ['first', 'reply'])));
    assert.equal(reports.children[0], first); assert.equal(h.get('task-conversation-update').hidden, false);
    assert.equal(h.document.activeElement, focused); assert.equal(h.document.selection, selection);
    h.get('task-conversation-update').onclick();
    assert.equal(messagesIn(reports.children[0]).length, 2); assert.equal(h.get('task-conversation-update').hidden, true);
    assert.equal(h.document.activeElement, reports); assert.equal(h.reads.length, 1);
    // Focus left on the region by that opt-in must not defer every later update.
    h.document.selection = null;
    h.update(conversationRead(conversationThread('task-a', ['first', 'reply', 'third'])));
    assert.equal(messagesIn(reports.children[0]).length, 3); assert.equal(h.get('task-conversation-update').hidden, true);
    assert.equal(h.document.activeElement, reports);
    assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
  }
});
test('hiding a focused update button keeps focus on the conversation region without taking it from elsewhere', async () => {
  for (const outcome of ['updated', 'reverted', 'elsewhere']) {
    const h = await conversationHarness(), reports = h.get('task-reports'), button = h.get('task-conversation-update');
    const control = disclosuresIn(reports.children[0])[0].children[0], close = h.get('task-detail-close');
    h.document.selection = selectionOver(control, control, control);
    h.update(conversationRead(conversationThread('task-a', ['first', 'reply']))); assert.equal(button.hidden, false);
    if (outcome === 'elsewhere') close.focus(); else button.focus();
    h.document.selection = null;
    h.update(conversationRead(conversationThread('task-a', outcome === 'updated' ? ['first', 'reply', 'third'] : ['first'])));
    assert.equal(button.hidden, true); assert.equal(messagesIn(reports.children[0]).length, outcome === 'updated' ? 3 : 1);
    assert.equal(h.document.activeElement, outcome === 'elsewhere' ? close : reports);
    assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
  }
});
test('the real stream source callback clears an open conversation through the channel-only door and keeps the board', async () => {
  const h = harness({renderThread:conversationRenderer, jget() { return {state:'unavailable',remediation:'Unsupported detail route'}; }});
  h.loop.setRoom('web'); await settle(); h.update(conversationRead(conversationThread())); h.open(); await settle();
  assert.match(h.get('task-detail-content').innerHTML, /limited board snapshot/);
  assert.equal(messagesIn(h.get('task-reports').children[0]).length, 1);
  // app.js's own openStream, wired to this controller; only its panels are stubbed.
  const listeners = new Map(), cleared = [];
  const bindings = { createEventSource: () => ({ addEventListener: (name, listener) => listeners.set(name, listener) }),
    workLoop: h.loop, $: id => h.get(id), renderSummary() {}, renderHeader: value => cleared.push(value),
    renderHostFacts: value => cleared.push(value), renderState() {}, pushDebugRow() {}, scheduleRefresh() {}, generation: 0 };
  runInNewContext(appSlice('function openStream() {', '$("debug-toggle")'), bindings); bindings.openStream();
  listeners.get('source')({ data: JSON.stringify({state:'unreadable',remediation:'Source access unavailable'}) });
  assert.deepEqual(cleared, [null, null]);
  assert.equal(h.get('task-reports').children.length, 0); assert.match(h.get('task-reports').innerHTML, /Source access unavailable/);
  h.get('task-detail-refresh').onclick(); await settle();
  assert.match(h.get('task-detail-content').innerHTML, /limited board snapshot/);
  assert.match(h.get('task-reports').innerHTML, /Source access unavailable/);
});
test('a refresh started before stream source loss cannot restore the cleared conversation on public or owner reads', async () => {
  for (const guarded of [false, true]) {
    const h = await conversationHarness({protected: guarded}), reports = h.get('task-reports');
    // app.js's own refreshBoards, coalescing, safety timer and stream callback; reads and timers are held here.
    const requests = [], timers = [], listeners = new Map(), painted = [];
    const bindings = { fleetsSeen: true, workLoop: h.loop, $: id => h.get(id),
      jget(url) { const request = deferred(); requests.push({ url, ...request }); return request.promise; },
      setTimeout(callback, delay) { timers.push({ callback, delay }); return timers.length; }, clearTimeout() {},
      createEventSource: () => ({ addEventListener: (name, listener) => listeners.set(name, listener) }),
      channelUrl: () => '/api/channel', fleetQuery: () => '', syncWorkRoom: () => false, adoptFleets() {},
      renderChannel: value => painted.push(value), renderTasks() {}, renderFleet() {}, renderSummary() {}, renderHeader() {},
      renderHostFacts() {}, renderFleetTabs() {}, renderOverview() {}, renderState() {}, pushDebugRow() {} };
    runInNewContext(appSlice('let refreshTimer = null;', '$("debug-toggle")'), bindings); bindings.openStream();
    const board = {state:'ok',data:{tasks:[{task_id:'task-a',fleet:'web',title:'task-a'}]}};
    const answer = async channel => {
      for (const request of requests.splice(0))
        request.resolve(request.url === '/api/channel' ? channel : request.url === '/api/tasks' ? board : {state:'ok',data:{}});
      await settle(); await settle();
    };
    const stale = bindings.refreshBoards(); assert.equal(requests.length, 5);
    listeners.get('source')({ data: JSON.stringify({state:'unreadable',remediation:'Source access unavailable'}) });
    assert.equal(reports.children.length, 0); assert.match(reports.innerHTML, /Source access unavailable/);
    await answer(conversationRead(conversationThread('task-a', ['first', 'older']))); await stale;
    assert.deepEqual(painted, []); assert.equal(reports.children.length, 0); assert.match(reports.innerHTML, /Source access unavailable/);
    // The retired read is replaced through the existing coalesced refetch, which re-arms the safety timer.
    assert.deepEqual(timers.map(timer => timer.delay), [400]);
    timers.shift().callback(); assert.equal(requests.length, 5);
    await answer(conversationRead(conversationThread('task-a', ['first', 'recovered'])));
    assert.equal(painted.length, 1);
    assert.deepEqual(messagesIn(reports.children[0]).map(message => message.dataset.msgId), ['first', 'recovered']);
    assert.deepEqual(timers.map(timer => timer.delay), [60000]);
    assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
  }
});
test('conversation does not disturb focus or selection in the frozen lifecycle region', async () => {
  const h = await conversationHarness(), content = h.get('task-detail-content');
  const title = h.get('task-detail-title'), selection = selectionOver(title, title, title);
  assert.ok(content.contains(title) && !h.get('task-reports').contains(title));
  h.get('task-detail-close').focus(); h.document.selection = selection;
  h.update(conversationRead(conversationThread('task-a', ['first', 'reply'])));
  assert.equal(messagesIn(h.get('task-reports').children[0]).length, 2);
  assert.equal(h.document.activeElement, h.get('task-detail-close')); assert.equal(h.document.selection, selection);
});
test('source loss clears conversation immediately and a retained update button cannot revive old room or session text', async () => {
  for (const change of ['room', 'session', 'close']) {
    const h = await conversationHarness(), reports = h.get('task-reports');
    disclosuresIn(reports.children[0])[0].children[0].focus();
    h.update(conversationRead(conversationThread('task-a', ['first', 'reply'])));
    const staleButton = h.get('task-conversation-update'); assert.equal(staleButton.hidden, false);
    h.update({state:'denied',remediation:'Source access unavailable'});
    assert.equal(reports.children.length, 0); assert.match(reports.innerHTML, /Source access unavailable/);
    // Focus moves off the cleared message onto the region now holding only the state.
    assert.equal(staleButton.hidden, true); assert.equal(h.document.activeElement, reports);
    if (change === 'room') h.loop.setRoom('other');
    else if (change === 'session') h.loop.pause(); else h.get('task-detail-close').onclick();
    staleButton.onclick(); await settle();
    assert.equal(h.get('task-detail-content').innerHTML, '');
    assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
  }
});
test('a superseded task update button cannot update a newly selected opaque task', async () => {
  const h = await conversationHarness(), reports = h.get('task-reports'); reports.children[0].focus();
  h.update(conversationRead(conversationThread('task-a', ['first','reply']), conversationThread('task-b', ['other'])));
  const staleButton = h.get('task-conversation-update'); h.open('task-b'); await settle();
  assert.equal(h.get('task-reports').children[0].dataset.key, 'task-b');
  staleButton.onclick(); assert.equal(h.get('task-reports').children[0].dataset.key, 'task-b');
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
});
test('recent conversation updates cannot advance the prepared feedback assignment or release', async () => {
  const assignment = {assignment_id:nudgeAssignment,task_id:nudgeTaskId,state:'active',terminal_event:null};
  let detailReads = 0;
  const h = harness(feedbackOptions({renderThread:conversationRenderer,jget(){detailReads++;return ownerDetail(assignment);}}));
  h.loop.setRoom('web'); await settle(); h.update(conversationRead(conversationThread(nudgeTaskId))); h.open(nudgeTaskId); await settle();
  const changed = ownerDetail({...assignment,assignment_id:'asg_'+'f'.repeat(32)}).data.task;
  h.update(conversationRead(conversationThread(nudgeTaskId,['first','reply'])),{state:'ok',data:{tasks:[changed]}});
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
  h.get('task-detail-content').querySelectorAll('[data-kind]').find(b=>b.dataset.kind==='feedback').onclick(); await settle();
  await h.submit();
  assert.equal(detailReads, 1); assert.equal(h.preparations.length, 1);
  assert.equal(h.preparations[0].target.assignment_id, nudgeAssignment);
  assert.equal(h.preparations[0].target.release_id, ownerFeedback.release_id);
  assert.equal(h.sends[0].target.assignment_id, nudgeAssignment);
});

test('production fleet adoption through overview keeps exact roster identity usable by the real message composer', async () => {
  const app = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
  const adopt = app.slice(app.indexOf('function adoptFleets('), app.indexOf('function fleetQuery('));
  const roster = app.slice(app.indexOf('function railRow('), app.indexOf('// The header in the operator'));
  const recipient = app.slice(app.indexOf('function equipmentRecipient()'), app.indexOf('function refreshEquipmentAction()'));
  // Minimal sanitized actual owner-browser response shapes: fleets has uid;
  // overview must carry that same uid when it replaces the initial dimension.
  // Server regression verifies both actual endpoint projections against SQLite.
  const fleet = { alias: inspectedWorker.fleet, uid: inspectedWorker.fleet_uid, bots: 2, provisional: 0 };
  const dimension = { state: 'ok', data: { fleets: [fleet], default: fleet.alias } };
  const overview = { state: 'ok', data: { fleets: [{ ...fleet, open: 0, attention: 0 }], default: fleet.alias } };
  let contextReads = 0;
  const h = harness({ interactionContext: () => { contextReads++; return inspectionContext; } });
  h.loop.setRoom(fleet.alias); await settle();
  const rail = new h.Element('fleet');
  const bindings = { document: h.document, $: id => h.get(id), fleets: [], fleetsSeen: false, currentFleet: null,
    currentView: 'channel', sessionPaused: false, sessionEpoch: 1, rosterIdentities: new Map(), rosterGeneration: 0,
    equipmentAlias: null, equipmentSelection: null, equipmentFocus: null,
    inventoryAliases: new Set([inspectedWorker.alias]), renderState: () => false,
    refreshEquipmentAction() {}, esc: value => String(value), ago: () => '', loadPick: () => null };
  runInNewContext(`${adopt}
${roster}
${recipient}`, bindings);
  bindings.adoptFleets(dimension); bindings.adoptFleets(overview);
  bindings.renderFleet({ state: 'ok', data: { identities: [{ ...inspectedWorker, kind: 'actor', short: 'Worker' }] } });
  assert.equal(rail.querySelectorAll('[data-bot-inspect]')[0].dataset.botInspect, inspectedWorker.uid);
  bindings.equipmentSelection = bindings.rosterIdentities.get(inspectedWorker.uid); bindings.equipmentAlias = inspectedWorker.alias;
  const identity = bindings.equipmentRecipient();
  assert.equal(identity.fleet_uid, fleet.uid);
  assert.equal(h.loop.canSelectMessageRecipient(identity), true);
  assert.equal(h.loop.selectMessageRecipient(identity), true);
  assert.equal(h.get('work-recipient').value, inspectedWorker.uid); assert.equal(contextReads, 1);
  assert.deepEqual(h.sends, []); assert.deepEqual(h.preparations, []); assert.deepEqual(h.lookups, []);
});

// Bounded-lineage diagnostics concern the room window, never this selected task.
const lineageRead = (unresolved_threads, ...threads) => ({state:'ok',data:{threads,
  lineage:{max_hops:32,max_ancestor_lookups:1000,ancestor_lookups:14,unresolved_threads,reasons:['<private technical reason>']}}});
test('room lineage notice is honest with an empty selected conversation and never a failed load', async () => {
  const h = await conversationHarness(); h.update(lineageRead(2, conversationThread('task-b')));
  const note = h.get('task-lineage-note'), reports = h.get('task-reports');
  assert.equal(note.hidden, false); assert.match(note.textContent, /recent replies in this team/);
  assert.match(note.textContent, /Task conversations may be incomplete/); assert.doesNotMatch(note.textContent, /task-a|2|1000|32|private technical|<|failed|unavailable/);
  assert.match(reports.innerHTML, /No linked conversation/); assert.doesNotMatch(reports.innerHTML, /failed|unavailable/);
  assert.equal(h.get('task-detail-refresh').hidden, false); assert.equal(h.reads.length, 1);
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
});
test('absent or invalid lineage diagnostics stay compatible and a resolved window removes the notice', async () => {
  const h = await conversationHarness(), reports = h.get('task-reports'), first = reports.children[0], note = h.get('task-lineage-note');
  assert.equal(note.hidden, true);
  h.update(lineageRead(1,conversationThread())); assert.equal(note.hidden, false); assert.equal(reports.children[0], first);
  for (const count of [0,-1,'1',null,1.5,Number.MAX_SAFE_INTEGER+1]) {
    h.update(lineageRead(count,conversationThread())); assert.equal(note.hidden, true); assert.equal(note.textContent, '');
  }
  h.update(lineageRead(3,conversationThread())); assert.equal(note.hidden, false);
  h.update(conversationRead(conversationThread())); assert.equal(note.hidden, true); assert.equal(note.textContent, '');
  assert.equal(reports.children[0], first); assert.equal(h.reads.length, 1);
});
test('notice-only updates preserve conversation focus, expanded disclosures and message draft until explicit update', async () => {
  const h = await conversationHarness(), reports = h.get('task-reports'), first = reports.children[0], note = h.get('task-lineage-note');
  const disclosure = disclosuresIn(first)[0], control = disclosure.children[0]; disclosure.open = true; control.focus();
  h.get('work-recipient').value = 'worker'; h.get('work-recipient').onchange(); h.get('work-body').value = 'Keep this draft'; h.get('work-body').emit('input');
  control.focus(); h.update(lineageRead(4,conversationThread()));
  assert.equal(note.hidden, true); assert.equal(h.get('task-conversation-update').hidden, false);
  assert.equal(h.document.activeElement, control); assert.equal(reports.children[0], first); assert.equal(disclosure.open, true);
  h.get('task-conversation-update').onclick(); assert.equal(note.hidden, false); assert.equal(reports.children[0], first);
  assert.equal(disclosure.open, true); assert.equal(h.document.activeElement, reports);
  assert.equal(h.get('work-recipient').value, 'worker'); assert.equal(h.get('work-body').value, 'Keep this draft');
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
});
test('selected lineage text defers notice removal and conversation changes together', async () => {
  const h = await conversationHarness(), reports = h.get('task-reports'), note = h.get('task-lineage-note');
  h.update(lineageRead(1,conversationThread())); const original = reports.children[0];
  const selection = selectionOver(note,note,note); h.document.selection = selection;
  h.update(lineageRead(0,conversationThread('task-a',['first','reply'])));
  assert.equal(note.hidden, false); assert.equal(reports.children[0], original);
  assert.equal(h.document.selection, selection); assert.equal(h.get('task-conversation-update').hidden, false);
  h.get('task-conversation-update').onclick(); assert.equal(note.hidden, true); assert.equal(note.textContent, '');
  assert.equal(messagesIn(reports.children[0]).length, 2); assert.equal(h.document.activeElement, reports);
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
});
test('room diagnostic changes do not re-render or interrupt when visible notice and conversation are unchanged', async () => {
  const h = await conversationHarness(); h.update(lineageRead(1,conversationThread()));
  const reports = h.get('task-reports'), first = reports.children[0], note = h.get('task-lineage-note'), copy = note.textContent;
  const control = disclosuresIn(first)[0].children[0]; control.focus();
  h.update(lineageRead(17,conversationThread()));
  assert.equal(note.textContent, copy); assert.equal(h.get('task-conversation-update').hidden, true);
  assert.equal(reports.children[0], first); assert.equal(h.document.activeElement, control);
});
test('source loss clears visible or deferred lineage notices and stale task/session/room handlers cannot restore them', async () => {
  for (const change of ['source','room','session','task']) {
    const h = await conversationHarness(), reports = h.get('task-reports'), note = h.get('task-lineage-note');
    h.update(lineageRead(1,conversationThread())); h.document.selection = selectionOver(note,note,note);
    h.update(lineageRead(0,conversationThread())); const stale = h.get('task-conversation-update');
    assert.equal(stale.hidden, false); assert.equal(note.hidden, false);
    if (change === 'source') {
      h.loop.updateChannel({state:'unreadable',remediation:'Source access unavailable',data:{lineage:{unresolved_threads:99}}});
      assert.equal(note.hidden, true); assert.equal(note.textContent, ''); assert.equal(stale.hidden, true);
      assert.match(reports.innerHTML, /Source access unavailable/); stale.onclick(); assert.equal(note.hidden, true);
    } else {
      if (change === 'room') h.loop.setRoom('other'); else if (change === 'session') h.loop.pause();
      else { h.document.selection = null; h.update(conversationRead(conversationThread('task-b'))); h.open('task-b'); }
      await settle(); stale.onclick();
      if (change === 'task') assert.equal(h.get('task-lineage-note').hidden, true);
      else assert.equal(h.get('task-detail-content').innerHTML, '');
    }
    assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
  }
});

test('the all-teams channel notice does not attribute room diagnostics to the selected task team', async () => {
  const h = await conversationHarness(); h.loop.setRoom(null); await settle();
  h.update(lineageRead(1,conversationThread())); h.open(); await settle();
  assert.match(h.get('task-lineage-note').textContent, /recent replies across teams/);
  assert.doesNotMatch(h.get('task-lineage-note').textContent, /in this team|task-a|web|channel window/);
  assert.equal(h.sends.length + h.preparations.length + h.lookups.length, 0);
});
