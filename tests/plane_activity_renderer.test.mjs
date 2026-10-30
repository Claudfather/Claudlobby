import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../claudlobby/plane/ui/app.js', import.meta.url), 'utf8');

test('roster message entry names the exact bot and remains disabled until existing grant admission',()=>{
  const c={Date,esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),ago:()=> 'recently'};
  runInNewContext(source.slice(source.indexOf('function railRow('),source.indexOf('// Selection uses only')),c);
  const html=c.railRow({uid:'north::actor_'+'a'.repeat(32),alias:'north::bot:team/worker',short:'Worker <one>',fleet:'north / team',kind:'actor',provisional:false});
  assert.match(html,/data-bot-message="north::actor_/);assert.match(html,/disabled>Message Worker &lt;one&gt;/);
  assert.match(html,/aria-label="Message Worker &lt;one&gt; in north \/ team"/);
});
test('connected directory separates recorded activity from live evidence and opens only the exact current team',()=>{
  const a={uid:'north::actor_'+'a'.repeat(32),alias:'north::bot:team/worker',short:'Worker <one>',fleet:'north / team',kind:'actor',last_seen:'2026-01-01',provisional:false};
  const b={...a,uid:a.uid.replace('north::','south::'),alias:a.alias.replace('north::','south::'),fleet:'south / team',provisional:true};
  const fleet={alias:a.fleet,uid:'north::fleet_'+'b'.repeat(32),display_label:'North / team'},opened=[];
  const grid={innerHTML:'',querySelectorAll:()=>[]},refresh={addEventListener(){}},roster={scrollIntoView(){}};
  const c={interactionApi:{dispose(){}},currentView:'grid',sessionPaused:false,sessionEpoch:1,rosterGeneration:1,
    document:{activeElement:null},$:id=>id==='grid'?grid:id==='fleet'?roster:refresh,
    fleets:[fleet],rosterIdentities:new Map([[a.uid,{...a,fleet_uid:fleet.uid}],[b.uid,b]]),
    esc:v=>String(v??'').replaceAll('<','&lt;').replaceAll('>','&gt;'),ago:()=> 'recently',
    PRESENCE_WORD:{working:'working',idle:'idle',unknown:'unknown'},
    setView:view=>{opened.push(view);},pickFleet:room=>opened.push(room),renderState(){}};
  runInNewContext(source.slice(source.indexOf('let agentPresence ='),source.indexOf('const STATUS_DOT')),c);
  const presence={data:{sources:[{key:'north',state:'ok',sampler_available:true}],
    bots:[{alias:a.alias,live:'sampling',presence:'working'}]}};
  c.renderAgentDirectory({state:'ok',data:{identities:[a,b,{...a,kind:'actor',fleet:null}],sources:[{key:'north',label:'North',state:'ok'},{key:'south',label:'South',state:'ok'}]}},presence);
  assert.match(grid.innerHTML,/2 agents in readable records/);assert.match(grid.innerHTML,/200 recorded identities/);
  assert.match(grid.innerHTML,/Worker &lt;one&gt;/);assert.match(grid.innerHTML,/Live session<\/dt><dd>unknown/);
  assert.match(grid.innerHTML,/Recorded heartbeat<\/dt><dd>working/);
  assert.match(grid.innerHTML,/Unconfirmed identity/);assert.doesNotMatch(grid.innerHTML,/running|pane-card|<pre/);
  c.renderAgentDirectory({state:'ok',data:{identities:[a],sources:[{key:'north',label:'North',state:'ok'},{key:'south',label:'South',state:'not_connected'}]}},presence);
  assert.match(grid.innerHTML,/South: not_connected/);assert.doesNotMatch(grid.innerHTML,/data-agent-team="south::/);
  c.openAgentTeam(a.uid);assert.deepEqual(opened,['channel',a.fleet]);opened.length=0;
  c.openAgentTeam(b.uid);c.rosterIdentities.get(a.uid).fleet_uid='changed';c.openAgentTeam(a.uid);
  c.sessionPaused=true;c.openAgentTeam(a.uid);assert.equal(opened.length,0);
});
const panel = await import(`data:text/javascript;base64,${Buffer.from(await readFile(new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url), 'utf8')).toString('base64')}`);
// Execute the real channel renderer, stopping before unrelated page mounting.
const rendering = source.slice(0, source.indexOf('// WHY a card needs you')).replace(/^import .*;\n/gm, '');
assert.ok(rendering.includes('function threadArticle('));
const channel = { children: [], querySelectorAll() { return this.children; },
  replaceChildren(fragment) { this.children = fragment.children; } };
const workLoop = await readFile(new URL('../claudlobby/plane/ui/work-loop.js', import.meta.url), 'utf8');
const controller = workLoop.replaceAll('from "./action-state.js"', `from "${new URL('../claudlobby/plane/ui/action-state.js', import.meta.url)}"`)
  .replaceAll('from "./permission-panel.js"', `from "${new URL('../claudlobby/plane/ui/permission-panel.js', import.meta.url)}"`)
  .replaceAll('from "./panel-state.js"', `from "${new URL('../claudlobby/plane/ui/panel-state.js', import.meta.url)}"`);
const {conversationTaskLink} = await import(`data:text/javascript;base64,${Buffer.from(controller).toString('base64')}`);
const recipients = new Map(), inspected = [];
const api = runInNewContext(`${rendering}\n({threadArticle, nudgeReason, renderChannel, checkinArticle, pause(value) { sessionPaused=value; }})`, {
  rosterIdentities: recipients, sessionPaused:false, inspectBot(identity) { inspected.push(identity); },
  URL, ...panel, conversationTaskLink, renderState() { return false; }, currentFleet: 'example', document: {
    documentElement: { style: { setProperty() {} } },
    createElement() { const disclosure = {open:false}; return { dataset: {}, messages: [], buttons: [],
      set innerHTML(value) {
        this.html = value;
        disclosure.open = value.includes('<details open>');
        this.buttons = [...value.matchAll(/data-checkin-bot="([^"]+)"\s+data-checkin-alias="([^"]+)" data-checkin-fleet="([^"]+)"/g)]
          .map(match=>({dataset:{checkinBot:match[1],checkinAlias:match[2],checkinFleet:match[3]},isConnected:true}));
        this.messages = [...value.matchAll(/<div class="msg" data-msg-id="([^"]+)">/g)].map(match => {
          const details = { open: false };
          return { dataset: { msgId: match[1] }, querySelector(selector) { return selector === 'details' ? details : null; } };
        });
      },
      get innerHTML() { return this.html || ''; },
      querySelector(selector) { return selector === 'details' ? disclosure : null; },
      querySelectorAll(selector) { return selector === '.msg[data-msg-id]' ? this.messages : selector === '[data-checkin-bot]' ? this.buttons : []; },
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

for (const outcome of ['healthy','unavailable','mixed','room','session','input','inflight','scheduled','stale','stale-epoch','partial-denied','partial-not_connected'])
test(`explicit activity recovery reuses guarded reads and preserves keyboard focus: ${outcome}`,async()=>{
  const document={activeElement:null,documentElement:{style:{setProperty(){}}}};
  class Node {
    constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.hidden=false;this._html='';}
    get isConnected(){return this===channel || !!this.parent?.isConnected;}
    contains(node){return node===this || this.children.some(c=>c.contains?.(node));}
    get innerHTML(){return this._html;}
    set innerHTML(value){this.replaceChildren();this._html=value;}
    replaceChildren(...nodes){for(const child of this.children){child.parent=null;if(child.contains(document.activeElement))document.activeElement=null;}this.children=[];this.append(...nodes);}
    append(...nodes){for(const node of nodes){if(node.tagName==='fragment'){this.append(...node.children);continue;}node.parent=this;this.children.push(node);}}
    appendChild(node){this.append(node);}
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}
    setAttribute(k,v){this.attributes[k]=v;}
    closest(){return this.hidden?this:this.parent?.closest()||null;}
    focus(){if(this.isConnected&&!this.closest())document.activeElement=this;}
    querySelectorAll(selector){return selector==='article.thread'?this.children.filter(n=>n.tagName==='article'):[];}
    querySelector(){return null;}
  }
  const channel=new Node(),focusedInput={};
  document.getElementById=()=>channel;document.createElement=tag=>new Node(tag);document.createDocumentFragment=()=>new Node('fragment');
  let resolve;const held=new Promise(r=>{resolve=r;}),reads=[],queued=[];
  const state={...panel,document,conversationTaskLink,sessionPaused:false,sessionEpoch:1,generation:0,currentFleet:'north / team',
    refreshInFlight:false,refreshPending:false,refreshTimer:null,fleetsSeen:true,clearTimeout(){},
    syncWorkRoom(){},fleetQuery:()=>'?fleet='+encodeURIComponent(state.currentFleet),channelUrl(){return '/api/channel?limit=120';},refreshOverview(){},
    agentDirectoryVisible:()=>false,
    jget(url){reads.push(url);return url.startsWith('/api/channel')?held:Promise.resolve({state:'ok',data:{}});},
    renderFleet(){},renderTasks(){},renderSummary(){},renderFleetTabs(){},restartSafety(){},scheduleRefresh(){queued.push('refresh');},workLoop:{update(){},updateChannel(){}}};
  const retry=source.slice(source.indexOf('async function retryActivity()'),source.indexOf('async function refreshBoards()'));
  const refresh=source.slice(source.indexOf('async function refreshBoards()'),source.indexOf('function scheduleRefresh()'));
  const view=runInNewContext(`${rendering}\n${retry}\n${refresh}\n({renderChannel,retryActivity,refreshBoards,control:()=>activityRecovery})`,state);
  if(outcome.startsWith('partial-')) {
    view.renderChannel({state:'ok',data:{threads:[],coverage:{partial:true},sources:[{state:'ok',connection_state:'ready'},{state:'unavailable',connection_state:outcome.slice(8)}]}});
    assert.equal(view.control(),null);assert.ok(channel.children.some(node=>node.textContent?.includes('No activity in readable sources.')));return;
  }
  view.renderChannel({state:'unavailable',remediation:'This panel timed out.'});
  const button=view.control().button;assert.equal(button.textContent,'Retry activity');assert.equal(button.isConnected,true);
  if(outcome.startsWith('stale')){if(outcome==='stale')state.currentFleet='other';else state.sessionEpoch++;await view.retryActivity();assert.equal(reads.length,0);return;}
  const existing=outcome==='inflight'?view.refreshBoards():null;
  if(outcome==='scheduled')state.refreshTimer=17;
  button.focus();let reading=view.retryActivity();assert.equal(button.attributes['aria-disabled'],'true');
  assert.equal(button.textContent,'Refreshing reads…');
  if(existing)reading=existing;
  if(outcome==='scheduled'){assert.equal(reads.length,0);reading=view.refreshBoards();}
  await view.retryActivity();await view.retryActivity();assert.equal(reads.length,4);assert.equal(queued.length,0);
  assert.equal(document.activeElement,button,'busy uses aria-disabled without dropping keyboard focus');
  if(outcome==='room'){state.currentFleet='south / team';state.generation++;channel.innerHTML='Other room';}
  if(outcome==='session'){state.sessionPaused=true;state.sessionEpoch++;state.generation++;view.control().paint();}
  if(outcome==='input')document.activeElement=focusedInput;
  const healthy={state:'ok',data:{threads:[]}};
  const response=outcome==='unavailable'?{state:'unavailable'}:outcome==='mixed'
    ?{state:'ok',data:{threads:[thread()],coverage:{partial:true},sources:[{state:'ok',connection_state:'ready'},{state:'unavailable',connection_state:'ready'}]}}:healthy;
  resolve(response);await reading;
  assert.equal(reads.length,4);assert.equal(queued.length,0);
  if(outcome==='healthy') {assert.equal(button.isConnected,false);assert.equal(document.activeElement,channel);assert.equal(channel.tabIndex,-1);}
  if(['unavailable','mixed'].includes(outcome)){assert.equal(button.isConnected,true);assert.equal(button.textContent,'Retry activity');assert.equal(document.activeElement,button);}
  if(outcome==='mixed')assert.ok(channel.children.some(n=>n.tagName==='article'),'healthy activity remains visible');
  if(outcome==='input')assert.equal(document.activeElement,focusedInput);
  if(outcome==='room')assert.equal(channel.innerHTML,'Other room','late rows cannot repaint another room');
  if(outcome==='session'){assert.equal(view.control().root.hidden,true);assert.equal(button.attributes['aria-disabled'],'false');}
});

test('activity recovery does not offer retries for paused, denied or unconnected sources',()=>{
  const document={documentElement:{style:{setProperty(){}}},getElementById:()=>({innerHTML:'',focus(){throw Error('unexpected focus');}})};
  const view=runInNewContext(`${rendering}\n({renderChannel,control:()=>activityRecovery,pause(){sessionPaused=true;}})`,{...panel,document,sessionPaused:false,conversationTaskLink});
  for(const state of ['denied','not_connected','disconnected']){
    view.renderChannel({state:'unavailable',data:{sources:[{state:'unavailable',connection_state:state}]}});
    assert.equal(view.control(),null);
  }
  view.renderChannel({state:'denied'});assert.equal(view.control(),null);
  view.pause();view.renderChannel({state:'unavailable'});assert.equal(view.control(),null);
});

test('check-ins show recorded requests and reuse current bot inspection without claiming resolution', () => {
  const identity={uid:'actor_'+'7'.repeat(32),alias:'bot:example/lead',fleet:'example',provisional:false};
  const row={actor_uid:identity.uid,actor_alias:identity.alias,actor_short:'lead',fleet:identity.fleet,
    action:'ask',reason:'Choose <one> & explain',rationale:'A decision is needed.',occurred_at:'2026-01-01T00:00:00Z'};
  recipients.set(identity.uid,identity);
  const envelope={data:{threads:[],checkins:[row],checkins_state:'ok',checkins_truncated:true}};
  api.renderChannel(envelope);
  const article=channel.children[0],button=article.buttons[0];
  assert.match(article.innerHTML,/Asked for your input/);
  assert.match(article.innerHTML,/Choose &lt;one&gt; &amp; explain/);
  assert.match(article.innerHTML,/not a list of unanswered requests/);
  assert.match(article.innerHTML,/Older decisions remain/);
  assert.equal(button.disabled,false);button.onclick();assert.equal(inspected.at(-1),identity);
  article.querySelector('details').open=false;
  api.renderChannel(envelope);assert.equal(channel.children[0],article);
  assert.equal(article.querySelector('details').open,false);
  const count=inspected.length;
  button.disabled=true;button.onclick();assert.equal(inspected.length,count);button.disabled=false;
  recipients.set(identity.uid,{...identity,alias:'bot:example/replacement'});
  button.onclick();assert.equal(inspected.length,count);
  recipients.set(identity.uid,identity);api.pause(true);button.onclick();assert.equal(inspected.length,count);api.pause(false);
  api.renderChannel({data:{threads:[],checkins:[],checkins_state:'unavailable'}});
  assert.match(channel.children[0].innerHTML,/history is unavailable/);
  recipients.clear();channel.children=[];
});

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
  assert.match(html({...qualified,task_link:{...qualified.task_link,
    fleet_uid:t.task_link.fleet_uid,host_uid:t.task_link.host_uid}}),/data-task-open="example::wi_/);
  for(const field of ['fleet_uid','host_uid'])
    assert.doesNotMatch(html({...qualified,task_link:{...qualified.task_link,[field]:'foreign::'+t.task_link[field]}}),/data-task-open/);
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

function reportedThread(overrides = {}) {
  const t = thread();t.title = null;t.work_item_id = null;t.task_events = [];
  Object.assign(t.messages[0], {message_class:'report', command_type:null, work_item_id:null,
    body:'{"format":"claudlobby.report","summary":"Ready <please>"}', body_words:'Ready <please>',
    report:{state:'captured',status:'completed',percent:null,
      pr_url:'https://github.com/example/project/pull/1',pr_role:'reviewed',
      artifacts:['https://example.org/result?a=1&b=2'],issues:[],skill:null}, ...overrides});
  return t;
}

test('structured report shows readable results and reported evidence, with raw content in machinery', () => {
  const t = reportedThread(), rendered = html(t), prose = visible(rendered);
  assert.match(prose,/Completion reported/);assert.match(prose,/Ready &lt;please&gt;/);
  assert.match(prose,/PR reviewed \(reported\)/);
  assert.match(prose,/href="https:\/\/example.org\/result\?a=1&amp;b=2" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(prose,/claudlobby.report|approved|verified|dispatched/);
  assert.match(rendered,/machinery raw[^>]*>.*claudlobby.report/);
  assert.equal(api.threadArticle(t,false).innerHTML,rendered);
  t.messages[0].report.status='progress';t.messages[0].report.percent=0;
  assert.match(visible(html(t)),/Progress reported · 0%/);
  t.messages[0].report.status='blocked';t.messages[0].report.percent=null;
  t.messages[0].body_words='Need your decision';
  assert.match(visible(html(t)),/Blocker reported/);assert.match(visible(html(t)),/Need your decision/);
});

test('report evidence never activates unsafe or credential-bearing URLs', () => {
  const t=reportedThread();t.messages[0].report.pr_url=null;
  const unsafe=['javascript:alert(1)','data:text/html,<script>bad</script>','file:///tmp/report',
    'https://user:secret@example.org/result','s3://example/result','https:example.org/result'];
  t.messages[0].report.artifacts=unsafe;
  const rendered=visible(html(t));
  assert.doesNotMatch(rendered,/<a |<script>/);
  for (const value of unsafe) assert.ok(rendered.includes(panel.esc(value)));
});

test('unavailable report content does not become status or evidence, legacy remains prose', () => {
  for (const state of ['withheld','truncated','invalid','unsupported']) {
    const t=reportedThread({report:{state}}), rendered=html(t);
    assert.doesNotMatch(visible(rendered),/Ready|claudlobby.report|Completion reported|<a /);
    assert.match(visible(rendered),/Report unavailable from/);
    assert.match(rendered,/machinery raw/);
  }
  assert.match(visible(html(reportedThread({report:{state:'legacy'},body_words:'Historical report'}))),/Historical report/);
  assert.doesNotMatch(visible(html(reportedThread({report:undefined}))),/Completion reported|PR reviewed/);
});


test('permission attention opens the existing task and clears after a fresh decided read', () => {
  const boardSource = source.slice(source.indexOf('// WHY a card needs you'), source.indexOf('function railRow('));
  const nodes = Object.fromEntries(['attention', 'tasks', 'attn-count'].map(id => [id, {innerHTML: '', hidden: false}]));
  const board = runInNewContext(`${rendering}\n${boardSource}\n({renderTasks})`, {
    ...panel, renderState: () => false, document: {
      getElementById: id => nodes[id], documentElement: {style: {setProperty() {}}},
    },
  });
  const row = {task_id: 'workshop::' + wi, fleet: 'web', title: 'Release signup', state: 'queued', resolved: true,
    attention: true, attention_reason: ['permission_pending'], permission_pending: 2};
  board.renderTasks({data: {tasks: [row]}});
  assert.match(nodes.attention.innerHTML, /2 permission requests need review/);
  assert.match(nodes.attention.innerHTML, /data-task-open="workshop::wi_/);
  assert.match(nodes.attention.innerHTML, /data-task-fleet="web"/);
  assert.equal(nodes['attn-count'].textContent, 1);
  board.renderTasks({data: {tasks: [{...row, attention_reason: ['permission_unavailable'], permission_pending: 0}]}});
  assert.match(nodes.attention.innerHTML, /permission history is incomplete/);
  assert.doesNotMatch(nodes.attention.innerHTML, /no tasks need attention/);
  board.renderTasks({data: {tasks: [{...row, attention: false, attention_reason: [], permission_pending: 0}]}});
  assert.match(nodes.attention.innerHTML, /no tasks need attention/);
  assert.doesNotMatch(nodes.tasks.innerHTML, /permission requests need review/);
});

test('task-specific partial coverage cannot become a clear empty board and preserves readable task rows', () => {
  const boardSource = source.slice(source.indexOf('// WHY a card needs you'), source.indexOf('function railRow('));
  const nodes = Object.fromEntries(['attention', 'tasks', 'attn-count'].map(id => [id, {innerHTML: '', hidden: false}]));
  const board = runInNewContext(`${rendering}\n${boardSource}\n({renderTasks})`, {
    ...panel, document: {
      getElementById: id => nodes[id], documentElement: {style: {setProperty() {}}},
    },
  });
  // Other endpoints can remain healthy; this envelope alone defines task coverage.
  const env = {state:'ok',data:{tasks:[],issue_count:0,truncated:false,
    coverage:{total:2,reachable:1,partial:true}}};
  board.renderTasks(env);
  assert.match(nodes.attention.innerHTML,/1\/2 sources/);
  assert.match(nodes.attention.innerHTML,/Attention status is incomplete/);
  assert.match(nodes.attention.innerHTML,/no tasks need attention in readable sources/);
  assert.match(nodes.tasks.innerHTML,/no other tasks in readable sources/);
  assert.equal(nodes['attn-count'].hidden,false);
  assert.equal(nodes['attn-count'].textContent,'!');
  const row={task_id:'workshop::'+wi,fleet:'workshop / web',title:'Readable task history',state:'queued',resolved:true,attention:false};
  env.data.tasks=[row];board.renderTasks(env);
  assert.match(nodes.tasks.innerHTML,/Readable task history/);
  assert.match(nodes.tasks.innerHTML,/data-task-open="workshop::wi_/);
  env.data.tasks=[{...row,attention:true}];board.renderTasks(env);
  assert.match(nodes.attention.innerHTML,/Readable task history/);
  assert.match(nodes.attention.innerHTML,/1\/2 sources/);
  assert.equal(nodes['attn-count'].textContent,1);
  env.data.coverage={total:2,reachable:2,partial:false};board.renderTasks(env);
  assert.doesNotMatch(nodes.attention.innerHTML,/Attention status is incomplete|1\/2 sources/);
  assert.equal(nodes['attn-count'].title,'');
  board.renderTasks({state:'unavailable',remediation:'Task records could not be read.'});
  assert.equal(nodes['attn-count'].hidden,true);
  assert.doesNotMatch(nodes.tasks.innerHTML,/st-idle|no other tasks/);
});

for (const change of ['same', 'lane', 'removed', 'source', 'unavailable', 'hidden', 'disabled', 'elsewhere', 'modal'])
test(`task board refresh preserves exact keyboard position without stealing focus: ${change}`, () => {
  const document = {activeElement:null, documentElement:{style:{setProperty(){}}}};
  const body = {}; document.activeElement = body;
  const nodes = Object.fromEntries(['attention','tasks','attn-count'].map(id => [id, {
    children:[], hidden:false, isConnected:true,
    contains(node) { return this.children.includes(node); },
    closest() { return this.hidden ? this : null; },
    focus() { if (!this.hidden) document.activeElement=this; },
    querySelectorAll() { return this.children; },
    set innerHTML(value) {
      for (const child of this.children) { child.isConnected=false; if (document.activeElement===child) document.activeElement=body; }
      this.children=[...value.matchAll(/data-task-open="([^"]+)" data-task-fleet="([^"]*)"/g)].map(match => ({
        dataset:{taskOpen:match[1],taskFleet:match[2]}, isConnected:true,hidden:false,disabled:false,
        closest:() => this.hidden ? this : null,
        focus() { if (this.isConnected && !this.hidden && !this.disabled) document.activeElement=this; },
      }));
      if (change==='disabled') for (const child of this.children) child.disabled=true;
    },
  }]));
  document.getElementById=id=>nodes[id];
  const boardSource=source.slice(source.indexOf('// WHY a card needs you'),source.indexOf('function railRow('));
  const board=runInNewContext(`${rendering}\n${boardSource}\n({renderTasks})`,{...panel,document});
  const row={task_id:'north::'+wi,fleet:'north / web',title:'Review release',state:'queued',attention:false};
  const env=tasks=>({state:'ok',data:{tasks}});
  board.renderTasks(env([row]));
  const original=nodes.tasks.children[0]; original.disabled=false;original.focus();
  const outside={id:change==='modal'?'task-detail-close':'work-body'};
  if (['elsewhere','modal'].includes(change)) document.activeElement=outside;
  if (change==='hidden') nodes.tasks.hidden=true;
  const peer={...row,task_id:change==='source'?'south::'+wi:row.task_id,fleet:'south / web'};
  board.renderTasks(change==='unavailable'?{state:'unavailable'}:env(['removed','source'].includes(change)?[peer]
    : [peer,{...row,attention:change==='lane'}]));
  if (['elsewhere','modal'].includes(change)) assert.equal(document.activeElement,outside);
  else if (['removed','source','unavailable','disabled'].includes(change)) {
    assert.equal(document.activeElement,nodes.tasks); assert.equal(nodes.tasks.tabIndex,-1);
  } else if (change==='hidden') assert.equal(document.activeElement,nodes.attention);
  else {
    assert.equal(document.activeElement,(change==='lane'?nodes.attention:nodes.tasks).children.find(button=>button.dataset.taskOpen===row.task_id&&button.dataset.taskFleet===row.fleet));
    assert.notEqual(document.activeElement,original);
    assert.equal(document.activeElement.dataset.taskFleet,row.fleet);
  }
});
