import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');
const panel = await import(`data:text/javascript;base64,${Buffer.from(await readFile(new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url), 'utf8')).toString('base64')}`);
// Execute the real channel renderer, stopping before unrelated page mounting.
const rendering = source.slice(0, source.indexOf('// WHY a card needs you')).replace(/^import .*;\n/gm, '');
assert.ok(rendering.includes('function threadArticle('));
const channel = { children: [], querySelectorAll() { return this.children; },
  replaceChildren(fragment) { this.children = fragment.children; } };
const workLoop = await readFile(new URL('../claudlobby/plane/ui/work-loop.js', import.meta.url), 'utf8');
const conversationTaskLink = runInNewContext(workLoop.slice(workLoop.indexOf('export function conversationTaskLink('),
  workLoop.indexOf('export function mountWorkLoop(')).replace('export ', '') + '\nconversationTaskLink;');
const api = runInNewContext(`${rendering}\n({threadArticle, nudgeReason, renderChannel})`, {
  ...panel, conversationTaskLink, renderState() { return false; }, currentFleet: 'example', document: {
    documentElement: { style: { setProperty() {} } },
    createElement() { return { dataset: {}, messages: [],
      set innerHTML(value) {
        this.html = value;
        this.messages = [...value.matchAll(/<div class="msg" data-msg-id="([^"]+)">/g)].map(match => {
          const details = { open: false };
          return { dataset: { msgId: match[1] }, querySelector(selector) { return selector === 'details' ? details : null; } };
        });
      },
      get innerHTML() { return this.html || ''; },
      querySelectorAll(selector) { return selector === '.msg[data-msg-id]' ? this.messages : []; },
    }; },
    getElementById() { return channel; },
    createDocumentFragment() { return { children: [], appendChild(node) { this.children.push(node); } }; },
  },
});
const wi = `wi_${'1'.repeat(32)}`, msg = `msg_${'2'.repeat(32)}`;
const wire = value => JSON.stringify(value).replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
function thread(reason = 'Any update?') {
  const body = wire({ assignment_id: null, by: 'human:reviewer', kind: 'task_nudge', reason, task_id: wi });
  return { key: wi, work_item_id: wi, title: 'Review fixture', latest_seq: 4, delivered: true, terminal: null,
    task_events: [{ event: 'nudged', work_item_id: wi, assignment_id: null, ingest_seq: 3,
      detail: JSON.stringify({ reason, by: 'human:reviewer' }) }],
    messages: [{ msg_id: msg, emitter: 'claudlobby.tasks.v1', message_class: 'task_request', command_type: 'query',
      sender_alias: 'human:reviewer', sender_short: 'reviewer', recipient_alias: 'bot:example/lead', recipient_short: 'lead',
      work_item_id: wi, assignment_id: null, ingest_seq: 4, truncated: 0, body, body_words: body,
      occurred_at: '2026-01-01T00:00:00Z', delivery: null, delivery_state: null,
      tx: [{ event: 'pane_submitted', carrier: 'tmux', attempt_no: 1, activated: true }] }] };
}
const html = t => api.threadArticle(t).innerHTML;
const visible = text => text.replace(/<details>[\s\S]*?<\/details>/g, '');

