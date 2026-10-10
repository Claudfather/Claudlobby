import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { ActionState } from '../claudlobby/plane/ui/action-state.js';

// Exercise the real controller without a browser dependency. This small DOM
// double models only the IDs, delegated buttons, focus and dialog close events
// used here. Browser rendering and native keyboard behavior remain browser QA.
const source = await readFile(new URL('../claudlobby/plane/ui/work-loop.js', import.meta.url), 'utf8');
const controller = source.replaceAll('from "/action-state.js"', `from "${new URL('../claudlobby/plane/ui/action-state.js', import.meta.url)}"`)
  .replaceAll('from "/panel-state.js"', `from "${new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url)}"`);
const { mountWorkLoop } = await import(`data:text/javascript;base64,${Buffer.from(controller).toString('base64')}`);
const context = { version: 1, room: 'web', simulation: true,
  scope: { workspace: 'example', host: 'workshop', fleet: 'web', viewer: 'owner' },
  recipients: [{ id: 'lead', label: 'Lead', lead: true }, { id: 'worker', label: 'Worker' }],
  actions: ['message', 'feedback', 'nudge'] };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
const receipt = (request, status = 'delivered') => ({ ...request, version: 1, status });
function store() { const values = new Map(); return { getItem: k => values.get(k) || null, setItem: (k, v) => values.set(k, v) }; }
function dom() {
  const elements = new Map();
  const document = { activeElement: null, getElementById: id => elements.get(id) };
  class Element {
    constructor(id = '', dataset = {}) {
      this.id = id; this.dataset = dataset; this.isConnected = true;
      this.hidden = false; this.disabled = false; this.value = ''; this.children = []; this.listeners = new Map();
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
      return this.children.filter(child => child.isConnected && key in child.dataset);
    }
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
  const sends = [], lookups = [];
  const api = {
    interactionContext: options.interactionContext || (() => context),
    sendAction: request => { sends.push(request); return options.sendAction ? options.sendAction(request) : receipt(request); },
    actionReceipt: request => { lookups.push(request); return options.actionReceipt ? options.actionReceipt(request) : receipt(request); },
  };
  const loop = mountWorkLoop({ api, renderThread: () => new ui.Element(), refresh: () => {} });
  const board = { state: 'ok', data: { tasks: ['task-a', 'task-b'].map(task_id => ({ task_id, fleet: 'web', title: task_id })) } };
  const update = () => loop.update(board, { state: 'ok', data: { threads: [] } });
  const open = (id = 'task-a', fleet = 'web', panel = 'tasks') => {
    const button = new ui.Element('', { taskOpen: id, taskFleet: fleet });
    button.panel = ui.get(panel); button.panel.children = [button];
    ui.get('rail-right').children = [button];
    ui.get('rail-right').emit('click', { target: button }); return button;
  };
  const pendingClick = (id, discard = false) => ui.get('work-pending').onclick({ target: new ui.Element('', { [discard ? 'discard' : 'request']: id }) });
  const submit = () => { ui.get('work-body').value = 'Hello'; ui.get('work-body').emit('input'); return ui.get('work-form').onsubmit({ preventDefault() {} }); };
  return { ...ui, loop, storage, sends, lookups, confirmations, update, open, pendingClick, submit };
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
  assert.match(h.get('work-notice').textContent, /message to lead:.*delivery confirmed/);
  assert.equal(h.get('work-body').value, 'Worker draft');
  assert.equal(new ActionState(storage).pending.length, 0);
});
test('receipt notice names its original task even if a different task was selected before checking', async () => {
  const storage = store(), original = saved(storage, 'task-a'), h = harness({ storage });
  h.loop.setRoom('web'); await settle(); h.update(); h.open('task-b');
  h.get('task-detail-content').querySelectorAll('[data-kind]')[0].onclick(); await settle();
  await h.pendingClick(original.request_id);
  assert.match(h.get('work-notice').textContent, /message to lead · task task-a:/);
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
  assert.match(h.get('work-notice').textContent, /message to lead:.*delivery confirmed/);
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
    assert.match(h.get('work-notice').textContent, /message to lead.*request refused/);
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
  assert.match(h.get('work-notice').textContent, /message to lead:.*delivery confirmed/);
  assert.equal(new ActionState(h.storage).pending.length, 0);
  assert.equal(h.sends.length, 1);
});