test('only validated task owner metadata enables a main-channel opener, never the dialog copy', () => {
  const t=thread();t.task_link={task_id:wi,fleet:'owner-team',fleet_uid:'fleet_'+'4'.repeat(32),host_uid:'host_'+'5'.repeat(32)};
  const rendered=html(t);
  assert.match(rendered,/View task<\/button>/);assert.ok(rendered.includes(`data-task-open="${wi}"`));
  assert.match(rendered,/data-task-fleet="owner-team"/);assert.match(rendered,/data-task-thread=/);
  assert.doesNotMatch(api.threadArticle(t,false).innerHTML,/data-task-open|View task<\/button>/);
  for(const link of [null,{...t.task_link,task_id:'wi_'+'9'.repeat(32)}, {...t.task_link,fleet:'all'},
    {...t.task_link,host_uid:'unverified host'}, {...t.task_link,fleet_uid:''}, {...t.task_link,host_uid:'elsewhere::'+t.task_link.host_uid}]) {
    assert.doesNotMatch(html({...t,task_link:link}),/data-task-open|View task<\/button>/);
  }
  const qualified={...t,work_item_id:'example::'+wi,task_link:{...t.task_link,
    task_id:'example::'+wi,host_uid:'example::'+t.task_link.host_uid,fleet_uid:'example::'+t.task_link.fleet_uid}};
  assert.match(html(qualified),/data-task-open="example::wi_/);
});

test('a task-owner metadata change replaces a keyed main-channel card even without newer messages', () => {
  const t=thread();api.renderChannel({data:{threads:[t]}});const original=channel.children[0];
  assert.doesNotMatch(original.innerHTML,/data-task-open/);
  t.task_link={task_id:wi,fleet:'owner-team',fleet_uid:'fleet_'+'4'.repeat(32),host_uid:'host_'+'5'.repeat(32)};
  api.renderChannel({data:{threads:[t]}});const linked=channel.children[0];
  assert.notEqual(linked,original);assert.match(linked.innerHTML,/data-task-fleet="owner-team"/);
  api.renderChannel({data:{threads:[t]}});assert.equal(channel.children[0],linked);
  t.task_link=null;api.renderChannel({data:{threads:[t]}});assert.doesNotMatch(channel.children[0].innerHTML,/data-task-open/);
});

test('genuine paired nudge renders authored reason, accurate purpose and full raw machinery', () => {
  const t = thread('  Please check\nthis task — thanks.  '), rendered = html(t);
  assert.match(visible(rendered), /task update request/);
  assert.match(visible(rendered), /  Please check\nthis task — thanks\.  /);
  assert.doesNotMatch(visible(rendered), /task_nudge|dispatched|working…|delivered|approved/);
  assert.match(rendered, /<details><summary>machinery/);
  assert.ok(rendered.includes(panel.esc(t.messages[0].body)));
  assert.match(rendered, new RegExp(msg));
});

test('an assigned task uses its exact original assignment link and a plain title fallback', () => {
  const t = thread('Check this assigned task'), assignment = `asg_${'3'.repeat(32)}`;
  t.title = null; t.messages[0].assignment_id = assignment; t.task_events[0].assignment_id = assignment;
  const body = JSON.parse(t.messages[0].body); body.assignment_id = assignment;
  t.messages[0].body = t.messages[0].body_words = wire(body);
  assert.equal(api.nudgeReason(t.messages[0], t), body.reason);
  assert.match(visible(html(t)), /t-title">Check this assigned task</);
  assert.ok(html(t).includes(assignment));
  assert.doesNotMatch(visible(html(t)), /dispatched|working…/);
});

test('only receiver delivery verdict confirms receipt; submission remains unconfirmed', () => {
  for (const status of [null, 'unconfirmed', 'altered', 'truncated', 'unknown']) {
    const t = thread(); t.messages[0].delivery = status;
    assert.match(visible(html(t)), /Delivery to the lead is unconfirmed\./);
    assert.doesNotMatch(visible(html(t)), /The lead received/);
  }
  const t = thread(); t.messages[0].delivery = 'delivered'; t.messages[0].delivery_state = 'confirmed by the receiver';
  assert.match(visible(html(t)), /The lead received this update request\./);
  assert.match(html(t), /receipt confirmed by the receiver/);
});

test('failed transmission remains a visible failure and receiver proof takes precedence', () => {
  const t = thread(); t.messages[0].tx[0].event = 'failed';
  assert.match(visible(html(t)), /delivery bad.*failed to reach the lead/);
  t.messages[0].delivery = 'delivered';
  assert.match(visible(html(t)), /delivery ok.*The lead received/);
  for (const [delivery, warning] of [['altered', 'arrived altered'], ['truncated', 'arrived short']]) {
    t.messages[0].delivery = delivery;
    const rendered = visible(html(t));
    assert.match(rendered, new RegExp(`delivery bad.*${warning}.*Delivery to the lead is unconfirmed`));
    assert.doesNotMatch(rendered, /failed to reach|The lead received/);
  }
});

test('receipt-only refresh updates the card, then reuses it when evidence is unchanged', () => {
  const t = thread();
  const otherMsg = `msg_${'3'.repeat(32)}`;
  t.messages.push({ ...t.messages[0], msg_id: otherMsg });
  api.renderChannel({ data: { threads: [t] } });
  const first = channel.children[0];
  const disclosure = (article, id) => article.querySelectorAll('.msg[data-msg-id]')
    .find(message => message.dataset.msgId === id).querySelector('details');
  disclosure(first, msg).open = true;
  assert.match(first.innerHTML, /Delivery to the lead is unconfirmed/);
  api.renderChannel({ data: { threads: [t] } });
  assert.equal(channel.children[0], first);
  t.messages[0].delivery = 'delivered';
  t.messages[0].delivery_state = 'confirmed by receiver';
  t.messages.reverse(); // Open state follows message identity, not row position.
  api.renderChannel({ data: { threads: [t] } });
  const confirmed = channel.children[0];
  assert.notEqual(confirmed, first);
  assert.equal(confirmed.dataset.seq, first.dataset.seq);
  assert.equal(disclosure(confirmed, msg).open, true);
  assert.equal(disclosure(confirmed, otherMsg).open, false);
  assert.match(confirmed.innerHTML, /The lead received this update request/);
  api.renderChannel({ data: { threads: [t] } });
  assert.equal(channel.children[0], confirmed);
  disclosure(confirmed, msg).open = false;
  const changed = t.messages.find(message => message.msg_id === msg);
  changed.delivery = null; changed.delivery_state = null;
  changed.tx[0].event = 'failed';
  api.renderChannel({ data: { threads: [t] } });
  assert.match(channel.children[0].innerHTML, /failed to reach the lead/);
  assert.equal(disclosure(channel.children[0], msg).open, false);
});

test('maximum bounded escaped reason stays readable, malformed string boundaries stay literal', () => {
  const t = thread('漢'.repeat(16384));
  assert.equal(api.nudgeReason(t.messages[0], t), '漢'.repeat(16384));
  const bad = thread('\udc00');
  const body = JSON.parse(bad.messages[0].body); body.by = 'human:\ud800';
  bad.messages[0].body = wire(body);
  bad.task_events[0].detail = JSON.stringify({ by: body.by, reason: body.reason });
  assert.equal(api.nudgeReason(bad.messages[0], bad), null);
});

test('ordinary JSON text cannot masquerade as a task nudge even with a matching event', () => {
  for (const change of [{ message_class: 'chat' }, { command_type: 'task' }, { emitter: 'other' }]) {
    const t = thread(); Object.assign(t.messages[0], change);
    assert.equal(api.nudgeReason(t.messages[0], t), null);
    assert.ok(visible(html(t)).includes(panel.esc(t.messages[0].body)));
  }
});

test('missing, malformed, ambiguous and wrong linked task facts fall back to literal text', () => {
  const cases = [t => t.task_events = [], t => t.task_events[0].detail = '{broken',
    t => t.task_events.push({ ...t.task_events[0] }), t => t.task_events[0].work_item_id = `wi_${'9'.repeat(32)}`,
    t => t.task_events[0].assignment_id = `asg_${'9'.repeat(32)}`, t => t.task_events[0].ingest_seq--,
    t => t.task_events[0].detail = JSON.stringify({ by: 'human:other', reason: 'Any update?' }),
    t => t.messages[0].truncated = 1, t => delete t.messages[0].assignment_id,
    t => t.messages[0].body = '{broken', t => t.messages[0].work_item_id = 'legacy-task',
    t => t.messages[0].body = t.messages[0].body.replace('Any update?', '\\ud800'),
    t => t.messages[0].body = t.messages[0].body.replace('"kind":', '"kind":"chat","kind":')];
  for (const change of cases) {
    const t = thread(); change(t);
    t.messages[0].body_words = t.messages[0].body;
    assert.equal(api.nudgeReason(t.messages[0], t), null);
    const body = t.messages[0].body_words || t.messages[0].body;
    assert.ok(visible(html(t)).includes(panel.esc(body)));
  }
});

test('unsafe authored text is escaped in prose, title fallback and raw evidence', () => {
  const t = thread('<img src=x onerror=alert(1)>\n<script>bad()</script>'); t.title = null;
  const rendered = html(t);
  assert.doesNotMatch(rendered, /<img|<script>/);
  assert.match(rendered, /&lt;img/); assert.match(rendered, /&lt;script&gt;/);
});

test('mixed task history shows recorded acts without deriving work from nudge delivery', () => {
  const t = thread(); t.task_events.unshift({ event: 'accepted' }, { event: 'progress' }, { event: 'progress' });
  t.task_events.push({ event: 'completed' }); t.terminal = 'completed';
  assert.match(visible(html(t)), /Task history:.*accepted.*progress.*completed/);
  assert.doesNotMatch(visible(html(t)), /dispatched|working…|>delivered</);
  assert.equal((visible(html(t)).match(/>progress</g) || []).length, 1);
});

test('task-linked chat receipt never implies assignment or work has started', () => {
  const t = thread(); Object.assign(t.messages[0], { message_class: 'chat', body: 'Ordinary <text>', body_words: 'Ordinary <text>',
    delivery: 'delivered', delivery_state: 'confirmed by the receiver' });
  const rendered = visible(html(t));
  assert.match(rendered, /Ordinary &lt;text&gt;/); assert.match(rendered, /confirmed by the receiver/);
  assert.doesNotMatch(rendered, /dispatched|working…|>delivered</);
  t.task_events = [{ event: 'accepted' }, { event: 'progress' }, { event: 'completed' }];
  assert.match(visible(html(t)), /Task history:.*accepted.*progress.*completed/);
});

test('captured parent-only answer on a queued task never invents dispatch or work', () => {
  // Shape captured in the 2026-10-10 synthetic browser run after its comment
  // parent left the 120-message window: answer, no direct task/assignment,
  // no command or transmission, and no task acts. IDs and prose are synthetic.
  const t = thread(); t.delivered = false; t.task_events = [];
  Object.assign(t.messages[0], { emitter: 'synthetic-reply-lineage-browser', message_class: 'answer',
    command_type: null, work_item_id: null, assignment_id: null, reply_to_msg_id: `msg_${'3'.repeat(32)}`,
    body: 'Synthetic answer <reply> & no task progress.', body_words: 'Synthetic answer <reply> & no task progress.',
    delivery: null, delivery_state: null, tx: [] });
  let rendered = visible(html(t));
  assert.match(rendered, /Synthetic answer &lt;reply&gt; &amp; no task progress/);
  assert.doesNotMatch(rendered, /t-ladder|dispatched|working…|>delivered</);
  // A later receipt proves this answer only, even if the thread is stamped delivered.
  t.delivered = true; t.messages[0].delivery = 'delivered';
  t.messages[0].delivery_state = 'confirmed by the receiver';
  rendered = visible(html(t));
  assert.match(rendered, /confirmed by the receiver/);
  assert.doesNotMatch(rendered, /t-ladder|dispatched|working…|>delivered</);
});

test('task-linked messages without a task dispatch render only recorded task history', () => {
  for (const message_class of ['report', 'question', 'notice', 'briefing', 'acknowledgement', 'task_request']) {
    const t = thread(); t.task_events = [];
    Object.assign(t.messages[0], { message_class, command_type: 'query',
      body: 'Recorded conversation', body_words: 'Recorded conversation',
      delivery: 'delivered', delivery_state: 'confirmed by the receiver' });
    assert.doesNotMatch(visible(html(t)), /t-ladder|dispatched|working…|>delivered</, message_class);
    t.task_events = [{ event: 'accepted' }, { event: 'progress' }, { event: 'completed' }];
    t.terminal = 'completed';
    const rendered = visible(html(t));
    assert.match(rendered, /Task history:.*accepted.*progress.*completed/, message_class);
    assert.match(rendered, /confirmed by the receiver/, message_class);
    assert.doesNotMatch(rendered, /dispatched|working…|>delivered</, message_class);
  }
});

test('ordinary task dispatch preserves its ladder alongside a recent answer', () => {
  const t = thread();
  Object.assign(t.messages[0], { command_type: 'task', assignment_id: `asg_${'3'.repeat(32)}`,
    body: 'Do the assigned work', body_words: 'Do the assigned work',
    delivery: 'delivered', delivery_state: 'confirmed by the receiver' });
  t.messages.push({ ...t.messages[0], msg_id: `msg_${'4'.repeat(32)}`, message_class: 'answer',
    command_type: null, body: 'Reply only', body_words: 'Reply only', tx: [] });
  t.task_events = [{ event: 'accepted' }, { event: 'progress' }];
  let rendered = visible(html(t));
  assert.match(rendered, /step done [^"]*">dispatched/);
  assert.match(rendered, /step done [^"]*">delivered/);
  assert.match(rendered, />accepted<.*>progress<.*>working…</s);
  assert.doesNotMatch(rendered, /Task history:/);
  t.terminal = 'completed'; t.task_events.push({ event: 'completed' });
  rendered = visible(html(t));
  assert.match(rendered, /step done [^"]*">completed/);
  assert.doesNotMatch(rendered, /working…/);
});

test('a received reply cannot confirm an unconfirmed task dispatch', () => {
  const t = thread(); t.task_events = [];
  Object.assign(t.messages[0], { command_type: 'task', assignment_id: `asg_${'3'.repeat(32)}`,
    body: 'Do the assigned work', body_words: 'Do the assigned work',
    delivery: 'unconfirmed', delivery_state: 'delivery unconfirmed' });
  t.messages.push({ ...t.messages[0], msg_id: `msg_${'4'.repeat(32)}`, message_class: 'answer',
    command_type: null, body: 'Received reply', body_words: 'Received reply',
    delivery: 'delivered', delivery_state: 'confirmed by the receiver',
    tx: [{ event: 'pane_submitted', activated: true, carrier: 'tmux', attempt_no: 1 }] });
  assert.equal(t.delivered, true); // Aggregate thread delivery belongs to the reply.
  const rendered = visible(html(t));
  assert.match(rendered, />dispatched</);
  assert.match(rendered, /delivery unconfirmed/);
  assert.match(rendered, /Received reply.*confirmed by the receiver/s);
  assert.doesNotMatch(rendered, />delivered<|working…/);
  t.task_events = [{ event: 'accepted' }, { event: 'progress' }];
  assert.match(visible(html(t)), />accepted<.*>progress</s);
  assert.match(visible(html(t)), /working…/); // Recorded work is independent of delivery.
  assert.doesNotMatch(visible(html(t)), />delivered</);
});

test('confirmed task dispatch without recorded work never claims working', () => {
  const t = thread(); t.task_events = [];
  Object.assign(t.messages[0], { command_type: 'task', assignment_id: `asg_${'3'.repeat(32)}`,
    body: 'Do the assigned work', body_words: 'Do the assigned work',
    delivery: 'delivered', delivery_state: 'confirmed by the receiver' });
  const rendered = visible(html(t));
  assert.match(rendered, />dispatched<.*>delivered</s);
  assert.doesNotMatch(rendered, /working…|>accepted<|>progress</);
});

if (process.env.PLANE_FEEDBACK_FIXTURE) test('canonical queued feedback receipt stays separate from task progress', async () => {
  const t = JSON.parse(await readFile(process.env.PLANE_FEEDBACK_FIXTURE, 'utf8'));
  assert.equal(t.delivered, true); // Canonical thread delivery includes the comment.
  assert.equal(t.messages[0].message_class, 'chat');
  assert.equal(t.messages[0].assignment_id, null);
  const rendered = visible(html(t));
  assert.match(rendered, /Consider &lt;this&gt; next\./);
  assert.doesNotMatch(rendered, /dispatched|working…|>delivered</);
});

if (process.env.PLANE_ACTIVITY_FIXTURE) test('actual canonical query projection renders the paired nudge', async () => {
  const t = JSON.parse(await readFile(process.env.PLANE_ACTIVITY_FIXTURE, 'utf8'));
  assert.equal(api.nudgeReason(t.messages[0], t), 'Check\nfixture <status> — please.');
  assert.match(visible(html(t)), /Check\nfixture &lt;status&gt; — please\./);
  assert.doesNotMatch(visible(html(t)), /task_nudge|dispatched|working…/);
});
