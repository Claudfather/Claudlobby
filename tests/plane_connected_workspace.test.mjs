import assert from 'node:assert/strict';
import test from 'node:test';
import {ActionState} from '../claudlobby/plane/ui/action-state.js';
import {readFile} from 'node:fs/promises';
const read = name => readFile(new URL('../claudlobby/plane/ui/'+name,import.meta.url),'utf8');
const uri = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const ownerURL = uri((await read('owner-api-client.js')).replace('"./action-state.js"',JSON.stringify(new URL('../claudlobby/plane/ui/action-state.js',import.meta.url).href)));
const actionsURL = uri((await read('connected-actions.js')).replace('"./action-state.js"',JSON.stringify(new URL('../claudlobby/plane/ui/action-state.js',import.meta.url).href)));
const carrierURL = uri((await read('connected-read-client.js')).replace('"./owner-api-client.js"',JSON.stringify(ownerURL)).replace('"./connected-actions.js"',JSON.stringify(actionsURL)));
const adapterURL = uri((await read('two-source-read.js')).replace('"./owner-api-client.js"',JSON.stringify(ownerURL)));
const {createWorkspaceReadTransport}=await import(adapterURL);
const enrollmentURL = uri(await read('workspace-enrollment.js'));
const workspaceText = await read('connected-workspace.js');
const workspaceURL = uri(workspaceText.replace('"./guided-setup.js"',JSON.stringify(new URL('../claudlobby/plane/ui/guided-setup.js',import.meta.url).href)).replace('"./workspace-enrollment.js"',JSON.stringify(enrollmentURL)).replace('"./connected-read-client.js"',JSON.stringify(carrierURL)).replace('"./two-source-read.js"',JSON.stringify(adapterURL)));
const {createConnectedWorkspace} = await import(workspaceURL);
const {createConnectedReadClient} = await import(carrierURL);
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const wi = 'wi_'+'1'.repeat(32), fleetUID='fleet_'+'2'.repeat(32);
const refs = ['a','b'].map((n,i)=>({reference_id:id(i+4),endpoint_origin:`https://${n}.example.test`,revision:1}));
const sourceKey=ref=>'c-'+BigInt('0x'+ref.reference_id.replaceAll('-','')).toString(36);
const sourceA=sourceKey(refs[0]),sourceB=sourceKey(refs[1]);
const documentContext = {version:1,user_id:id(1),workspace_id:id(2),context_generation:id(3),shell_origin:'https://accounts.example.test',operational_origin:'https://plane.example.test',references:refs,expires_at:1800000060};
const context = references => ({version:2,user:{id:id(1)},workspace:{id:id(2),name:'Test workspace'},context_generation:id(3),references});
const reply = (status,value) => new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const ok = data => ({state:'ok',provenance:{last_ingest_at:'2026-10-10T00:00:00Z'},data});
const deferred = () => { let resolve; const promise=new Promise(r=>resolve=r);return {promise,resolve}; };
const flush = async()=>{for(let i=0;i<5;i++)await new Promise(r=>setImmediate(r));};
const descendants = node => [node,...node.children.flatMap(descendants)];
const proposalOf = row => descendants(row).find(node=>node.className==='connection-proposal');
const optionOf = (row,operation) => descendants(row).find(node=>node.dataset.operation===operation && !node.className.includes('connection-primary'));
function dom({rail=true}={}) {
  const elements=new Map(),observers=[],listeners=new Map();
  const document={visibilityState:'visible',activeElement:null,
    getElementById:id=>elements.get(id),createElement:name=>new Element(name),
    querySelector:q=>q==='main'?elements.get('main'):null,
    querySelectorAll:q=>{
      assert.ok(['#view-nav button, [data-bot-inspect]', '#view-nav button, #debug-toggle, [data-bot-inspect]'].includes(q), 'selector must be modelled explicitly');
      return [...nav,...(q.includes('#debug-toggle')?[elements.get('debug-toggle')]:[]),...['fleet','channel'].flatMap(id=>elements.get(id).children).filter(e=>e.dataset.botInspect)];
    },
    addEventListener(n,fn){listeners.set(n,fn);},removeEventListener(n){listeners.delete(n);}};
  class Element {
    constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.handlers={};this.hidden=false;this.disabled=false;this.isConnected=true;this.value='';this.textContent='';}
    set disabled(value){this._disabled=value;if(value&&document.activeElement===this)document.activeElement=null;}
    get disabled(){return this._disabled;}
    adopt(node){node.parentNode?.children&&(node.parentNode.children=node.parentNode.children.filter(c=>c!==node));node.parentNode=this;return node;}
    append(...nodes){for(const node of nodes)this.children.push(this.adopt(node));}
    prepend(...nodes){this.children.unshift(...nodes.map(node=>this.adopt(node)));}
    remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(c=>c!==this);this.parentNode=null;this.isConnected=false;}
    replaceChildren(...nodes){for(const c of this.children){c.isConnected=false;c.parentNode=null;}this.children=[];this.append(...nodes);this.textContent='';}
    setAttribute(k,v){this.attributes[k]=v;}
    get classList(){const names=()=>(this.className||'').split(/\s+/).filter(Boolean);
      return {contains:name=>names().includes(name),toggle:(name,force=!names().includes(name))=>{
        this.className=[...names().filter(n=>n!==name),...(force?[name]:[])].join(' ');return force;}};}
    addEventListener(n,fn){this.handlers[n]=fn;}
    focus(){document.activeElement=this;}
    close(){this.closed=true;}
    click(){if(!this.disabled)this.handlers.click?.();}
  }
  for(const id of ['owner-session','main','fleet','channel','fleet-totals','presence-strip','task-detail-content','focus-pane','task-detail','focus-overlay','focus-title','debug-toggle','search',...(rail?['rail-channel']:[])])elements.set(id,new Element());
  const tools=new Element('details'),viewNav=new Element('nav');viewNav.querySelector=q=>q==='details'?tools:null;elements.set('view-nav',viewNav);viewNav.append(tools);
  const nav=['channel','tasks','grid','fleet','trust'].map(view=>{const e=new Element('button');e.dataset.view=view;return e;});
  class Observer {constructor(fn){this.fn=fn;this.targets=[];observers.push(this);}observe(target){this.targets.push(target);}disconnect(){this.closed=true;}}
  return {document,Element,nav,observers,listeners,get:id=>elements.get(id),MutationObserver:Observer};
}
// Replaces one global for a test; the returned function restores the original descriptor.
function stubGlobal(name,descriptor){
  const previous=Object.getOwnPropertyDescriptor(globalThis,name);
  Object.defineProperty(globalThis,name,{configurable:true,...descriptor});
  return ()=>previous?Object.defineProperty(globalThis,name,previous):delete globalThis[name];
}
const stubLocalStorage=()=>{const store=new Map();return stubGlobal('localStorage',{value:{getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v)}});};
const visible=node=>{for(let n=node;n;n=n.parentNode)if(n.hidden)return false;return true;};
function harness(options={}) {
  const count=options.count??2,doc={...documentContext,references:options.references??refs.slice(0,count)},calls=[],timers=[],streams=[],overrides=new Map();
  let metadata=context(doc.references),metadataStatus=200,time=0;
  const tokens=new Map(),receipts=new Map(), grants=new Set(['message','nudge','feedback']);
  const fetch=async(url,init)=>{
    calls.push({url,init});const u=new URL(url),path=u.pathname;
    if(overrides.has(url))return overrides.get(url)(init);
    if(u.origin===doc.shell_origin){
      if(path==='/api/plane/context')return reply(metadataStatus,metadata);
      if(path==='/api/plane/host-assertion')return reply(200,{version:1,assertion:'e30.e30.AAAA',expires_at:1800000120});
      if(path==='/api/plane/session-assertion')return reply(200,{version:1,assertion:'e30.e30.AAAA',expires_at:1800000120});
      throw new Error('Unexpected website route');
    }
    const n=u.hostname[0],host='host_'+n.repeat(32),binding={binding_revision:1,binding_generation:id(n==='a'?6:7)};
    const ready=token=>({version:1,state:'ready',expires_in:900,read_profile:{version:1,profile:'direct-owner-read-v1',host_uid:host},session_context:{issuer:doc.shell_origin,subject:doc.user_id,workspace_id:doc.workspace_id,operational_origin:doc.operational_origin,...binding},...(token?{token}:{})});
    if(path==='/api/owner/workspace/challenge')return reply(202,{version:1,state:'awaiting_assertion',issuer:doc.shell_origin,host_uid:host,nonce:'A'.repeat(43),operational_origin:doc.operational_origin,expires_at:1800000300});
    if(path==='/api/owner/workspace/stage')return reply(202,{version:1,state:'awaiting_local_approval',expires_at:1800000120});
    if(path.endsWith('/challenge'))return reply(202,{version:1,state:'awaiting_session_assertion',nonce:'A'.repeat(43),issuer:doc.shell_origin,host_uid:host,operational_origin:doc.operational_origin,...binding,expires_at:1800000120});
    if(path.endsWith('/issue')){const token=n.toUpperCase().repeat(42)+'A';tokens.set(n,token);return reply(200,ready(token));}
    if(init.headers.Authorization!=='Bearer '+tokens.get(n))return reply(403,{state:'denied'});
    if(path.endsWith('/status'))return reply(200,ready());
    if(path.endsWith('/renew')){const token='R'.repeat(42)+'A';tokens.set(n,token);return reply(200,ready(token));}
    if(path.endsWith('/logout')){tokens.delete(n);return reply(200,{version:1,state:'signed_out'});}
    if(path.startsWith('/api/owner/connected/actions/')) {
      const input=JSON.parse(init.body),operation=path.split('/').at(-1),kind=input.kind || 'message';
      if(!grants.has(kind))return reply(403,{state:'denied'});
      const scope={workspace:'connected-test',host,fleet:input.scope?.fleet||input.room,viewer:n+'-'+kind+'-grant1'};
      if(operation==='context')return reply(200,{version:kind==='message'?1:2,room:input.room,scope,simulation:false,
        recipients:[{id:'actor_'+'c'.repeat(32),label:'Lead',lead:true},...(kind==='message'?[{id:'actor_'+'d'.repeat(32),label:'Worker'}]:[])],
        actions:[kind],...(kind!=='message'?{release_id:'r-'+'e'.repeat(64)}:{})});
      assert.deepEqual(input.scope,scope,'HTTP receives exact raw scope');
      assert.match(input.target.recipient,/^actor_[0-9a-f]{32}$/);
      if(kind!=='message'){assert.equal(input.target.task_id,wi);assert.equal(input.target.assignment_id,null);}
      const {body,...metadata}=input;
      if(operation==='prepare')return reply(200,{...metadata,semantic_sha256:'f'.repeat(64)});
      if(operation==='send'){const result={...metadata,version:kind==='message'?1:2,status:'recorded'};receipts.set(n+input.request_id,result);return reply(200,result);}
      if(operation==='receipt')return reply(200,{...receipts.get(n+input.request_id),status:'delivered'});
      throw new Error('Unexpected action route');
    }
    const readPath=path.split('/read/')[1];
    if(readPath==='stream'){
      let control;const body=new ReadableStream({start(c){control=c;}});streams.push({n,control,init});
      init.signal.addEventListener('abort',()=>{try{control.error(new Error('aborted'));}catch{}},{once:true});
      return new Response(body,{headers:{'Content-Type':'text/event-stream'}});
    }
    if(readPath==='fleets')return reply(200,ok({fleets:[{alias:'team',uid:fleetUID,bots:1,provisional:0}]}));
    if(readPath==='overview')return reply(200,ok({fleets:[{alias:'team',uid:fleetUID,bots:1,provisional:0}],host:{rows:1,daemon_serving:true},totals:{fleets:1,bots:1,provisional:0,working:0,attention:0,overdue:0,live_poll:'ok',recorder_gaps:[]}}));
    if(readPath==='channel')return reply(200,ok({threads:[{key:wi,work_item_id:wi,task_link:{task_id:wi,fleet:'team',fleet_uid:fleetUID,host_uid:host},messages:[{msg_id:'msg_'+'3'.repeat(32),sender_alias:'team/lead',body:'<literal>',tx:[]}]}]}));
    if(readPath===`tasks/${wi}`)return reply(200,ok({task:{task_id:wi,fleet:'team',fleet_uid:fleetUID,title:'Outside board',body:'full body',current_assignment:null,history:[]}}));
    if(readPath==='tasks')return reply(200,ok({tasks:[],issues:[],issue_count:0,attention_count:0}));
    if(readPath==='identities')return reply(200,ok({identities:[]}));
    if(readPath==='summary')return reply(200,ok({daemon_serving:true}));
    throw new Error('Unexpected host route');
  };
  const ui=dom({rail:options.rail??true});const dependencies={documentContext:doc,documentOrigin:doc.operational_origin,fetch,
    now:()=>1800000000,monotonicNow:()=>time,setTimeout(fn,ms){const t={fn,ms,active:true};timers.push(t);return t;},clearTimeout(t){if(t)t.active=false;},MutationObserver:ui.MutationObserver,...options.dependencies};
  const api=createConnectedWorkspace(dependencies);
  return {api,doc,ui,calls,timers,streams,overrides,dependencies,grants,receipts,
    get metadata(){return metadata;},set metadata(v){metadata=v;},set metadataStatus(v){metadataStatus=v;},
    get time(){return time;},set time(v){time=v;},
    mount(callbacks={}){return api.mountSessionControls({document:ui.document,element:ui.get('owner-session'),...callbacks});},
    timer(ms){const t=timers.find(t=>t.active&&t.ms===ms);assert.ok(t);t.active=false;t.fn();}};
}

test('trusted source labels name computers without conflating a read failure with disconnection',async()=>{
  const h=harness({count:1});h.mount();await h.api.checkAccount();await flush();
  assert.equal(await h.api.act(0,'connect'),true);
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/read/overview',()=>reply(200,ok({
    fleets:[{alias:'team',uid:fleetUID,bots:1,provisional:0}],
    host:{display_name:'Office computer',rows:1,daemon_serving:true},
    totals:{fleets:1,bots:1,provisional:0,working:0,attention:0,overdue:0,live_poll:'ok',recorder_gaps:[]},
  })));
  await h.api.jget('/api/overview');
  const label=h.ui.get('owner-session').children[1].children[0];
  assert.equal(label.textContent,'Office computer · Connected');assert.equal(label.title,'a.example.test');
  assert.equal(h.api.snapshot().computers[0].label,'Office computer');
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/read/overview',()=>reply(503,{}));
  const failed=await h.api.jget('/api/overview');
  assert.equal(failed.data.sources[0].connection_state,'ready');assert.equal(failed.data.sources[0].state,'unavailable');
  assert.equal(label.textContent,'Office computer · Connected','only admitted connection facts can report disconnection');
  h.api.dispose();
});

test('rendered connected controls acknowledge only their exact shell without waiting for host/account reads',async()=>{
  const h=harness({count:0}),messages=[],held=deferred();
  h.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2)+'&include=computer_names',()=>held.promise);
  h.ui.document.defaultView={parent:{postMessage:(...args)=>messages.push(args)}};
  h.mount();
  assert.equal(h.ui.get('owner-session').hidden,false);
  assert.ok(h.ui.get('owner-session').children.length);
  assert.deepEqual(messages,[[{type:'claudfather.workspace.ready',version:1},documentContext.shell_origin]]);
  assert.equal(h.api.snapshot().phase,'checking','ready describes the rendered interface, not host authority');
  h.api.dispose();held.resolve(reply(200,context([])));await flush();
  assert.equal(messages.length,1,'late account completion never reopens a disposed view');
  for(const invalid of [true,false]) {
    const other=harness(invalid?{dependencies:{documentContext:null}}:{count:0});
    const view={postMessage:()=>{throw new Error('must not announce');}};view.parent=view;
    other.ui.document.defaultView=invalid?{parent:{postMessage:()=>assert.fail('invalid document must not announce')}}:view;
    other.mount();other.api.dispose();
  }
});

test('known expiry offers explicit Connect without reconnecting or disturbing another computer',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  await h.api.act(0,'connect');h.time=1000;await h.api.act(1,'connect');
  const rows=h.ui.get('owner-session').children.filter(e=>e.className==='connection-card');
  const primary=rows[0].children[1].children[0];
  const issued=()=>h.calls.filter(c=>c.url.endsWith('/issue')).length;
  assert.equal(issued(),2);
  h.time=900000;h.timer(900000);await flush();
  assert.equal(h.api.snapshot().computers[0].state,'unavailable','expiry still revokes read admission');
  assert.match(rows[0].children[0].textContent,/Session expired$/);
  assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
  assert.equal(rows[1].children[1].children[0].textContent,'Disconnect');
  assert.equal(issued(),2,'no automatic reconnect');
  primary.click();await flush();
  assert.equal(issued(),3,'only the explicit click creates a fresh session');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')||c.url.endsWith('/logout')).length,0);
  assert.equal(primary.textContent,'Disconnect');
  assert.match(rows[0].children[0].textContent,/Connected$/);
  assert.equal(h.api.snapshot().computers[1].state,'ready');h.api.dispose();
});

test('unconnected Rooms and Tasks explain owner pairing without claiming it is disabled',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  for(const path of ['/api/tasks','/api/channel?limit=120']) {
    const value=await h.api.jget(path);
    assert.equal(value.state,'unavailable');
    assert.match(value.remediation,/Not connected.*Rooms and Tasks.*Connect.*first-time owner pairing.*Set up computer access/);
    assert.doesNotMatch(value.remediation,/pairing (?:is )?(?:disabled|off)|no tasks|nothing here/i);
  }
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,0);
  h.api.dispose();
});

test('unreadable Rooms and Tasks retain known expiry guidance without hiding readable peers',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  assert.match(h.ui.get('view-nav').children[0].children[0].textContent,/Recorder and activity search are unavailable/);
  await h.api.act(0,'connect');h.time=1000;await h.api.act(1,'connect');
  h.time=900000;h.timer(900000);await flush();
  const partial=await h.api.jget('/api/tasks');
  assert.equal(partial.state,'ok');
  assert.match(partial.data.sources[0].remediation,/expired.*Connect again/);
  h.time=901000;h.timer(900000);await flush();
  for(const path of ['/api/tasks','/api/channel?limit=120']) {
    const value=await h.api.jget(path);
    assert.equal(value.state,'unavailable');
    assert.match(value.remediation,/expired.*Connect again/);
  }
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,2);
  h.api.dispose();
});

for(const stage of ['challenge','assertion','issue'])
 test(`failed ${stage} keeps explicit Connect available without expiry, automatic retry or peer disruption`,async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  await h.api.act(1,'connect');
  const door=stage==='assertion' ? documentContext.shell_origin+'/api/plane/session-assertion'
    : refs[0].endpoint_origin+'/api/owner/connected/'+stage;
  h.overrides.set(door,()=>reply(503,{}));
  assert.equal(await h.api.act(0,'connect'),false);
  const rows=h.ui.get('owner-session').children.filter(e=>e.className==='connection-card');
  const primary=rows[0].children[1].children[0];
  assert.equal(h.api.snapshot().computers[0].state,'unavailable');
  assert.doesNotMatch(rows[0].children[0].textContent,/expired/i);
  assert.match(rows[0].children[2].textContent,stage === 'issue' ? /outcome is unknown/ : /No session issue was sent/);
  if(stage !== 'issue') assert.doesNotMatch(rows[0].children[2].textContent,/outcome is unknown/);
  assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
  assert.equal(rows[1].children[1].children[0].textContent,'Disconnect');
  const calls=h.calls.length;
  h.ui.listeners.get('input')({isTrusted:true});await flush();
  assert.equal(h.calls.length,calls,'no handshake or mutation retries on activity');
  assert.equal((await h.api.jget('/api/overview')).data.sources[1].connection_state,'ready');
  h.overrides.delete(door);primary.click();await flush();
  assert.equal(primary.textContent,'Disconnect');assert.match(rows[0].children[0].textContent,/Connected$/);
  assert.equal(h.api.snapshot().computers[1].state,'ready');
  assert.equal(h.calls.filter(c=>/\/(renew|logout|actions\/send)$/.test(c.url)).length,0);
  h.api.dispose();
 });

test('idle warning requires trusted visible input to renew and removes activity listeners on close',async()=>{
  const h=harness({count:1});h.mount();await flush();await h.api.act(0,'connect');
  h.time=780000;h.timer(780000);await flush();
  const card=h.ui.get('owner-session').children.find(e=>e.className==='connection-card');
  const detail=card.children.find(e=>e.className==='connection-detail');
  assert.match(detail.textContent,/expires soon.*Keep connected/);
  h.ui.listeners.get('input')({isTrusted:false});await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
  h.ui.document.visibilityState='hidden';h.ui.listeners.get('pointerdown')({isTrusted:true});await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);
  h.ui.document.visibilityState='visible';h.ui.listeners.get('keydown')({isTrusted:true});h.timer(0);await flush();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);
  assert.doesNotMatch(detail.textContent,/expires soon/);
  h.api.dispose();
  for(const name of ['visibilitychange','pointerdown','keydown','input','wheel','touchmove'])assert.equal(h.ui.listeners.has(name),false);
});

test('fresh account admission precedes explicit host handshake; aggregate and outside-board detail use genuine core handles',async()=>{
  const h=harness();assert.equal(await h.api.checkAccount(),true);await flush();
  assert.equal(h.api.snapshot().phase,'ready');assert.ok(h.api.snapshot().computers.every(c=>c.state!=='checking'));
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].init.credentials,'include');assert.equal(h.calls[0].init.redirect,'error');
  assert.equal(await h.api.act(0,'connect'),true);assert.equal(await h.api.act(1,'connect'),true);
  const overview=await h.api.jget('/api/overview');assert.equal(overview.data.coverage.reachable,2);
  const all=await h.api.jget('/api/channel?limit=120');assert.equal(all.data.threads.length,2);assert.equal(all.data.threads[0].messages[0].body,'<literal>');
  assert.equal(all.data.threads[0].task_link.task_id,sourceA+'::'+wi);
  const detail=await h.api.jget(`/api/tasks/${encodeURIComponent(sourceA+'::'+wi)}?fleet=${encodeURIComponent(sourceA+' / team')}`);
  assert.equal(detail.data.task.body,'full body');assert.equal(detail.data.task.fleet_uid,sourceA+'::'+fleetUID);
  const signer=h.calls.find(c=>c.url.endsWith('/session-assertion'));
  assert.deepEqual(Object.keys(JSON.parse(signer.init.body)).sort(),['binding_generation','binding_revision','expected_context_generation','host_uid','nonce','operational_origin','reference_id','workspace_id']);
  assert.equal(typeof h.api.sendAction,'function');assert.equal(typeof h.api.prepareAction,'function');assert.ok(!JSON.stringify(h.api.snapshot()).includes('AAAAA'));
  h.api.dispose();
});

test('zero references is explicit empty; invalid document and stale complete reference sets never fall back to local reads',async()=>{
  const empty=harness({count:0});assert.equal(await empty.api.checkAccount(),true);assert.equal((await empty.api.jget('/api/tasks')).state,'empty');assert.equal(empty.calls.length,1);empty.api.dispose();
  for(const change of [{user:{id:id(9)}},{workspace:{id:id(9),name:'Other'}},{context_generation:id(9)},{references:[{...refs[0],revision:2},refs[1]]},{references:[refs[0]]}]){
    const h=harness();h.metadata={...h.metadata,...change};assert.equal(await h.api.checkAccount(),false);assert.equal(h.api.snapshot().phase,'denied');assert.equal(h.calls.length,1);assert.equal(await h.api.act(0,'connect'),false);h.api.dispose();
  }
  for(const change of [{version:2},{references:[{...refs[0],endpoint_origin:'https://*.example.test'}]},{user_id:id(1)+'\n'},{operational_origin:'https://wrong.example.test'},{extra:true}]){
    const h=harness({dependencies:{documentContext:{...documentContext,...change}}});assert.equal(await h.api.checkAccount(),false);assert.equal(h.calls.length,0);assert.equal((await h.api.jget('/api/tasks')).state,'denied');h.api.dispose();
  }
});

test('single source is usable; source failures remain independent, with per-host explicit serialized controls',async()=>{
  const h=harness({count:1});await h.api.checkAccount();await flush();assert.equal(await h.api.act(0,'connect'),true);
  const read=await h.api.jget('/api/overview');assert.equal(read.data.coverage.total,1);assert.equal(read.data.coverage.reachable,1);
  assert.equal(await h.api.act(1,'connect'),false);assert.equal(await h.api.act(0,'send'),false);
  const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/renew',()=>held.promise);
  const renewal=h.api.act(0,'renew');await flush();assert.equal(await h.api.act(0,'logout'),false);
  held.resolve(reply(503,{}));await renewal;assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,1);h.api.dispose();
  const two=harness();await two.api.checkAccount();await flush();await two.api.act(0,'connect');await two.api.act(1,'connect');
  two.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/read/overview',()=>reply(503,{}));
  const partial=await two.api.jget('/api/overview');assert.equal(partial.data.coverage.reachable,1);assert.equal(two.api.snapshot().phase,'ready');two.api.dispose();
});

test('provider outage retains known current context honestly; initial outage admits no host or fabricated empty state',async()=>{
  const h=harness();await h.api.checkAccount();await flush();await h.api.act(0,'connect');h.metadataStatus=503;
  assert.equal(await h.api.checkAccount(),false);assert.equal(h.api.snapshot().accountUnavailable,true);assert.equal(h.api.snapshot().phase,'ready');
  assert.equal((await h.api.jget('/api/overview')).data.coverage.reachable,1);h.api.dispose();
  const first=harness();first.metadataStatus=503;assert.equal(await first.api.checkAccount(),false);assert.equal(first.api.snapshot().phase,'unavailable');assert.equal(await first.api.act(0,'connect'),false);assert.equal(first.calls.length,1);first.api.dispose();
});

test('incremental reads use the same account and transport fence as final reads',async()=>{
  const held=deferred(),paints=[];let progress;
  const h=harness({dependencies:{createTransport:()=>({
    jget(url,onUpdate){progress=onUpdate;return held.promise;},dispose(){},
  })}});
  await h.api.checkAccount();await flush();
  const reading=h.api.jget('/api/tasks',value=>paints.push(value));
  const accepted=ok({tasks:[]});progress(accepted);assert.deepEqual(paints,[accepted]);
  h.metadataStatus=401;await h.api.checkAccount();
  progress(ok({tasks:[{title:'late private'}]}));
  held.resolve(accepted);
  assert.equal((await reading).state,'denied');
  assert.equal(paints.length,1,'retired account cannot receive late source progress');
  h.api.dispose();
});

test('observed account refusal retires clients, fences held private reads and clears all private regions even if callback throws',async()=>{
  for(const refusal of [401,403,409,'generation']) {
  const h=harness();h.ui.get('main').append(h.ui.get('owner-session'));
  h.mount({onPause(){throw new Error('callback');},onResume(){throw new Error('callback');}});await h.api.checkAccount();await flush();await h.api.act(0,'connect');
  for(const name of ['main','fleet-totals','presence-strip','task-detail-content','focus-pane']) {
    const privateNode=h.ui.document.createElement('b');privateNode.textContent='private stale content';h.ui.get(name).append(privateNode);
  }
  const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/read/tasks',()=>held.promise);
  const old=h.api.jget('/api/tasks');await flush();
  if(refusal==='generation')h.metadata={...h.metadata,context_generation:id(99)};else h.metadataStatus=refusal;
  await h.api.checkAccount();
  held.resolve(reply(200,ok({tasks:[{task_id:wi,title:'late private'}]})));const result=await old;
  assert.equal(result.state,'denied');assert.equal(result.data,undefined);assert.equal(h.api.snapshot().phase,'denied');
  for(const name of ['fleet-totals','presence-strip','task-detail-content','focus-pane'])assert.equal(h.ui.get(name).children.length,0);
  assert.deepEqual(h.ui.get('main').children,[h.ui.get('owner-session')],'only the safe recovery scaffold remains in the rendered main');
  const recovery=descendants(h.ui.get('main')).map(node=>node.textContent).join(' ');
  assert.match(recovery,/Your account or saved computers changed.*open your workspace again/);
  assert.doesNotMatch(recovery,/private stale content|late private|[ab]\.example\.test/);
  assert.equal(h.ui.get('task-detail').closed,true);assert.equal(h.ui.get('focus-overlay').hidden,true);
  h.metadataStatus=200;assert.equal(await h.api.checkAccount(),false);assert.equal(await h.api.act(0,'connect'),false);h.api.dispose();
  }
});

test('metadata total body deadline and disposal reject late responses without issuing hosts',async()=>{
  const h=harness();let body;h.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2)+'&include=computer_names',()=>new Response(new ReadableStream({start(c){body=c;}}),{headers:{'Content-Type':'application/json'}}));
  const checking=h.api.checkAccount();await flush();h.timer(8000);assert.equal(await checking,false);assert.equal(h.api.snapshot().phase,'unavailable');assert.equal(h.calls.length,1);h.api.dispose();
  const late=harness(),held=deferred();late.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2)+'&include=computer_names',()=>held.promise);
  const pending=late.api.checkAccount();late.api.dispose();assert.equal(await pending,false);held.resolve(reply(200,late.metadata));await flush();assert.equal(late.api.snapshot().phase,'disposed');assert.equal(late.calls.length,1);
});

test('signer account denial and late issuance cannot revive a retired document or automatically replay issue',async()=>{
  const h=harness();await h.api.checkAccount();await flush();h.overrides.set(documentContext.shell_origin+'/api/plane/session-assertion',()=>reply(409,{}));
  assert.equal(await h.api.act(0,'connect'),false);assert.equal(h.api.snapshot().phase,'denied');assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,0);h.api.dispose();
  const late=harness();await late.api.checkAccount();await flush();const held=deferred();late.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/issue',()=>held.promise);
  const connect=late.api.act(0,'connect');await flush();late.metadata={...late.metadata,context_generation:id(9)};await late.api.checkAccount();held.resolve(reply(503,{}));assert.equal(await connect,false);assert.equal(late.api.snapshot().phase,'denied');assert.equal(late.calls.filter(c=>c.url.endsWith('/issue')).length,1);late.api.dispose();
});

test('stable stream facade rejects stale callbacks; adding/removing listeners does not reopen a source',async()=>{
  const children=[];const h=harness({dependencies:{createTransport:()=>({jget:async()=>ok({}),dispose(){},createEventSource(){const child={listeners:new Map(),callbacks:new Map(),addEventListener(k,fn){if(!this.callbacks.has(k))this.callbacks.set(k,new Set());this.callbacks.get(k).add(fn);this.listeners.set(k,event=>{for(const callback of this.callbacks.get(k))callback(event);});},removeEventListener(k,fn){this.callbacks.get(k)?.delete(fn);},close(){this.closed=true;}};children.push(child);return child;}})}});
  const stream=h.api.createEventSource('/api/stream');let messages=0,sources=0;stream.onmessage=()=>messages++;const source=()=>sources++;stream.addEventListener('source',source);
  await h.api.checkAccount();await flush();assert.equal(children.length,1);stream.addEventListener('source',()=>{});assert.equal(children.length,1);
  children[0].onmessage({data:'{}'});children[0].listeners.get('source')({data:'{}'});assert.equal(messages,1);assert.equal(sources,1);
  stream.removeEventListener('source',source);children[0].listeners.get('source')({data:'{}'});assert.equal(sources,1);
  h.metadataStatus=403;await h.api.checkAccount();children[0].onmessage({data:'late'});children[0].onopen?.({});assert.equal(messages,1);assert.equal(children[0].closed,true);h.api.dispose();
});

test('controls retain focus across state updates and disable unsupported navigation/inspection honestly',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  for(const n of h.ui.nav)assert.equal(n.disabled,!['channel','tasks','grid'].includes(n.dataset.view));assert.equal(h.ui.get('search').disabled,true);
  const root=h.ui.get('owner-session'),row=root.children[1],check=root.children[0].children[1];check.focus();await h.api.checkAccount();assert.equal(h.ui.document.activeElement,check);assert.equal(root.children[0].children[1],check);
  const connect=row.children[1].children[0];assert.equal(connect.disabled,false);connect.click();await flush();assert.equal(h.api.snapshot().computers[0].state,'ready');assert.equal(row.children[1].children[0],connect);
  for(const id of ['fleet','channel']){
    const target=h.ui.get(id);assert.ok(h.ui.observers[0].targets.includes(target));
    const inspect=new h.ui.Element('button');inspect.dataset.botInspect='actor';target.append(inspect);h.ui.observers[0].fn();assert.equal(inspect.disabled,true);assert.match(inspect.title,/Activity and task records/);
    const presence=new h.ui.Element('button');presence.dataset.botInspect='actor';presence.dataset.botInspection='presence';target.append(presence);
    let inspections=0;presence.addEventListener('click',()=>{inspections++;});h.ui.observers[0].fn();
    assert.equal(presence.disabled,false);assert.equal(presence.title,'');presence.click();assert.equal(inspections,1);
  }
  const tasks=h.ui.nav.find(button=>button.dataset.view==='tasks');assert.equal(tasks.disabled,false);
  h.metadataStatus=503;await h.api.checkAccount();h.ui.observers[0].fn();
  assert.equal(tasks.disabled,false,'an account-service outage preserves task navigation over retained reads');
  h.metadataStatus=403;await h.api.checkAccount();h.ui.observers[0].fn();
  assert.equal(tasks.disabled,true,'account refusal retires task navigation');
  for(const id of ['fleet','channel'])for(const button of h.ui.get(id).children)assert.equal(button.disabled,true,'account refusal retires presence inspection too');
  h.api.dispose();assert.equal(h.ui.observers[0].closed,true);assert.equal(h.ui.listeners.has('visibilitychange'),false);assert.equal(h.calls.filter(c=>c.url.endsWith('/logout')).length,0);
});

test('api-client selects only an existing marker; absent marker preserves legacy same-origin reads and zero owner probes',async()=>{
  const original={document:globalThis.document,location:globalThis.location,fetch:globalThis.fetch,EventSource:globalThis.EventSource};
  const apiText=(await read('api-client.js')).replace('"./connected-workspace.js"',JSON.stringify(workspaceURL))
    .replace('"./browser-owner-transport.js"',JSON.stringify(uri(await read('browser-owner-transport.js'))));
  try {
    const calls=[];globalThis.location={origin:documentContext.operational_origin};globalThis.document={getElementById:()=>null};globalThis.fetch=async(url)=>{calls.push(url);return reply(200,{legacy:true});};globalThis.EventSource=class{constructor(url){calls.push(url);}};
    const legacy=await import(uri(apiText+'\n//legacy'));assert.equal(legacy.mountSessionControls,undefined);assert.equal(legacy.dispose,undefined);assert.deepEqual(await legacy.jget('/api/tasks'),{legacy:true});legacy.createEventSource('/api/stream');assert.deepEqual(calls,['/api/tasks','/api/stream']);
    const marker={textContent:'{invalid',remove(){this.removed=true;}};globalThis.document={getElementById:()=>marker};calls.length=0;
    const invalid=await import(uri(apiText+'\n//invalid'));assert.equal((await invalid.jget('/api/tasks')).state,'denied');assert.equal(calls.length,0);assert.equal(marker.removed,true);invalid.dispose();
  } finally {for(const [key,value]of Object.entries(original)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});

// This separates keyed focus from the unsupported-view assertions above.
test('account recheck preserves the same focused control rather than replacing its DOM',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  const root=h.ui.get('owner-session'),check=root.children[0].children[1];check.focus();
  await h.api.checkAccount();assert.equal(root.children[0].children[1],check);assert.equal(check.isConnected,true);
  assert.equal(h.ui.document.activeElement,check);h.api.dispose();
});


test('computer cards offer one primary action and keep first-use setup and diagnostics in a native disclosure',async()=>{
 const h=harness(),attention=[];h.mount({onConnectionAttention:event=>attention.push(event)});await h.api.checkAccount();await flush();
 assert.deepEqual(attention,[{source:sourceA,state:'not_connected'},{source:sourceB,state:'not_connected'}]);
 const row=h.ui.get('owner-session').children[1],actions=row.children[1],primary=actions.children[0],help=row.children.at(-1);
 assert.equal(actions.children.filter(c=>!c.hidden).length,1);assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
 assert.equal(help.tagName,'DETAILS');assert.ok(!help.open);
 assert.match(help.children[0].textContent,/Connection options/);
 assert.equal(optionOf(row,'enroll').textContent,'Prepare local approval');
 assert.ok(descendants(help).some(node=>node.href===refs[0].endpoint_origin+'/owner'));
 assert.equal(h.calls.filter(c=>c.url.includes('/api/owner/')).length,0,'showing guidance contacts no host');
 primary.click();await flush();assert.equal(primary.textContent,'Disconnect');assert.equal(primary.disabled,false);
 assert.equal(optionOf(row,'logout').hidden,true);assert.equal(optionOf(row,'check').hidden,false);
 assert.equal(h.api.snapshot().computers[1].state,'denied','the other host is not automatically connected');
 primary.click();await flush();assert.equal(h.calls.filter(c=>c.url.endsWith('/logout')).length,1);
 assert.equal(primary.textContent,'Connect');h.api.dispose();
});

test('uncertain local approval stays prominent with its original code and cannot be prepared again',async()=>{
 const h=harness();h.mount();await h.api.checkAccount();await flush();await h.api.act(1,'connect');
 h.overrides.set(refs[0].endpoint_origin+'/api/owner/workspace/stage',()=>{throw new Error('lost reply');});
 assert.equal(await h.api.act(0,'enroll'),false);
 const row=h.ui.get('owner-session').children[1],help=row.children.at(-1),proposal=proposalOf(row),prepare=optionOf(row,'enroll');
 assert.equal(help.open,true);assert.match(row.children[2].textContent,/may have been prepared.*do not prepare it again/);
 assert.ok(proposal.children.some(c=>c.textContent.includes('Approval code:')));assert.equal(prepare.disabled,true);
 const before=h.calls.length;prepare.click();await flush();assert.equal(h.calls.length,before);
 assert.equal(h.api.snapshot().computers[1].state,'ready');
 h.metadataStatus=403;await h.api.checkAccount();assert.equal(row.hidden,true);assert.equal(row.children[1].children[0].disabled,true);
 assert.ok(!proposal.children.some(c=>c.textContent.includes('Approval code:')));h.api.dispose();
});

test('first-use approval uses enrollment purpose only, preserves peer reads and requires later explicit Connect',async()=>{
 const h=harness();h.mount();await h.api.checkAccount();await flush();await h.api.act(1,'connect');
 const before=h.calls.length;assert.equal(await h.api.act(0,'enroll'),true);
 const calls=h.calls.slice(before);assert.deepEqual(calls.map(c=>new URL(c.url).pathname),['/api/plane/context','/api/owner/workspace/challenge','/api/plane/host-assertion','/api/owner/workspace/stage']);
 const signer=calls.find(c=>c.url.endsWith('/host-assertion'));assert.equal(signer.init.credentials,'include');assert.deepEqual(Object.keys(JSON.parse(signer.init.body)).sort(),['host_uid','nonce','operational_origin','workspace_id']);
 assert.notEqual(h.api.snapshot().computers[0].state,'ready');assert.equal(h.api.snapshot().computers[1].state,'ready');
 const root=h.ui.get('owner-session'),first=root.children[1],proposal=proposalOf(first);
 assert.ok(proposal.children.some(c=>c.textContent.includes('Approval code:')));assert.equal((await h.api.jget('/api/overview')).data.coverage.reachable,1);
 assert.equal(await h.api.act(0,'enroll'),false);assert.equal(h.calls.filter(c=>c.url.endsWith('/workspace/stage')).length,1);
 // Synthetic host fixture now represents local approval; UI itself has no confirm door.
 assert.equal(await h.api.act(0,'connect'),true);assert.ok(!proposal.children.some(c=>c.textContent.includes('Approval code:')));
 const count=h.calls.length;assert.equal(await h.api.act(0,'enroll'),false);assert.equal(h.calls.length,count);h.api.dispose();
});

test('account loss during host staging erases approval code and fences late staged result',async()=>{
 const h=harness();h.mount();await h.api.checkAccount();await flush();const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/workspace/stage',()=>held.promise);
 const panel=proposalOf(h.ui.get('owner-session').children[1]);
 const enroll=h.api.act(0,'enroll');await flush();h.metadataStatus=403;await h.api.checkAccount();held.resolve(reply(202,{version:1,state:'awaiting_local_approval',expires_at:1800000120}));
 assert.equal(await enroll,false);assert.equal(h.api.snapshot().phase,'denied');assert.ok(!panel.children.some(c=>c.textContent.includes('Approval code:')));
 assert.ok(!descendants(h.ui.get('owner-session')).includes(panel),'retired source controls are absent from recovery guidance');
 assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,0);h.api.dispose();
});

test('forgetting approval returns keyboard focus to that computer, including an in-flight request',async()=>{
 for(const busy of [false,true]){
  const h=harness();h.mount();await h.api.checkAccount();await flush();const held=deferred();
  if(busy)h.overrides.set(refs[0].endpoint_origin+'/api/owner/workspace/stage',()=>held.promise);
  const pending=h.api.act(0,'enroll');if(busy)await flush();else await pending;
  const row=h.ui.get('owner-session').children[1],proposal=proposalOf(row),cancel=proposal.children.at(-1);
  cancel.focus();cancel.click();assert.ok([row,optionOf(row,'enroll')].includes(h.ui.document.activeElement));
  assert.ok(!proposal.children.some(c=>c.textContent.includes('Approval code:')));
  if(busy)held.resolve(reply(202,{version:1,state:'awaiting_local_approval',expires_at:1800000120}));await pending;
  assert.equal(h.calls.filter(c=>c.url.endsWith('/workspace/stage')).length,1);h.api.dispose();
 }
});

test('temporary account loading is aria-disabled and activation guarded without blurring focus',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  const check=h.ui.get('owner-session').children[0].children[1];check.focus();
  const held=deferred();h.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2)+'&include=computer_names',()=>held.promise);
  const before=h.calls.length,pending=h.api.checkAccount();await flush();
  assert.equal(check.disabled,false);assert.equal(check.attributes['aria-disabled'],'true');assert.equal(h.ui.document.activeElement,check);
  check.click();assert.equal(h.calls.length,before+1);
  held.resolve(reply(200,h.metadata));await pending;
  assert.equal(check.attributes['aria-disabled'],'false');assert.equal(h.ui.document.activeElement,check);h.api.dispose();
});

test('successful account checks resume only a paused renderer; hidden documents have no account timer',async()=>{
  for(const count of [0,1]){
    const h=harness({count});let paused=0,resumed=0;h.mount({onPause:()=>paused++,onResume:()=>resumed++});
    await h.api.checkAccount();await flush();assert.equal(paused,1);assert.equal(resumed,1);
    await h.api.checkAccount();h.timer(30000);await flush();h.ui.listeners.get('visibilitychange')();await flush();assert.equal(resumed,1);
    h.ui.document.visibilityState='hidden';h.ui.listeners.get('visibilitychange')();
    const calls=h.calls.length;assert.equal(h.timers.some(t=>t.active&&t.ms===30000),false);
    await h.api.checkAccount();assert.equal(h.calls.length,calls+1);assert.equal(h.timers.some(t=>t.active&&t.ms===30000),false);
    h.ui.document.visibilityState='visible';h.ui.listeners.get('visibilitychange')();await flush();
    assert.equal(h.timers.some(t=>t.active&&t.ms===30000),true);assert.equal(resumed,1);h.api.dispose();
  }
});

test('retirement clears private focus text/dataset and disables every remaining scaffold control',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  const title=h.ui.get('focus-title');title.textContent='Private bot · private room';title.dataset.bot='private';title.dataset.fleet='private';
  h.metadataStatus=403;await h.api.checkAccount();
  assert.equal(title.textContent,'');assert.deepEqual(title.dataset,{});
  for(const button of [...h.ui.nav,h.ui.get('debug-toggle')])assert.equal(button.disabled,true);
  h.ui.observers[0].fn();assert.equal(h.ui.nav[0].disabled,true);h.api.dispose();
});

test('retirement selectors are pinned to actual canonical HTML',async()=>{
  const html=await read('index.html');
  for(const id of ['view-nav','debug-toggle','fleet-totals','presence-strip','task-detail-content','focus-pane','focus-title','task-detail','focus-overlay','search','fleet'])
    assert.match(html,new RegExp('id="'+id+'"'));
  const nav=html.match(/<nav id="view-nav"[^>]*>([\s\S]*?)<\/nav>/)[1];
  assert.deepEqual([...nav.matchAll(/data-view="([^"]+)"/g)].map(m=>m[1]),['channel','tasks','grid','fleet','trust']);
  assert.match(workspaceText,/#view-nav button, #debug-toggle, \[data-bot-inspect\]/);
  assert.match(workspaceText,/'focus-title'/);
});

test('partial client/transport admission is disposed and a later explicit account check retries cleanly',async()=>{
  for(const failAt of ['client','transport','subscription','stream']){
    let failing=true,created=0,disposed=0,stopped=0,subscribed=0,transportDisposed=0;
    const h=harness({dependencies:{
      createClient(){if(failing&&failAt==='client'&&created===1)throw new Error('second client');created++;
        return {createActions:()=>({invalidate(){}}),readHandle:{},snapshot:()=>({state:'login_required'}),subscribe(){subscribed++;if(failing&&failAt==='subscription'&&subscribed===2){throw new Error('subscription');}return()=>stopped++;},start:async()=>{},dispose(){disposed++;}};},
      createTransport(){if(failing&&failAt==='transport')throw new Error('brand');return {jget:async()=>ok({}),dispose(){transportDisposed++;},createEventSource(){if(failing&&failAt==='stream')throw new Error('stream');return {close(){},addEventListener(){}};}};},
    }});
    if(failAt==='stream')h.api.createEventSource('/api/stream');
    assert.equal(await h.api.checkAccount(),false);assert.equal(h.api.snapshot().phase,'unavailable');assert.equal(h.api.snapshot().computers.length,0);
    assert.equal(disposed,created);assert.equal((await h.api.jget('/api/tasks')).state,'unavailable');
    if(['subscription','stream'].includes(failAt))assert.equal(transportDisposed,1);
    if(failAt==='subscription')assert.equal(stopped,1);
    failing=false;assert.equal(await h.api.checkAccount(),true);await flush();assert.equal(h.api.snapshot().computers.length,2);
    assert.equal((await h.api.jget('/api/tasks')).state,'ok');h.api.dispose();assert.equal(disposed,created);
  }
});

test('unavailable admitted host exposes explicit Disconnect and uses existing carrier logout',async()=>{
  const h=harness({count:1});h.mount();await h.api.checkAccount();await flush();await h.api.act(0,'connect');
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/status',()=>reply(503,{}));
  await h.api.act(0,'check');assert.equal(h.api.snapshot().computers[0].state,'unavailable');
  const row=h.ui.get('owner-session').children[1];
  assert.equal(optionOf(row,'renew').disabled,true);assert.equal(optionOf(row,'logout').disabled,false);
  assert.equal(row.children[1].children[0].textContent,'Check connection');
  optionOf(row,'logout').click();await flush();assert.equal(h.calls.filter(c=>c.url.endsWith('/logout')).length,1);
  assert.equal(h.api.snapshot().computers[0].state,'signed_out');h.api.dispose();
});

test('enrollment resources join admission rollback, including partial streams, and a fresh retry disposes every resource',async()=>{
  for (const failAt of ['enrollment','transport','subscription','stream']) {
    let failing=true, clientsMade=0, clientsDisposed=0, enrollmentsMade=0, enrollmentsDisposed=0, subscriptions=0, stopped=0, opened=0, closed=0, transportsDisposed=0;
    const h=harness({dependencies:{
      createClient(){clientsMade++;return {createActions:()=>({invalidate(){}}),readHandle:{},snapshot:()=>({state:'unavailable'}),start:async()=>{},dispose(){clientsDisposed++;},subscribe(){subscriptions++;if(failing&&failAt==='subscription'&&subscriptions===2)throw new Error('second subscription');return()=>stopped++;}};},
      createEnrollment(){if(failing&&failAt==='enrollment'&&enrollmentsMade===1)throw new Error('second enrollment');enrollmentsMade++;return {snapshot:()=>({state:'idle',busy:false}),mountPanel(){},cancel(){},dispose(){enrollmentsDisposed++;}};},
      createTransport(){if(failing&&failAt==='transport')throw new Error('transport');return {jget:async()=>ok({}),dispose(){transportsDisposed++;},createEventSource(){opened++;if(failing&&failAt==='stream'&&opened===2)throw new Error('second stream');return {addEventListener(){},close(){closed++;}};}};},
    }});
    if(failAt==='stream'){h.api.createEventSource('/api/stream');h.api.createEventSource('/api/stream');}
    assert.equal(await h.api.checkAccount(),false);assert.equal(h.api.snapshot().phase,'unavailable');assert.equal(h.api.snapshot().computers.length,0);
    assert.ok(enrollmentsMade>0);assert.equal(enrollmentsDisposed,enrollmentsMade);assert.equal(clientsDisposed,clientsMade);
    assert.equal((await h.api.jget('/api/tasks')).state,'unavailable');
    if(failAt==='stream'){assert.equal(closed,1);assert.equal(stopped,2);assert.equal(transportsDisposed,1);}
    failing=false;assert.equal(await h.api.checkAccount(),true);await flush();assert.equal(h.api.snapshot().computers.length,2);
    h.api.dispose();assert.equal(enrollmentsDisposed,enrollmentsMade);assert.equal(clientsDisposed,clientsMade);
  }
});


test('connected message, nudge and feedback use canonical pending bookkeeping and raw HTTP at one source boundary',async()=>{
  const h=harness({count:1});await h.api.checkAccount();await flush();await h.api.act(0,'connect');await h.api.jget('/api/overview');
  const room=sourceA+' / team',values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)},state=new ActionState(storage);
  for(const [index,kind]of ['message','nudge','feedback'].entries()){
    const context=await h.api[kind==='message'?'interactionContext':kind+'Context'](room);
    assert.equal(context.scope.host,sourceA+'::host_'+'a'.repeat(32));
    const target={recipient:context.recipients[0].id,task_id:kind==='message'?null:sourceA+'::'+wi,
      ...(kind!=='message'?{assignment_id:null,release_id:context.release_id}:{})};
    const request=kind==='message'?state.begin(context,kind,target,'Literal <body>',id(20+index))
      :state.prepare(context,target,'Literal <body>',id(20+index));
    const sending=kind==='message'?request:state.beginPrepared(context,request,await h.api.prepareAction(request));
    assert.equal(state.accept(sending,await h.api.sendAction(sending)),'recorded');
    assert.equal(state.pending.length,1);assert.ok(!values.get('plane.pending-actions.v1').includes('Literal'));
    const restored=new ActionState(storage),saved=restored.pending[0];assert.equal(saved.request_id,id(20+index));
    assert.equal(restored.accept(saved,await h.api.actionReceipt(saved)),'delivered');state.pending=restored.pending;
  }
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/send')).length,3);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/prepare')).length,2);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/receipt')).length,3);
  for(const call of h.calls.filter(c=>c.url.includes('/actions/'))){assert.equal(call.init.credentials,'omit');assert.equal(call.init.headers['X-Claudlobby-Owner'],'1');assert.ok(!call.init.body.includes(sourceA+'::'));assert.ok(!call.init.body.includes(sourceA+' / '));}
  assert.equal(h.api.snapshot().computers[0].state,'ready');h.api.dispose();
});

test('wrong source targets/scopes cannot reach either same-named lead; missing grants preserve read access',async()=>{
  const h=harness();await h.api.checkAccount();await flush();await h.api.act(0,'connect');await h.api.act(1,'connect');await h.api.jget('/api/overview');
  const context=await h.api.feedbackContext(sourceA+' / team');
  const request={version:2,request_id:id(30),kind:'feedback',scope:context.scope,target:{recipient:context.recipients[0].id,task_id:sourceA+'::'+wi,assignment_id:null,release_id:context.release_id},submitted_at:'2026-10-10T00:00:00Z',body:'Comment'};
  const before=h.calls.length;
  for(const patch of [{target:{...request.target,recipient:sourceB+'::actor_'+'c'.repeat(32)}},{target:{...request.target,task_id:sourceB+'::'+wi}},{scope:{...request.scope,fleet:sourceB+' / team'}},{scope:{...request.scope,viewer:'forged'}}])await assert.rejects(h.api.prepareAction({...request,...patch}),/unknown/);
  assert.equal(h.calls.length,before);
  h.grants.delete('nudge');const statusBefore=h.calls.filter(c=>c.url.endsWith('/status')).length;
  await assert.rejects(h.api.nudgeContext(sourceA+' / team'),{code:'action_not_authorized'});
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length-statusBefore,1);
  assert.equal(h.api.snapshot().computers[0].state,'ready');assert.equal((await h.api.jget('/api/overview')).data.coverage.reachable,2);h.api.dispose();
});

for(const recovery of ['renew','failed-connect'])
 test(`late send through ${recovery} stays unknown with original UUID and never replays`,async()=>{
  const h=harness({count:1});await h.api.checkAccount();await flush();await h.api.act(0,'connect');await h.api.jget('/api/overview');
  const context=await h.api.interactionContext(sourceA+' / team'),state=new ActionState({getItem:()=>null,setItem(){}});
  const request=state.begin(context,'message',{recipient:context.recipients[0].id,task_id:null},'Keep me',id(40));
  const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/actions/send',()=>held.promise);
  const send=h.api.sendAction(request);await flush();
  if(recovery==='renew')await h.api.act(0,'renew');
  else {
    const door=refs[0].endpoint_origin+'/api/owner/connected/issue';
    h.overrides.set(door,()=>reply(503,{}));assert.equal(await h.api.act(0,'connect'),false);
    h.overrides.delete(door);assert.equal(await h.api.act(0,'connect'),true);
  }
  const {body,...metadata}=request;const raw={...metadata,scope:{workspace:'connected-test',host:'host_'+'a'.repeat(32),fleet:'team',viewer:'a-message-grant1'},target:{recipient:'actor_'+'c'.repeat(32),task_id:null},version:1,status:'delivered'};
  held.resolve(reply(200,raw));await assert.rejects(send,/unknown/);assert.equal(state.pending[0].request_id,id(40));
  await h.api.interactionContext(sourceA+' / team');h.receipts.set('a'+request.request_id,raw);
  assert.equal(state.accept(state.pending[0],await h.api.actionReceipt(state.pending[0])),'delivered');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/send')).length,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/receipt')).length,1);h.api.dispose();
});

test('out-of-order team and same-team context replies cannot replace the newest capability before send or receipt',async()=>{
  const h=harness({count:1});await h.api.checkAccount();await flush();await h.api.act(0,'connect');
  const endpoint=refs[0].endpoint_origin,overview=endpoint+'/api/owner/connected/read/overview';
  h.overrides.set(overview,()=>reply(200,ok({fleets:['alpha','beta'].map((alias,i)=>({alias,uid:'fleet_'+String(i+1).repeat(32),bots:1,provisional:0})),host:{daemon_serving:true},totals:{}})));
  await h.api.jget('/api/overview');
  const held=deferred(),door=endpoint+'/api/owner/connected/actions/context';
  const raw=room=>({version:1,room,scope:{workspace:'connected-test',host:'host_'+'a'.repeat(32),fleet:room,viewer:'a-message-grant1'},simulation:false,actions:['message'],recipients:[{id:'actor_'+'c'.repeat(32),label:'Lead',lead:true}]});
  h.overrides.set(door,init=>JSON.parse(init.body).room==='alpha'?held.promise:reply(200,raw('beta')));
  const a=h.api.interactionContext(sourceA+' / alpha'),b=await h.api.interactionContext(sourceA+' / beta');
  held.resolve(reply(200,raw('alpha')));assert.equal(await a,null);
  const state=new ActionState({getItem:()=>null,setItem(){}}),request=state.begin(b,'message',{recipient:b.recipients[0].id,task_id:null},'Newest team',id(60));
  assert.equal(state.accept(request,await h.api.sendAction(request)),'recorded');
  assert.equal(state.accept(state.pending[0],await h.api.actionReceipt(state.pending[0])),'delivered');
  const older=deferred();let reads=0;
  h.overrides.set(door,()=>++reads===1?older.promise:reply(200,raw('beta')));
  const first=h.api.interactionContext(sourceA+' / beta'),latest=await h.api.interactionContext(sourceA+' / beta');
  older.resolve(reply(200,{...raw('beta'),scope:{...raw('beta').scope,viewer:'stale-grant'}}));assert.equal(await first,null);
  const next=state.begin(latest,'message',{recipient:latest.recipients[0].id,task_id:null},'Newest refresh',id(61));
  assert.equal(state.accept(next,await h.api.sendAction(next)),'recorded');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/send')).length,2);h.api.dispose();
});

test('original pending receipt identity survives reordering or removal of another saved reference',async()=>{
  for(const references of [[refs[1],refs[0]],[refs[1]]]){
    const original=harness();await original.api.checkAccount();await flush();await original.api.act(1,'connect');await original.api.jget('/api/overview');
    const context=await original.api.interactionContext(sourceB+' / team'),values=new Map(),storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
    const state=new ActionState(storage),request=state.begin(context,'message',{recipient:context.recipients[0].id,task_id:null},'Original UUID',id(62));
    state.accept(request,await original.api.sendAction(request));original.api.dispose();
    const reopened=harness({references});for(const [key,value] of original.receipts)reopened.receipts.set(key,value);
    await reopened.api.checkAccount();await flush();await reopened.api.act(0,'connect');await reopened.api.jget('/api/overview');
    const current=await reopened.api.interactionContext(sourceB+' / team'),restored=new ActionState(storage),pending=restored.pending[0];
    assert.deepEqual(current.scope,pending.scope);assert.equal(restored.accept(pending,await reopened.api.actionReceipt(pending)),'delivered');
    assert.equal(reopened.calls.filter(c=>c.url.endsWith('/actions/send')).length,0);
    assert.equal(reopened.calls.filter(c=>c.url.endsWith('/actions/receipt')).length,1);reopened.api.dispose();
  }
});

test('quiet source recovery in the existing overview refresh restores actions once without a new poller or mutation',async()=>{
  const h=harness({count:1}),room=sourceA+' / team';let refresh;
  h.mount({onActionSourceChange:key=>{assert.equal(key,sourceA);refresh=h.api.interactionContext(room);}});
  await h.api.checkAccount();await flush();await h.api.act(0,'connect');await h.api.jget('/api/overview');await h.api.interactionContext(room);
  const stream=h.api.createEventSource('/api/stream');await flush();
  const contextDoor=refs[0].endpoint_origin+'/api/owner/connected/actions/context',overviewDoor=refs[0].endpoint_origin+'/api/owner/connected/read/overview';
  h.overrides.set(contextDoor,()=>reply(503,{state:'unavailable'}));h.overrides.set(overviewDoor,()=>reply(200,{state:'unreadable',provenance:null,remediation:'Recorder source unavailable'}));
  h.streams[0].control.enqueue(new TextEncoder().encode('event: source\ndata: {"state":"unreadable"}\n\n'));await flush();assert.equal(await refresh,null);
  const before=h.calls.filter(c=>c.url===contextDoor).length;await h.api.jget('/api/overview');assert.equal(h.calls.filter(c=>c.url===contextDoor).length,before);
  h.overrides.delete(contextDoor);h.overrides.delete(overviewDoor);await h.api.jget('/api/overview');await flush();
  assert.equal((await refresh).room,room);assert.equal(h.calls.filter(c=>c.url===contextDoor).length,before+1);
  await h.api.jget('/api/overview');assert.equal(h.calls.filter(c=>c.url===contextDoor).length,before+1);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/actions/send')).length,0);stream.close();h.api.dispose();
});

test('stable source routing uses readable computer/team display labels without changing receipt metadata',async()=>{
  const h=harness({count:1});await h.api.checkAccount();await flush();await h.api.act(0,'connect');
  const overview=await h.api.jget('/api/overview'),choice=overview.data.fleet_choices[0],fleet=overview.data.fleets[0];
  assert.equal(fleet.alias,sourceA+' / team');assert.equal(choice.alias,fleet.alias);
  assert.equal(fleet.display_label,'a.example.test / team');assert.equal(choice.display_label,fleet.display_label);
  assert.equal(h.api.roomLabel(fleet.alias),fleet.display_label);
  const capability=await h.api.interactionContext(fleet.alias);assert.equal(capability.scope.fleet,fleet.alias);
  h.api.dispose();
});

for(const duplicate of [false,true]) test(`saved computer names stay presentation only before and after Connect (duplicates: ${duplicate})`,async()=>{
  const h=harness(),names=['Office <computer>',duplicate?'Office <computer>':'Workshop'];
  h.metadata={...h.metadata,computer_names:refs.map((ref,i)=>({reference_id:ref.reference_id,name:names[i]})).reverse()};
  h.mount();assert.equal(await h.api.checkAccount(),true);await flush();
  assert.equal(new URL(h.calls[0].url).searchParams.get('include'),'computer_names');
  const cards=h.ui.get('owner-session').children.filter(e=>e.className==='connection-card');
  for(let i=0;i<2;i++) {
    assert.equal(cards[i].children[0].textContent,names[i]+' · Not connected');
    assert.equal(cards[i].children[0].title,new URL(refs[i].endpoint_origin).hostname);
    assert.equal(await h.api.act(i,'connect'),true,'carrier must receive its unchanged identity-only context');
  }
  const overviewDoor=refs[0].endpoint_origin+'/api/owner/connected/read/overview';
  h.overrides.set(overviewDoor,()=>reply(200,ok({fleets:[{alias:'team',uid:fleetUID,bots:1,provisional:0}],host:{display_name:'Host override'},totals:{fleets:1,bots:1,provisional:0,working:0,attention:0,overdue:0,live_poll:'ok',recorder_gaps:[]}})));
  const overview=await h.api.jget('/api/overview');
  for(const [i,key] of [sourceA,sourceB].entries()) {
    assert.equal(overview.data.sources[i].label,names[i]);
    assert.equal(overview.data.fleets[i].display_label,names[i]+' / team');
    assert.equal(overview.data.fleets[i].alias,key+' / team');
    assert.equal(cards[i].children[0].textContent,names[i]+' · Connected');
    assert.equal(h.api.roomLabel(key+' / team'),names[i]+' / team');
  }
  const context=await h.api.interactionContext(sourceA+' / team');
  const state=new ActionState({getItem:()=>null,setItem(){}});
  const request=state.begin(context,'message',{recipient:context.recipients[0].id,task_id:null},'A saved-name comment',id(44));
  assert.equal(context.scope.fleet,sourceA+' / team');assert.match(request.target.recipient,new RegExp('^'+sourceA+'::actor_'));
  const receipt=await h.api.sendAction(request);assert.equal(receipt.request_id,request.request_id);assert.deepEqual(receipt.target,request.target);
  assert.equal(h.calls.filter(c=>c.url===refs[1].endpoint_origin+'/api/owner/connected/actions/send').length,0);
  const detail=await h.api.jget(`/api/tasks/${encodeURIComponent(sourceA+'::'+wi)}?fleet=${encodeURIComponent(sourceA+' / team')}`);
  assert.equal(detail.data.task.task_id,sourceA+'::'+wi);
  h.metadata={...h.metadata,context_generation:id(99)};
  assert.equal(await h.api.checkAccount(),false);assert.equal(h.api.snapshot().phase,'denied','existing name-change generation invalidation remains authoritative');
  h.api.dispose();
});

for(const names of [[],[{reference_id:id(99),name:'Other'}],refs.map(()=>({reference_id:refs[0].reference_id,name:'Same'})),
  refs.map(ref=>({reference_id:ref.reference_id,name:' '})),refs.map(ref=>({reference_id:ref.reference_id,name:'x'.repeat(81)})),
  refs.map(ref=>({reference_id:ref.reference_id,name:'bad\0name'})),refs.map(ref=>({reference_id:ref.reference_id,name:'\ud800'}))])
test(`display metadata requires bounded names exactly once per saved reference: ${JSON.stringify(names)}`,async()=>{
  const h=harness();h.metadata={...h.metadata,computer_names:names};
  assert.equal(await h.api.checkAccount(),false);assert.equal(h.api.snapshot().phase,'denied');
  assert.equal(h.calls.length,1);h.api.dispose();
});

test('overview completed before source loss cannot consume the later recovery refresh',async()=>{
  const reached=deferred(),release=deferred();let hold=false,refresh;
  const h=harness({count:1,dependencies:{createTransport(options){
    const actual=createWorkspaceReadTransport(options);
    return {...actual,async jget(url){const value=await actual.jget(url);if(hold&&url==='/api/overview'){hold=false;reached.resolve();await release.promise;}return value;}};
  }}}),room=sourceA+' / team';
  h.mount({onActionSourceChange:()=>{refresh=h.api.interactionContext(room);}});
  await h.api.checkAccount();await flush();await h.api.act(0,'connect');await h.api.jget('/api/overview');
  const stream=h.api.createEventSource('/api/stream');await flush();hold=true;
  const old=h.api.jget('/api/overview');await reached.promise;
  const door=refs[0].endpoint_origin+'/api/owner/connected/actions/context';
  h.overrides.set(door,()=>reply(503,{state:'unavailable'}));
  h.streams[0].control.enqueue(new TextEncoder().encode('event: source\ndata: {"state":"unreadable"}\n\n'));await flush();assert.equal(await refresh,null);
  const before=h.calls.filter(c=>c.url===door).length;release.resolve();await old;await flush();
  assert.equal(h.calls.filter(c=>c.url===door).length,before,'stale healthy read must not spend recovery');
  h.overrides.delete(door);await h.api.jget('/api/overview');await flush();assert.equal((await refresh).room,room);
  assert.equal(h.calls.filter(c=>c.url===door).length,before+1);
  await h.api.jget('/api/overview');assert.equal(h.calls.filter(c=>c.url===door).length,before+1);
  stream.close();h.api.dispose();
});

test('connected permission decisions unwrap only their host and inspect the original receipt after a lost response',async()=>{
 const h=harness();await h.api.checkAccount();await h.api.act(0,'connect');await h.api.act(1,'connect');
 await h.api.jget('/api/overview');
 const permission={permission_id:'perm_'+'1'.repeat(32),task_id:wi,request_event_id:'ev_'+'2'.repeat(32),assignment_id:null,
  requester:{uid:'actor_'+'3'.repeat(32),alias:'Lead'},scope:{action:'deploy',target:'example.test',constraints:'Staging only'},
  scope_sha256:'4'.repeat(64),reason:'Review release',state:'pending',decided_by:null,decision_reason:null};
 const capability={version:2,room:'team',scope:{workspace:'connected-test',host:'host_'+'a'.repeat(32),fleet:'team',viewer:'permission-grant'},
  simulation:false,actions:['permission'],release_id:'r-'+'5'.repeat(64),task_id:wi,permissions:[permission],has_more:false};
 // A full page of valid quoted text exceeds the old 512 KiB context cap.
 capability.permissions=Array.from({length:50},(_,i)=>({...permission,permission_id:'perm_'+i.toString(16).padStart(32,'0'),
  scope:{...permission.scope,target:'"'.repeat(1024),constraints:'"'.repeat(4096)},reason:'"'.repeat(2048)}));
 capability.permissions[0]=permission;capability.has_more=true;
 assert.ok(new TextEncoder().encode(JSON.stringify(capability)).length>524288);
 const url=refs[0].endpoint_origin+'/api/owner/connected/actions/';let sent;
 h.overrides.set(url+'context',init=>{assert.deepEqual(JSON.parse(init.body),{room:'team',kind:'permission',task_id:wi});return reply(200,capability);});
 h.overrides.set(url+'prepare',init=>{const {body,...metadata}=JSON.parse(init.body);assert.equal(metadata.target.task_id,wi);assert.equal(metadata.target.permission_id,permission.permission_id);return reply(200,{...metadata,semantic_sha256:'6'.repeat(64)});});
 h.overrides.set(url+'send',init=>{sent=JSON.parse(init.body);throw Error('Lost response');});
 h.overrides.set(url+'receipt',init=>{const metadata=JSON.parse(init.body);const {body,...original}=sent;assert.deepEqual(metadata,original);return reply(200,{...metadata,status:'recorded',execution:'not_requested',notification:'not_requested',permission:{...permission,state:'approved',decided_by:{uid:'actor_'+'7'.repeat(32),alias:'Owner'},decision_reason:body}});});
 const c=await h.api.permissionContext(sourceA+' / team',sourceA+'::'+wi);
 assert.equal(c.permissions[0].requester.uid,sourceA+'::'+permission.requester.uid);
 const state=new ActionState({getItem:()=>null,setItem(){}}),p=c.permissions[0],target={permission_id:p.permission_id,task_id:p.task_id,scope_sha256:p.scope_sha256,release_id:c.release_id};
 const request=state.prepare(c,target,'Approved scope',id(8),'approve');
 const before=h.calls.length;
 await assert.rejects(()=>h.api.prepareAction({...request,target:{...target,permission_id:sourceB+'::'+permission.permission_id}}));
 assert.equal(h.calls.length,before);
 const sending=state.beginPrepared(c,request,await h.api.prepareAction(request));await assert.rejects(()=>h.api.sendAction(sending));
 const result=await h.api.actionReceipt(state.pending[0]);state.accept(state.pending[0],result);
 assert.equal(result.permission.permission_id,p.permission_id);assert.equal(state.pending.length,0);
 assert.equal(h.calls.filter(c=>c.url.endsWith('/send')).length,1);h.api.dispose();
});


test('never-connected computer stays distinct from an admitted source panel failure',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();await h.api.act(0,'connect');
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/read/overview',()=>reply(503,{state:'unavailable'}));
  const overview=await h.api.jget('/api/overview');
  assert.equal(overview.data.sources[0].connection_state,'ready');
  assert.equal(overview.data.sources[0].state,'unavailable');
  assert.equal(overview.data.sources[1].connection_state,'not_connected');
  assert.equal(overview.data.sources[1].state,'not_connected');
  assert.match(overview.data.sources[0].remediation,/This panel could not be read/);
  assert.equal((await h.api.jget('/api/tasks')).state,'ok');
  const cards=h.ui.get('owner-session').children.filter(e=>e.className==='connection-card');
  assert.match(cards[1].children[0].textContent,/b.example.test.*Not connected/);
  assert.doesNotMatch(cards[1].children[0].textContent,/denied/i);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,1);
  h.api.dispose();
});


test('long saved hostname remains a valid reference with a bounded presentation label',async()=>{
 const reference={...refs[0],endpoint_origin:'https://'+'a'.repeat(63)+'.'+'b'.repeat(63)+'.example.test'};
 const h=harness({count:1,dependencies:{documentContext:{...documentContext,references:[reference]}}});
 h.metadata=context([reference]);assert.equal(await h.api.checkAccount(),true);
 assert.equal(h.api.snapshot().phase,'ready');h.api.dispose();
});

test('actual host rejection is distinct from a missing computer session',async()=>{
 const h=harness({count:1});await h.api.checkAccount();await flush();
 h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/challenge',()=>reply(403,{state:'denied'}));
 assert.equal(await h.api.act(0,'connect'),false);
 const view=await h.api.jget('/api/overview');
 assert.equal(view.data.sources[0].connection_state,'denied');
 assert.equal(view.data.sources[0].state,'denied');h.api.dispose();
});
test('actual setup HTTP denial retains status for managed-handoff classification without new auth',async()=>{
  const clients=[];
  const h=harness({count:1,dependencies:{createClient:input=>{const c=createConnectedReadClient(input);clients.push(c);return c;}}});
  await h.api.checkAccount();await h.api.act(0,'connect');
  const issued=h.calls.filter(c=>c.url.endsWith('/issue')).length;
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/setup/receipt',()=>reply(403,{state:'denied'}));
  await assert.rejects(clients[0].setupReceipt({generation:id(9),request_id:id(10),config_fingerprint:'a'.repeat(64),source_revision:'b'.repeat(40)}),
    error=>error.status===403);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,issued);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/renew')).length,0);h.api.dispose();
});

const setupOnlyIssue=()=>reply(200,{version:1,state:'setup_only',token:'A'.repeat(43),expires_in:900,
  setup_profile:{version:2,profile:'first-team-setup-v2',host_uid:'host_'+'a'.repeat(32)},session_context:{issuer:documentContext.shell_origin,
    subject:documentContext.user_id,workspace_id:documentContext.workspace_id,operational_origin:documentContext.operational_origin,binding_revision:1,binding_generation:id(6)}});
test('setup-only computer stays visible without blocking the other admitted dashboard',async()=>{
  const h=harness(),a=refs[0].endpoint_origin,attention=[];
  let computersOpen=false;
  h.mount({onConnectionAttention:event=>{attention.push(event);computersOpen=true;}});
  h.overrides.set(a+'/api/owner/connected/issue',setupOnlyIssue);
  await h.api.checkAccount();
  const card=h.ui.get('owner-session').children[1],primary=card.children[1].children[0],goto=card.children[1].children[1],help=card.children.at(-1);
  const rail=h.ui.get('rail-channel'),panel=descendants(help).find(n=>n.className==='setup-focus-panel');
  const summary=help.children[0];summary.focus();
  assert.equal(panel.parentNode,help,'before setup-only the panel stays in the sidebar card');assert.equal(goto.hidden,true);
  await h.api.act(0,'connect');
  assert.equal(computersOpen,true);
  assert.equal(attention.filter(event=>event.source===sourceA&&event.state==='setup_only').length,1);
  const host=rail.children[0];
  assert.equal(host.id,'setup-focus');assert.equal(panel.parentNode,host,'setup-only moves the panel into the main area');
  assert.equal(host.hidden,false);assert.equal(rail.dataset.setupFocus,'only','with nothing readable, setup replaces the main area');
  assert.equal(descendants(panel).find(n=>n.tagName==='H2').textContent,'Finish setting up a.example.test');
  assert.ok(!help.open,'the sidebar card stays compact');assert.equal(goto.hidden,false);
  assert.match(primary.className,/connection-primary-quiet/,'Go to setup leads; Disconnect is quiet');
  assert.match(card.children[2].textContent,/setup panel/);
  assert.equal(h.ui.document.activeElement,summary,'revealing setup never moves focus');
  goto.click();assert.equal(h.ui.document.activeElement,descendants(panel).find(n=>n.tagName==='H2'),'Go to setup returns to the panel');
  summary.focus();help.open=false;
  await h.api.act(1,'connect');
  assert.equal(rail.dataset.setupFocus,'shared','beside a readable computer the panel sits above its teams');
  const attentionCount=attention.length;computersOpen=false;
  await h.api.checkAccount();
  assert.equal(attention.length,attentionCount,'unchanged source state never repeats attention');
  assert.equal(computersOpen,false,'manual collapse of the outer computer disclosure survives refresh');
  assert.equal(help.open,false,'later connection and account paints preserve manual collapse');
  assert.ok(!h.ui.get('owner-session').children[2].children.at(-1).open,'another computer is not expanded');
  assert.equal(h.ui.document.activeElement,summary);
  assert.equal(h.calls.filter(c=>c.url.includes('/api/owner/connected/setup/')).length,0,'revealing guidance never checks approval or launches setup');
  assert.equal(h.api.snapshot().computers[0].state,'setup_only');
  const fleets=await h.api.jget('/api/fleets');
  assert.equal(fleets.data.fleets.length,1); assert.ok(fleets.data.fleets[0].alias.includes(sourceB));
  assert.equal(h.calls.filter(c=>c.url.startsWith(a+'/api/owner/connected/read/')).length,0);
  assert.equal(primary.textContent,'Disconnect');assert.equal(primary.disabled,false);
  h.overrides.set(a+'/api/owner/connected/logout',init=>{assert.equal(init.headers.Authorization,'Bearer '+'A'.repeat(43));return reply(200,{version:1,state:'signed_out'});});
  assert.equal(await h.api.act(0,'logout'),true);
  assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
  assert.equal(panel.parentNode,help,'leaving setup-only returns the panel to its card');assert.equal(host.hidden,true);
  assert.equal(rail.dataset.setupFocus,undefined);assert.equal(goto.hidden,true);assert.doesNotMatch(primary.className,/quiet/);
  assert.equal(h.api.snapshot().computers[0].state,'signed_out');
  assert.equal(h.api.snapshot().computers[1].state,'ready');
  assert.equal(h.calls.filter(c=>c.url.startsWith(a+'/api/owner/connected/read/')).length,0);
  await h.api.act(0,'connect');
  assert.equal(panel.parentNode,host,'a new explicit setup-only connection shows the panel again');assert.ok(!help.open);
  assert.equal(h.calls.filter(c=>c.url.includes('/api/owner/connected/setup/')).length,0);
  const b=refs[1].endpoint_origin;
  const unavailable=()=>attention.filter(event=>event.source===sourceB&&event.state==='unavailable').length;
  const beforeUnavailable=unavailable();
  h.overrides.set(b+'/api/owner/connected/status',()=>reply(503,{}));
  await h.api.act(1,'check');
  assert.equal(unavailable(),beforeUnavailable+1);computersOpen=false;
  await h.api.checkAccount();assert.equal(computersOpen,false);assert.equal(unavailable(),beforeUnavailable+1);
  h.overrides.delete(b+'/api/owner/connected/status');await h.api.act(1,'check');
  assert.equal(h.api.snapshot().computers[1].state,'ready');
  h.overrides.set(b+'/api/owner/connected/status',()=>reply(503,{}));await h.api.act(1,'check');
  assert.equal(unavailable(),beforeUnavailable+2,'ready resets attention so a later unavailable transition reveals once');
  assert.equal(computersOpen,true);
  h.api.dispose();
});

const {createGuidedSetup}=await import(new URL('../claudlobby/plane/ui/guided-setup.js',import.meta.url));
function setupHarness(options={}) {
  const calls=[],listeners=new Set(),saved=new Map(options.saved); let state={state:'setup_only',epoch:1};
  const pending={generation:id(9),request_id:id(10),config_fingerprint:'a'.repeat(64),source_revision:'b'.repeat(40)};
  const context={version:2,template:'revenue-web-app',fleet:'shop',repository:'example-org/shop',source_revision:pending.source_revision,
    generation:id(9),expires_at:1800003600,request:null,launch_reserved:false};
  const preview={...pending,version:2,fleets:['shop'],changes:[],effects:{restart_bots:['shop/lead'],reload_supervision:true},
    dispatch:'prepared',phase:'quiesce',expires_at:1800000600};
  const client={snapshot:()=>state,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},
    async setupContext(){calls.push(['context']);return context;},
    async setupPreview(input){calls.push(['preview',input]);return preview;},
    async setupLaunch(input){assert.deepEqual(JSON.parse(saved.get('test')).pending,pending);calls.push(['launch',input]);throw new Error('lost');},
    async setupReceipt(input){calls.push(['receipt',input]);return {...input,version:2,state:'done',launch_reserved:true,config_committed:true,health_errors:{}};},...options.client};
  const storage={getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,value)};
  const api=createGuidedSetup({client,storage,storageKey:'test',now:()=>1800000000,...options.setupOptions});
  return {api,client,calls,saved,pending,context,preview,storage,
    change(next){state={...state,...next};api.connectionChanged(state);for(const fn of listeners)fn(state);}};
}
test('preview owns original Git request before one explicit launch; lost response and reload only inspect',async()=>{
  const h=setupHarness();assert.equal(h.calls.length,0);
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');
  assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.deepEqual(JSON.parse(h.saved.get('test')).pending,h.pending);
  h.api.setGoal('Changed goal');assert.equal(h.api.snapshot().goal,'Improve checkout tests','a real preview freezes its goal');
  assert.equal(await h.api.run('preview'),false);
  assert.equal(await h.api.run('launch'),false);
  assert.deepEqual(h.calls.at(-1),['launch',h.pending]);
  await h.api.run('launch');assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);
  h.api.dispose();
  const resumed=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.deepEqual(resumed.snapshot().pending,h.pending);
  assert.equal(await resumed.run('launch'),false);
  await resumed.run('receipt');assert.equal(h.calls.at(-1)[0],'receipt');
  assert.match(resumed.snapshot().note,/Team launched.*message the lead/);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);resumed.dispose();
});
test('retained launch explains local handoff and cannot launch a second time',async()=>{
  let launches=0;
  const h=setupHarness({client:{async setupLaunch(input){launches++;
    return {...input,version:2,state:'quiesce',launch_reserved:true,config_committed:false,health_errors:{}};}}});
  await h.api.run('context');h.api.setGoal('Review local work');await h.api.run('preview');
  assert.equal(await h.api.run('launch'),true);
  assert.match(h.api.snapshot().note,/handoff.*status and reconnect/);
  assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.equal(await h.api.run('launch'),false);
  assert.equal(launches,1);
  h.api.dispose();
});
test('managed handoff only checks the original receipt and existing connection after socket outage',async()=>{
  const timers=[];let reads=0,checks=0;
  const h=setupHarness({setupOptions:{setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}},
    client:{async check(){checks++;},async setupReceipt(input){reads++;
      assert.deepEqual(input,h.pending);
      if(reads===1)throw new Error('owner handoff gap');
      return {...input,version:2,state:'done',managed_owner:true,launch_reserved:true,config_committed:true,health_errors:{}};}}});
  h.context.managed_owner=true;
  await h.api.run('context');h.api.setGoal('Review local work');await h.api.run('preview');
  await h.api.run('launch');
  assert.equal(timers.length,1);
  await timers.shift()();await timers.shift()();
  assert.equal(h.calls.filter(row=>row[0]==='launch').length,1);
  assert.equal(reads,2);assert.equal(checks,1);
  assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.match(h.api.snapshot().note,/message the lead.*setup goal alone does not start a task/);
  h.api.dispose();
});
test('managed handoff stops at denied connection without issuing or replaying effects',async()=>{
  const timers=[];let reads=0,checks=0;
  const h=setupHarness({setupOptions:{setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}},
    client:{async check(){checks++;},async setupReceipt(){reads++;throw new Error();}}});
  h.context.managed_owner=true;
  await h.api.run('context');h.api.setGoal('Review local work');await h.api.run('preview');await h.api.run('launch');
  h.change({state:'denied',epoch:2});await timers.shift()();
  assert.equal(reads,0);assert.equal(checks,0);
  assert.equal(h.calls.filter(row=>row[0]==='launch').length,1);
  h.api.dispose();
});
test('managed handoff pauses hidden reads and resumes the same original when visible',async()=>{
  const timers=[];let visible=true,reads=0;
  const h=setupHarness({setupOptions:{setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},visible:()=>visible},
    client:{async check(){},async setupReceipt(input){reads++;return {...input,version:2,state:'done',managed_owner:true,launch_reserved:true,config_committed:true,health_errors:{}};}}});
  h.context.managed_owner=true;
  await h.api.run('context');h.api.setGoal('Review work');await h.api.run('preview');await h.api.run('launch');
  visible=false;await timers.shift()();assert.equal(reads,0);
  visible=true;h.api.resumeHandoff();await timers.shift()();
  assert.equal(reads,1);assert.equal(h.calls.filter(row=>row[0]==='launch').length,1);h.api.dispose();
});
test('managed handoff reports bounded pause and stops on actual HTTP refusal metadata',async()=>{
  const timers=[];let time=1800000000,reads=0;
  const h=setupHarness({setupOptions:{now:()=>time,setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}},
    client:{async setupReceipt(){reads++;const error=new Error('refused');error.status=403;throw error;}}});
  h.context.managed_owner=true;
  await h.api.run('context');h.api.setGoal('Review work');await h.api.run('preview');await h.api.run('launch');
  await timers.shift()();assert.equal(reads,1);assert.equal(timers.length,0);
  assert.match(h.api.snapshot().note,/refused.*launch is not repeated/);h.api.dispose();
  const held=[];
  const slow=setupHarness({setupOptions:{now:()=>time,setTimeout:fn=>{held.push(fn);return held.length;},clearTimeout:()=>{}},
    client:{async setupReceipt(){throw new Error('outage');}}});
  slow.context.managed_owner=true;
  await slow.api.run('context');slow.api.setGoal('Review work');await slow.api.run('preview');await slow.api.run('launch');
  time+=601;await held.shift()();
  assert.match(slow.api.snapshot().note,/Automatic checks paused/);slow.api.dispose();
});
test('changed connection fences held preview, preserves goal and never launches',async()=>{
  const held=deferred(),h=setupHarness({client:{setupPreview:()=>held.promise}});
  await h.api.run('context');h.api.setGoal('Improve checkout tests');const reading=h.api.run('preview');
  h.change({epoch:2,state:'unavailable'});held.resolve(h.preview);assert.equal(await reading,false);
  assert.equal(h.api.snapshot().plan,null);assert.equal(h.api.snapshot().goal,'Improve checkout tests');
  assert.equal(await h.api.run('launch'),false);h.api.dispose();
});
const signedIn=h=>{h.context.provider_signin=true;h.context.signin_attempt={attempt_id:id(11),generation:id(9),state:'connected',reason:null};};
const refused=()=>Object.assign(new Error('refused'),{status:403});
const timedOut=()=>new Error('timed out');
const CMD="'/example/cli' --root '/example/data' host update continue";
const buttonOf=(panel,text)=>descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
const offered=(panel,text)=>{const b=buttonOf(panel,text);return !!b&&visible(b)&&!b.disabled;};
const doneSteps=panel=>{const s=Object.fromEntries(stepShape(panel));return [s.approve,s.signin];};
// What the computer says about this approval's Preview, read by read (the last answer repeats).
const said={none:()=>({preview_state:'none'}),preparing:()=>({preview_state:'preparing'}),
  ready:h=>({preview_state:'ready',request:{...h.pending},preview:{...h.preview,goal:'Improve tests'},continuation:CMD}),
  expired:h=>({preview_state:'expired',request:{...h.pending},continuation:CMD}),
  failed:()=>({preview_state:'failed',continuation:CMD}),failedUnrecorded:()=>({preview_state:'failed'})};
function previewHarness({answers,preview=timedOut,previewSent}={}) {
  const timers=[],ui=dom(),panel=new ui.Element(),clock={t:1800000000};let reads=0;
  const h=setupHarness({setupOptions:{now:()=>clock.t,setTimeout(fn,ms){const t={fn,ms,live:true};timers.push(t);return t;},clearTimeout(t){if(t)t.live=false;}},
    client:{async setupContext(){h.calls.push(['context']);
      const answer=reads++===0?said.none:answers[Math.min(reads-2,answers.length-1)];
      return {...h.context,...answer(h)};},
      async setupPreview(input){h.calls.push(['preview',input]);previewSent?.(h);throw preview();}}});
  signedIn(h);h.api.mountPanel({document:ui.document,element:panel});
  const live=()=>timers.filter(t=>t.live);
  return {h,panel,timers,clock,live,
    async start(){await h.api.run('context');h.api.setGoal('Improve tests');assert.equal(await h.api.run('preview'),false);await flush();},
    async tick(){const t=live()[0];assert.ok(t,'a check is scheduled');t.live=false;await t.fn();await flush();}};
}
const fail=error=>()=>{throw error;};
test('a timed-out Preview the computer is still preparing is checked again by itself until it is ready, with the steps kept',async()=>{
  const p=previewHarness({answers:[said.preparing,said.preparing,said.ready]}),{h,panel}=p;
  await p.start();
  assert.equal(h.api.snapshot().note,'Your computer is still preparing your team. This page checks again by itself.');
  assert.deepEqual(doneSteps(panel),['done','done'],'Approve setup and Sign in to Claude stay ticked');
  assert.equal(Object.fromEntries(stepShape(panel)).goal,'current');
  assert.equal(stepPart(panel,'goal','setup-progress').textContent,'Preparing your team preview…');
  assert.equal(offered(panel,'Preview team'),false,'the computer would refuse a second Preview');
  assert.equal(p.live().length,1);assert.equal(p.live()[0].ms,5000);
  await p.tick();
  assert.equal(h.api.snapshot().note,'Your computer is still preparing your team. This page checks again by itself.');
  assert.deepEqual(doneSteps(panel),['done','done']);
  await p.tick();
  assert.equal(h.api.snapshot().note,'Preview ready. Review it below, then choose Launch team.');
  assert.equal(offered(panel,'Launch team'),true);assert.deepEqual(filledIn(panel),['Launch team']);
  assert.deepEqual(doneSteps(panel),['done','done']);assert.equal(p.live().length,0,'checks stop once the preview is ready');
  await h.api.run('launch');assert.deepEqual(h.calls.at(-1),['launch',h.pending]);
  assert.equal(h.calls.filter(c=>c[0]==='preview').length,1);h.api.dispose();
});
test('automatic checks on a preparing preview stop after ten minutes and say so; Check preview looks again',async()=>{
  const p=previewHarness({answers:[said.preparing]}),{h,panel}=p;
  await p.start();
  p.clock.t+=600;await p.tick();
  assert.equal(p.live().length,0);
  assert.equal(h.api.snapshot().note,'Automatic checks stopped after 10 minutes. Your computer was still preparing your team then. Choose Check preview to look again.');
  assert.equal(offered(panel,'Check preview'),true);assert.deepEqual(filledIn(panel),['Check preview']);
  assert.deepEqual(doneSteps(panel),['done','done']);
  const before=h.calls.filter(c=>c[0]==='context').length;
  buttonOf(panel,'Check preview').click();await flush();
  assert.equal(h.calls.filter(c=>c[0]==='context').length,before+1);
  assert.equal(h.api.snapshot().note,'Your computer is still preparing your team. This page checks again by itself.');
  assert.equal(p.live().length,1,'looking again restarts the bounded checks');
  assert.deepEqual(doneSteps(panel),['done','done']);h.api.dispose();
});
test('a preview that ran out before Launch says this page cannot launch it and shows the local command',async()=>{
  const p=previewHarness({answers:[said.expired]}),{h,panel}=p;
  await p.start();
  assert.equal(h.api.snapshot().note,`The preview ran out before you chose Launch team, so this page can no longer launch it. To launch it from your computer, run there: ${CMD}`);
  assert.deepEqual(doneSteps(panel),['done','done']);assert.equal(offered(panel,'Launch team'),false);
  assert.equal(await h.api.run('launch'),false);assert.equal(p.live().length,0);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);h.api.dispose();
});
test('a preview that failed on the computer says so and shows the local command only when the computer has one',async()=>{
  for (const [answer,note] of [[said.failed,`Preparing your team stopped on your computer before it finished. To retry it there, and launch your team if that works, run: ${CMD}`],
      [said.failedUnrecorded,'Preparing your team stopped on your computer before it finished. This setup cannot go on from this page.']]) {
    const p=previewHarness({answers:[answer]}),{h,panel}=p;
    await p.start();
    assert.equal(h.api.snapshot().note,note);
    assert.deepEqual(doneSteps(panel),['done','done']);assert.equal(offered(panel,'Preview team'),false);
    assert.equal(offered(panel,'Check preview'),true);assert.equal(p.live().length,0);
    assert.equal(h.api.snapshot().goal,'Improve tests');h.api.dispose();
  }
});
test('a refused Preview and one that never arrived read differently when the computer has no preview, and Preview opens again',async()=>{
  for (const [preview,note] of [[refused,'Your computer refused to start the preview, so nothing was prepared.'],
      [timedOut,'The preview did not start on your computer, so nothing was prepared.']]) {
    const p=previewHarness({answers:[said.none],preview}),{h,panel}=p;
    await p.start();
    assert.equal(h.api.snapshot().note,note);assert.doesNotMatch(note,/try again/i,'no promise that trying again works');
    assert.deepEqual(doneSteps(panel),['done','done']);assert.equal(offered(panel,'Preview team'),true);
    assert.equal(offered(panel,'Check preview'),false);assert.equal(h.api.snapshot().goal,'Improve tests');h.api.dispose();
  }
});
test('a check the computer refuses and one that gets no answer read differently, keep the steps and offer Check preview',async()=>{
  for (const [answer,note] of [[fail(refused()),'Your computer refused to show this setup. Choose Check preview to look again.'],
      [fail(timedOut()),'Could not get an answer from your computer about the preview. Choose Check preview to look again.']]) {
    const p=previewHarness({answers:[answer,said.preparing]}),{h,panel}=p;
    await p.start();
    assert.equal(h.api.snapshot().note,note);assert.notEqual(h.api.snapshot().context,null);
    assert.deepEqual(doneSteps(panel),['done','done']);assert.equal(offered(panel,'Preview team'),false);
    assert.deepEqual(filledIn(panel),['Check preview']);
    // The approval's own re-check from Details keeps the steps too.
    buttonOf(panel,'Check setup approval again').click();await flush();
    assert.equal(h.api.snapshot().note,'Your computer is still preparing your team. This page checks again by itself.');
    assert.deepEqual(doneSteps(panel),['done','done']);h.api.dispose();
  }
});
test('a reload during preparation shows it, and a connection change says the checks stopped instead of a stuck check line',async()=>{
  const p=previewHarness({answers:[said.preparing]}),{h,panel}=p;
  await p.start();
  h.change({state:'expired',epoch:2});
  assert.equal(h.api.snapshot().note,'The connection to your computer changed, so this page stopped checking on the preview. Once connected again, choose Check setup approval.');
  assert.equal(p.live().length,0);h.api.dispose();
  // The connection goes inactive after the failed Preview, before its queued read runs.
  const q=previewHarness({answers:[said.preparing],preview:()=>{q.h.client.snapshot=()=>({state:'expired',epoch:1});return timedOut();}});
  await q.start();
  assert.notEqual(q.h.api.snapshot().note,'Checking what happened to the preview…');
  assert.equal(q.h.api.snapshot().note,'The connection to your computer changed, so this page stopped checking on the preview. Once connected again, choose Check setup approval.');
  q.h.api.dispose();
  // A fresh page whose first read finds the preparation shows it the same way.
  const r=setupHarness();signedIn(r);r.context.preview_state='preparing';await r.api.run('context');
  assert.equal(r.api.snapshot().note,'Your computer is still preparing your team. This page checks again by itself.');
  assert.equal(await r.api.run('preview'),false);r.api.dispose();
});
test('a preview that finished on the computer after this page gave up is picked up and can be launched',async()=>{
  const ui=dom(),panel=new ui.Element();
  const h=setupHarness({client:{async setupPreview(input){h.calls.push(['preview',input]);
    h.context.request={...h.pending};h.context.preview_state='ready';h.context.preview={...h.preview,goal:'Improve tests'};throw new Error('timed out');}}});signedIn(h);
  h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await flush();
  const snap=h.api.snapshot();
  assert.deepEqual(snap.pending,h.pending);assert.equal(snap.plan.phase,'quiesce');assert.equal(snap.goal,'Improve tests');
  assert.equal(snap.note,'Preview ready. Review it below, then choose Launch team.');
  const launch=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Launch team');
  assert.equal(launch.hidden,false);assert.equal(launch.disabled,false);
  assert.equal(Object.fromEntries(stepShape(panel)).signin,'done');
  assert.equal(h.calls.filter(c=>c[0]==='preview').length,1,'picking it up never previews a second time');
  await h.api.run('launch');assert.deepEqual(h.calls.at(-1),['launch',h.pending]);h.api.dispose();
});
test('reloading the approval shows a retained preview and offers Launch, never a second preview',async()=>{
  const h=setupHarness();signedIn(h);h.context.request={...h.pending};h.context.preview_state='ready';h.context.preview={...h.preview,goal:'Improve tests'};
  await h.api.run('context');
  assert.equal(h.api.snapshot().plan.request_id,h.pending.request_id);assert.equal(h.api.snapshot().goal,'Improve tests');
  assert.equal(await h.api.run('preview'),false);await h.api.run('launch');
  assert.equal(h.calls.filter(c=>c[0]==='preview').length,0);assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
});
test('a context read whose preview state contradicts its request is refused',async()=>{
  for (const bad of [{preview_state:'preparing',request:'pending'},{preview_state:'ready'},{preview_state:'expired'},
      {preview_state:'later'},{preview_state:'none',preview:'plan'},{preview_state:'ready',request:'pending',launch_reserved:true,preview:'plan'}]) {
    const h=setupHarness();
    Object.assign(h.context,bad,bad.request?{request:{...h.pending}}:{},bad.preview?{preview:{...h.preview,goal:'g'}}:{});
    assert.equal(await h.api.run('context'),false,JSON.stringify(bad));assert.equal(h.api.snapshot().context,null);h.api.dispose();
  }
});
test('a kept request the computer shows without a preview state points at Check launch status',async()=>{
  const h=setupHarness();h.context.request={...h.pending};h.context.continuation=CMD;await h.api.run('context');
  assert.equal(h.api.snapshot().note,'Choose Check launch status to see where setup stands on your computer.');h.api.dispose();
});
test('setup panel retains disclosure/focus and refuses launch without durable original identifiers',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  const detail=panel.children.find(e=>e.tagName==='DETAILS');detail.open=true;detail.focus();
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');
  assert.equal(panel.children.find(e=>e.tagName==='DETAILS'),detail);assert.equal(detail.open,true);assert.equal(ui.document.activeElement,detail);
  const authority=descendants(panel).find(e=>e.textContent.includes('This approval covers'));
  assert.ok(descendants(detail).includes(authority),'approval scope lives under Details');
  assert.ok(authority.textContent.includes(h.context.repository));assert.ok(authority.textContent.includes(h.context.source_revision));
  assert.match(authority.textContent,/for app version \S+ only\./);
  assert.match(authority.textContent,/starts the team and the app’s background services on this computer and may use the provider accounts you set up/);
  assert.match(authority.textContent,/Some folders, tools or accounts may still need your approval on this computer/);
  assert.match(authority.textContent,/Merging, deploying, extra spending and destructive changes each need your separate permission/);
  assert.ok(descendants(panel).some(e=>e.textContent==='Goal: Improve checkout tests'));
  h.storage.setItem=()=>{throw new Error('blocked');};assert.equal(await h.api.run('launch'),false);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);
  assert.match(h.api.snapshot().note,/could not save the setup identifiers, so nothing was launched/);
  h.api.dispose();
});
test('canonical request wins two-tab storage conflict, preserving both original bindings through reload',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  const losing={...h.pending,request_id:id(11)};h.saved.set('test',JSON.stringify(losing));
  const restored=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  h.context.request={...h.pending};h.context.launch_reserved=true;
  assert.equal(await restored.run('context'),true);
  assert.deepEqual(restored.snapshot().pending,h.pending);assert.deepEqual(restored.snapshot().conflicts,[losing]);
  await restored.run('receipt');assert.deepEqual(h.calls.at(-1),['receipt',h.pending]);
  const reloaded=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.deepEqual(reloaded.snapshot().pending,h.pending);assert.deepEqual(reloaded.snapshot().conflicts,[losing]);
  assert.equal(await reloaded.run('launch'),false);
  restored.dispose();reloaded.dispose();h.api.dispose();
});
test('null context or a new grant never clears the original request or permits another preview',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  await h.api.run('context');assert.deepEqual(h.api.snapshot().pending,h.pending);
  h.context.generation=id(12);await h.api.run('context');assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.equal(await h.api.run('preview'),false);assert.equal(await h.api.run('launch'),false);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
});
test('historical sealed identifiers remain visible and block a replacement Git request',async()=>{
  const h=setupHarness(),legacy={generation:id(9),plan_id:'p-'+'a'.repeat(64),request_id:id(10)};
  h.saved.set('test',JSON.stringify(legacy));
  const restored=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  const ui=dom(),panel=new ui.Element();restored.mountPanel({document:ui.document,element:panel});
  assert.deepEqual(restored.snapshot().legacy,[legacy]);assert.equal(restored.snapshot().pending,null);
  await restored.run('context');restored.setGoal('Improve tests');assert.equal(await restored.run('preview'),false);
  assert.ok(descendants(panel).some(e=>/identifiers from an older setup, so it cannot launch/.test(e.textContent)));
  assert.equal(h.saved.get('test'),JSON.stringify(legacy));assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);
  restored.dispose();h.api.dispose();
});
test('mixed legacy or changed-source previews are refused without dispatch',async()=>{
  for(const defect of ['legacy','source']) {
    const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');
    h.client.setupPreview=async()=>({...h.preview,...(defect==='legacy'?{release_id:'r-'+'a'.repeat(64)}:{source_revision:'c'.repeat(40)})});
    assert.equal(await h.api.run('preview'),false);assert.equal(h.api.snapshot().pending,null);
    assert.equal(await h.api.run('launch'),false);assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);h.api.dispose();
  }
});
test('unknown receipt after fresh preview retires launch permission without a launch POST',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  const launch=descendants(panel).find(e=>e.tagName==='BUTTON'&&e.textContent==='Launch team');
  assert.equal(launch.disabled,false);
  h.client.setupReceipt=async input=>({...input,version:2,state:'unknown',launch_reserved:false,
    config_committed:false,health_errors:{}});
  assert.equal(await h.api.run('receipt'),true);assert.match(h.api.snapshot().note,/Do not launch again/);
  assert.equal(launch.disabled,true);assert.equal(await h.api.run('launch'),false);
  assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);h.api.dispose();
});
test('prepared receipt shows exact server continuation and never launches or treats restoration errors as healthy',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');
  h.change({epoch:2,state:'setup_only'});
  const command="'/example/managed cli' --root '/example/data root' host update continue";
  h.client.setupReceipt=async input=>({...input,version:2,state:'quiesce',launch_reserved:false,
    config_committed:false,health_errors:{},continuation:command});
  assert.equal(await h.api.run('receipt'),true);assert.ok(h.api.snapshot().note.includes(command));
  assert.equal(await h.api.run('launch'),false);
  h.client.setupReceipt=async input=>({...input,version:2,state:'done',launch_reserved:true,
    config_committed:true,health_errors:{'example.service':'unknown start'}});
  assert.equal(await h.api.run('receipt'),true);assert.match(h.api.snapshot().note,/needs attention/);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);h.api.dispose();
});
test('expired or revoked receipt preserves original identifiers and local inspection guidance',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  h.client.setupReceipt=async()=>{throw new Error('approval expired');};
  assert.equal(await h.api.run('receipt'),false);assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.match(h.api.snapshot().note,/inspect the update on the computer if approval expired or was revoked/);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
});
test('failed approval refresh retires new-work permission while retaining original request and goal',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  const button=title=>descendants(panel).find(e=>e.tagName==='BUTTON'&&e.textContent===title);
  await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');
  const plan=h.api.snapshot().plan;
  assert.equal(button('Launch team').disabled,false);
  h.client.setupContext=async()=>{throw new Error('approval revoked');};
  assert.equal(await h.api.run('context'),false);assert.equal(h.api.snapshot().context,null);assert.equal(h.api.snapshot().plan,plan);
  assert.equal(h.api.snapshot().goal,'Improve tests');assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.equal(button('Preview team').disabled,true);assert.equal(button('Launch team').disabled,true);
  assert.equal(await h.api.run('launch'),false);
  await h.api.run('receipt');assert.deepEqual(h.calls.at(-1),['receipt',h.pending]);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);h.api.dispose();
});

test('explicit connected summary retry forwards only its source key and successful reconnect clears its latch',async()=>{
  const retries=[];
  const h=harness({count:1,dependencies:{createTransport(options){
    const transport=createWorkspaceReadTransport(options);
    return {...transport,retryOverview(key){retries.push(key);return transport.retryOverview(key);}};
  }}});
  assert.equal(h.api.retryOverview(sourceA),false);
  h.mount();await h.api.checkAccount();await flush();
  assert.equal(await h.api.act(0,'connect'),true);
  assert.deepEqual(retries,[sourceA]);
  assert.equal(h.api.retryOverview(sourceA),true);
  assert.equal(h.api.retryOverview(sourceB),false);
  assert.deepEqual(retries,[sourceA,sourceA,sourceB]);
  await h.api.act(0,'check');assert.equal(retries.length,3,'status checks cannot clear a timeout latch');
  h.api.dispose();assert.equal(h.api.retryOverview(sourceA),false);assert.equal(retries.length,3);

});


test('workspace document permits eight exact saved computers and refuses a ninth or duplicate', async () => {
  const references=Array.from({length:8},(_,i)=>({reference_id:id(40+i),endpoint_origin:`https://host${i}.example.test`,revision:1}));
  const h=harness({references});h.mount();await h.api.checkAccount();await flush();
  assert.equal(h.api.snapshot().phase,'ready');assert.equal(h.api.snapshot().computers.length,8);h.api.dispose();
  for(const bad of [[...references,{reference_id:id(60),endpoint_origin:'https://ninth.example.test',revision:1}],[...references.slice(0,7),references[0]]]) {
    const rejected=harness({references:bad});rejected.mount();await rejected.api.checkAccount();await flush();
    assert.notEqual(rejected.api.snapshot().phase,'ready');rejected.api.dispose();
  }
});

test('connected Git provenance survives qualification and old receipt inspection after source update',async()=>{
 const h=harness({count:1});await h.api.checkAccount();await h.api.act(0,'connect');await h.api.jget('/api/overview');
 const runtime={release_id:null,source_revision:'1'.repeat(40),config_fingerprint:'2'.repeat(64)};
 let current={...runtime};
 const door=refs[0].endpoint_origin+'/api/owner/connected/actions/';
 h.overrides.set(door+'context',init=>{const input=JSON.parse(init.body);return reply(200,{
  version:2,room:input.room,simulation:false,scope:{workspace:'connected-test',host:'host_'+'a'.repeat(32),fleet:input.room,viewer:'a-nudge-grant1'},
  recipients:[{id:'actor_'+'c'.repeat(32),label:'Lead',lead:true}],actions:['nudge'],...current});});
 const room=sourceA+' / team',c=await h.api.nudgeContext(room),state=new ActionState({getItem:()=>null,setItem(){}});
 assert.deepEqual({release_id:c.release_id,source_revision:c.source_revision,config_fingerprint:c.config_fingerprint},runtime);
 const target={recipient:c.recipients[0].id,task_id:sourceA+'::'+wi,assignment_id:null,...runtime};
 const request=state.prepare(c,target,'Reason',id(90));
 const sending=state.beginPrepared(c,request,await h.api.prepareAction(request));
 assert.equal(state.accept(sending,await h.api.sendAction(sending)),'recorded');
 current={...runtime,source_revision:'3'.repeat(40),config_fingerprint:'4'.repeat(64)};
 await h.api.nudgeContext(room);
 const before=h.calls.length;
 await assert.rejects(h.api.prepareAction(request),/unknown/);assert.equal(h.calls.length,before);
 const original=state.pending[0],result=await h.api.actionReceipt(original);
 assert.deepEqual(result.target,target);assert.equal(state.accept(original,result),'delivered');
 const receiptCall=h.calls.filter(c=>c.url===door+'receipt').at(-1),raw=JSON.parse(receiptCall.init.body);
 assert.deepEqual(raw.target,{...target,recipient:'actor_'+'c'.repeat(32),task_id:wi});
 assert.equal(state.pending.length,0);h.api.dispose();
});

for (const [operation, route, heading] of [['connect','challenge','Connecting'],['check','status','Checking connection']])
test(`computer heading describes pending ${operation} and restores the actual result`,async()=>{
  const h=harness({count:1});h.mount();await h.api.checkAccount();await flush();
  if(operation==='check')await h.api.act(0,'connect');
  const wait=deferred();
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/'+route,()=>wait.promise);
  const action=h.api.act(0,operation);await flush();
  const card=h.ui.get('owner-session').children.find(e=>e.className==='connection-card');
  assert.match(card.children[0].textContent,new RegExp(heading+'$'));
  assert.doesNotMatch(card.children[0].textContent,/Connection unavailable/);
  assert.ok(card.children[1].children.filter(e=>e.tagName==='BUTTON'&&!e.hidden).every(e=>e.disabled));
  wait.resolve(reply(503,{state:'unavailable'}));await action;await flush();
  assert.match(card.children[0].textContent,/Connection unavailable$/);
  h.api.dispose();
});

test('local-project approval renders locally bound scope and preserves original request on reload',async()=>{
  const h=setupHarness();
  Object.assign(h.context,{version:3,template:'local-project',project_directory:'/example/local-project'});
  delete h.context.repository;h.preview.version=3;
  h.client.setupReceipt=async input=>({...input,version:3,state:'done',launch_reserved:true,config_committed:true,health_errors:{}});
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  assert.equal(await h.api.run('context'),true);
  assert.ok(descendants(panel).some(e=>e.textContent.includes('/example/local-project')));
  assert.ok(descendants(panel).some(e=>e.textContent.startsWith('This approval covers reading and changing files in /example/local-project, for app version ')),'the local scope is disclosed');
  assert.equal(descendants(panel).filter(e=>e.tagName==='TEXTAREA').length,1,'only the goal is authored');
  h.api.setGoal('Review the approved local project');
  assert.equal(await h.api.run('preview'),true);
  assert.deepEqual(h.calls.find(c=>c[0]==='preview')[1],{generation:h.context.generation,goal:'Review the approved local project'});
  await h.api.run('launch');
  h.api.dispose();
  const resumed=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.equal(await resumed.run('launch'),false);
  await resumed.run('context');await resumed.run('receipt');
  assert.match(resumed.snapshot().note,/Team launched/);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);resumed.dispose();
});

test('setup refuses mixed local/repository protocol shapes before preview',async()=>{
  for(const fields of [{version:2,template:'local-project',project_directory:'/example/project'},
      {version:3,template:'revenue-web-app'},
      {version:3,template:'local-project',project_directory:'relative'}]){
    const h=setupHarness();Object.assign(h.context,fields);
    assert.equal(await h.api.run('context'),false);
    h.api.setGoal('Review local project');assert.equal(await h.api.run('preview'),false);
    assert.equal(h.calls.filter(c=>c[0]==='preview').length,0);h.api.dispose();
  }
});

test('Claude sign-in retains one attempt before effect and never remints after response loss',async()=>{
  let calls=[];
  const h=setupHarness({client:{async setupSignin(action,input){calls.push([action,input]);throw new Error('lost');}}});
  h.context.provider_signin=true;h.context.signin_attempt=null;
  await h.api.run('context');
  assert.equal(await h.api.run('signin-begin'),false);
  assert.equal(calls.length,1);assert.equal(calls[0][0],'begin');
  const attempt=JSON.parse(h.saved.get('test:claude'));
  assert.deepEqual(calls[0][1],attempt);
  await h.api.run('signin-begin');assert.equal(calls.length,1);
  await h.api.run('signin-status');assert.equal(calls[1][0],'status');assert.deepEqual(calls[1][1],attempt);
  const resumed=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  await resumed.run('context');await resumed.run('signin-begin');assert.equal(calls.length,2);
  await resumed.run('signin-cancel');assert.equal(calls[2][0],'cancel');
});

test('Claude sign-in UI accepts only host result with safe manual provider URL',async()=>{
  const h=setupHarness({client:{async setupSignin(_action,input){return {...input,state:'waiting',reason:null,url:'https://evil.example/steal'};}}});
  h.context.provider_signin=true;await h.api.run('context');const before=h.api.snapshot().note;
  assert.equal(await h.api.run('signin-begin'),false);
  assert.equal(h.api.snapshot().note,before,'a refused sign-in result leaves the setup note unchanged');
  assert.equal(h.saved.size,1);assert.ok(h.saved.has('test:claude'));
});

test('Claude code goes only to the selected host and never enters retained UI state',async()=>{
  const clients=[];
  const network=harness({count:1,dependencies:{createClient:input=>{const c=createConnectedReadClient(input);clients.push(c);return c;}}});
  await network.api.checkAccount();await network.api.act(0,'connect');
  const endpoint=refs[0].endpoint_origin+'/api/owner/connected/setup/signin-code';
  network.overrides.set(endpoint,()=>reply(200,{state:'starting'}));
  const offset=network.calls.length,code='sensitive-code#'+'b'.repeat(32);
  await clients[0].setupSignin('code',{generation:id(9),attempt_id:id(12),code});
  const sent=network.calls.slice(offset);assert.equal(sent.length,1);assert.equal(sent[0].url,endpoint);
  assert.equal(JSON.parse(sent[0].init.body).code,code);network.api.dispose();
  const ui=dom(),panel=new ui.Element();let input;
  const h=setupHarness({client:{async setupSignin(action,body){
    if(action==='code'){assert.equal(input.value,'');return {generation:body.generation,attempt_id:body.attempt_id,state:'starting',reason:null};}
    return {...body,state:'waiting',reason:null,url:'https://claude.com/cai/oauth/authorize?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference'};
  }}});
  h.context.provider_signin=true;h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');await h.api.run('signin-begin');
  input=descendants(panel).find(node=>node.dataset.field==='signin-code');
  input.value=code;await h.api.run('signin-code');
  assert.equal(input.value,'');assert.ok(!JSON.stringify(h.api.snapshot()).includes(code));
  assert.ok(!JSON.stringify([...h.saved]).includes(code));h.api.dispose();
});

const authorizeURL='https://claude.com/cai/oauth/authorize?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference';
function pollHarness(statuses,options={}) {
  const timers=[],calls=[],clock={t:1800000000}; let active=0,overlap=0;
  const ui=dom(),panel=new ui.Element();
  const h=setupHarness({setupOptions:{now:()=>clock.t,setTimeout(fn,ms){const t={fn,ms,live:true};timers.push(t);return t;},clearTimeout(t){if(t)t.live=false;},...options.setupOptions},
    client:{async setupSignin(action,body){
      calls.push([action,body]);active++;overlap=Math.max(overlap,active);
      try{
        if(action==='begin')return options.begin?options.begin(body):{...body,state:'starting',reason:null};
        const next=await statuses.shift();if(next instanceof Error)throw next;
        return {generation:body.generation,attempt_id:body.attempt_id,state:next,reason:null,...(next==='waiting'?{url:authorizeURL}:{})};
      }finally{active--;}
    }}});
  h.context.provider_signin=true;h.api.mountPanel({document:ui.document,element:panel});
  const link=descendants(panel).find(n=>n.tagName==='A'),code=descendants(panel).find(n=>n.dataset.field==='signin-code');
  const live=()=>timers.filter(t=>t.live);
  const fire=async()=>{const t=live()[0];assert.ok(t,'a poll is scheduled');t.live=false;t.fn();await flush();};
  return {h,panel,calls,clock,link,code,live,fire,timers,get overlap(){return overlap;}};
}
test('one serialized sign-in status poll follows the original attempt to its terminal result',async()=>{
  const gate=deferred(),p=pollHarness([gate.promise,'waiting','connected']);
  await p.h.api.run('context');assert.equal(p.live().length,0,'no poll before an attempt');
  await p.h.api.run('signin-begin');
  assert.equal(p.live().length,1);assert.equal(p.live()[0].ms,2000);
  const attempt=JSON.parse(p.h.saved.get('test:claude'));
  await p.fire(); // the status call is now held in flight
  assert.equal(p.calls.at(-1)[0],'status');assert.equal(p.live().length,0,'no second timer while a call is in flight');
  assert.equal(await p.h.api.run('signin-status'),false,'manual status serializes behind it');
  assert.equal(await p.h.api.run('signin-cancel'),false,'cancel serializes behind it');
  gate.resolve('waiting');await flush();
  assert.equal(p.link.hidden,false);assert.equal(p.live().length,1,'the next poll follows only after completion');
  p.code.value='typed-code';assert.equal(await p.h.api.run('signin-code'),true);assert.equal(p.code.value,'','code is cleared at once');
  await p.fire();
  assert.equal(p.h.api.snapshot().busy,false);assert.equal(p.live().length,0,'terminal state stops the poll');
  assert.equal(p.overlap,1,'calls never overlapped');
  assert.equal(p.calls.filter(c=>c[0]==='begin').length,1,'no automatic begin');
  assert.ok(p.calls.every(c=>c[1].attempt_id===attempt.attempt_id&&c[1].generation===attempt.generation),'the original attempt throughout');
  assert.deepEqual(JSON.parse(p.h.saved.get('test:claude')),attempt);p.h.api.dispose();
});
test('a poll error stops polling without replay; manual progress resumes it and keeps the original attempt',async()=>{
  const p=pollHarness([new Error('lost'),'waiting','waiting']);
  await p.h.api.run('context');await p.h.api.run('signin-begin');
  const attempt=p.h.saved.get('test:claude');
  await p.fire();assert.equal(p.live().length,0,'an error stops the timer');
  assert.equal(p.link.hidden,true);
  await p.h.api.run('signin-status');assert.equal(p.live().length,1,'an explicit check resumes polling');
  assert.equal(p.h.saved.get('test:claude'),attempt,'the retained attempt is untouched');
  assert.equal(p.calls.filter(c=>c[0]==='begin').length,1);p.h.api.dispose();
});
test('expiry, context loss and disposal stop the poll and clear link and code',async()=>{
  for(const loss of ['expiry','context','dispose']){
    const p=pollHarness(['waiting','waiting','waiting']);
    await p.h.api.run('context');await p.h.api.run('signin-begin');await p.fire();
    assert.equal(p.link.hidden,false);p.code.value='pending-code';
    if(loss==='expiry'){const before=p.calls.length;p.clock.t=1800003601;await p.fire();
      assert.equal(p.calls.length,before,'an expired approval sends no further status call');}
    else if(loss==='context')p.h.change({state:'ready',epoch:2});
    else p.h.api.dispose();
    assert.equal(p.live().length,0,loss);assert.equal(p.code.value,'',loss);
    if(loss!=='dispose')assert.equal(p.link.hidden,true,loss);
    const attempts=p.calls.length;await flush();assert.equal(p.calls.length,attempts,'no further call after '+loss);
    assert.ok(p.h.saved.has('test:claude'),'the original attempt is retained');
    if(loss!=='dispose')p.h.api.dispose();
  }
});
test('Preview and Launch wait for an open provider sign-in; setups without one are unchanged',async()=>{
  const p=pollHarness(['connected']);p.h.api.setGoal('Ship it');
  await p.h.api.run('context');await p.h.api.run('signin-begin');
  assert.equal(await p.h.api.run('preview'),false,'refused while the attempt is open');
  assert.ok(!p.h.calls.some(c=>c[0]==='preview'));
  await p.fire();
  assert.equal(await p.h.api.run('preview'),true,'allowed once connected');
  p.h.api.dispose();
  const plain=setupHarness();plain.api.setGoal('Ship it');await plain.api.run('context');
  assert.equal(await plain.api.run('preview'),true);
});

test('mounted ready session renews before begin and keeps the approved context, link and one serialized poll',async()=>{
  const restore=stubLocalStorage();
  try {
  const h=harness({count:1}),origin=refs[0].endpoint_origin+'/api/owner/connected/setup/',ROTATED_TOKEN='R'.repeat(42)+'A';
  await h.api.checkAccount();h.mount();await h.api.act(0,'connect');
  const body={version:2,template:'revenue-web-app',repository:'example-org/shop',fleet:'shop',source_revision:'b'.repeat(40),
    generation:id(9),expires_at:1800003600,request:null,launch_reserved:false,provider_signin:true,signin_attempt:null};
  const seen=[];let attemptId=null;
  h.overrides.set(origin+'context',()=>{seen.push('context');return reply(200,body);});
  h.overrides.set(origin+'signin-begin',init=>{
    seen.push('begin');assert.equal(init.headers.Authorization,'Bearer '+ROTATED_TOKEN,'begin used the rotated bearer');
    attemptId=JSON.parse(init.body).attempt_id;
    return reply(200,{generation:id(9),attempt_id:attemptId,state:'waiting',reason:null,url:authorizeURL});});
  h.overrides.set(origin+'signin-status',init=>{
    seen.push('status');assert.equal(JSON.parse(init.body).attempt_id,attemptId);
    return reply(200,{generation:id(9),attempt_id:attemptId,state:'connected',reason:null});});
  h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/renew',()=>{seen.push('renew');
    return reply(200,{version:1,state:'ready',expires_in:900,token:ROTATED_TOKEN,read_profile:{version:1,profile:'direct-owner-read-v1',host_uid:'host_'+'a'.repeat(32)},
      session_context:{issuer:documentContext.shell_origin,subject:documentContext.user_id,workspace_id:documentContext.workspace_id,operational_origin:documentContext.operational_origin,binding_revision:1,binding_generation:id(6)}});});
  const nodes=()=>descendants(h.ui.get('owner-session')),button=text=>nodes().find(n=>n.tagName==='BUTTON'&&n.textContent===text);
  button('Check setup approval').click();await flush();
  assert.deepEqual(seen,['context']);
  h.time=600000; // 300 seconds left of 900: begin must renew first
  button('Sign in to Claude').click();await flush();
  assert.deepEqual(seen,['context','renew','begin'],'renewal precedes begin, and no second approval check ran');
  const link=nodes().find(n=>n.tagName==='A'&&n.textContent==='Open Claude sign-in');
  assert.equal(link.hidden,false,'the mounted panel shows the waiting link');
  assert.equal(link.attributes.href,authorizeURL);
  const polls=()=>h.timers.filter(t=>t.active&&t.ms===2000);
  assert.equal(polls().length,1,'exactly one serialized poll is scheduled');
  h.timer(2000);await flush();
  assert.deepEqual(seen,['context','renew','begin','status']);
  assert.equal(polls().length,0,'the same-ID connected result stops polling');
  assert.equal(link.hidden,true);
  h.api.dispose();
  } finally { restore(); }
});

test('Copy link sits beside the new-tab sign-in link and shows the link to copy by hand when the clipboard is blocked',async()=>{
  const written=[];let clipboard={writeText:async text=>{written.push(text);}};
  const restores=[stubLocalStorage(),stubGlobal('navigator',{get:()=>({clipboard})})];
  try {
  const h=harness({count:1}),origin=refs[0].endpoint_origin+'/api/owner/connected/setup/';
  await h.api.checkAccount();h.mount();await h.api.act(0,'connect');
  h.overrides.set(origin+'context',()=>reply(200,{version:2,template:'revenue-web-app',repository:'example-org/shop',fleet:'shop',
    source_revision:'b'.repeat(40),generation:id(9),expires_at:1800003600,request:null,launch_reserved:false,provider_signin:true,signin_attempt:null}));
  h.overrides.set(origin+'signin-begin',init=>reply(200,{generation:id(9),attempt_id:JSON.parse(init.body).attempt_id,state:'waiting',reason:null,url:authorizeURL}));
  const nodes=()=>descendants(h.ui.get('owner-session')),button=text=>nodes().find(n=>n.tagName==='BUTTON'&&n.textContent===text);
  const field=()=>nodes().find(n=>n.attributes['aria-label']==='Claude sign-in link');
  assert.equal(button('Copy link').hidden,true,'no link yet, so nothing to copy');
  button('Check setup approval').click();await flush();button('Sign in to Claude').click();await flush();
  const link=nodes().find(n=>n.tagName==='A'&&n.textContent==='Open Claude sign-in');
  assert.equal(link.attributes.target,'_blank','the link still opens a new tab');
  const copy=button('Copy link'),siblings=nodes().find(n=>n.children.includes(link)),note=()=>siblings.children.find(n=>n.tagName==='SPAN');
  assert.equal(note().attributes.role,'status');
  assert.equal(copy.hidden,false);assert.equal(siblings.children.indexOf(copy),siblings.children.indexOf(link)+1,'Copy link is right after the link');
  assert.equal(field().hidden,true);assert.equal(note().textContent,'');
  const copied='Link copied. Paste it into a new tab.',blocked='Copying is blocked here. Copy the link from the box below.';
  copy.click();await flush();
  assert.deepEqual(written,[authorizeURL]);assert.equal(note().textContent,copied);assert.equal(field().hidden,true);
  copy.click();await flush();
  assert.notEqual(note().textContent,copied,'a repeated copy changes the note so it is announced again');assert.equal(note().textContent.trim(),copied);
  copy.click();await flush();assert.equal(note().textContent,copied,'and again on the next repeat');
  const ranges=[];field().setSelectionRange=(start,end)=>ranges.push([start,end]);
  clipboard=undefined;copy.click();await flush();
  assert.equal(note().textContent,blocked,'a missing clipboard falls back to the box');
  assert.equal(field().hidden,false);assert.equal(field().value,authorizeURL);assert.equal(field().readOnly,true);
  assert.deepEqual(ranges,[[0,authorizeURL.length]],'the whole link is selected where select() is ignored');
  copy.click();await flush();assert.equal(note().textContent.trim(),blocked);assert.notEqual(note().textContent,blocked);
  clipboard={writeText:async()=>{throw new DOMException('blocked','NotAllowedError');}};
  copy.click();await flush();assert.equal(note().textContent.trim(),blocked,'a rejected write falls back the same way');
  // A copy that settles after the link changed, or after a later click, changes nothing.
  const pending=[];clipboard={writeText:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))};
  const renewed=authorizeURL+'&state=renewed';
  h.overrides.set(origin+'signin-status',init=>reply(200,{generation:id(9),attempt_id:JSON.parse(init.body).attempt_id,state:'waiting',reason:null,url:renewed}));
  copy.click();await flush();
  assert.equal(visible(button('Check sign-in progress')),false);h.timer(2000);await flush(); // the automatic check
  assert.equal(link.attributes.href,renewed);assert.equal(note().textContent,'','a new link clears the old copy state');assert.equal(field().hidden,true);
  pending[0].reject(new DOMException('blocked','NotAllowedError'));await flush();
  assert.equal(note().textContent,'','a stale rejection is ignored');assert.equal(field().hidden,true);
  copy.click();await flush();copy.click();await flush();
  pending[2].resolve();await flush();assert.equal(note().textContent,copied);
  pending[1].reject(new DOMException('blocked','NotAllowedError'));await flush();
  assert.equal(note().textContent,copied,'an older click settling later is ignored');assert.equal(field().hidden,true);
  h.overrides.set(origin+'signin-cancel',init=>reply(200,{generation:id(9),attempt_id:JSON.parse(init.body).attempt_id,state:'cancelled',reason:null}));
  button('Cancel sign-in').click();await flush();
  assert.equal(copy.hidden,true);assert.equal(note().textContent,'','cancel clears the copy note');assert.equal(field().hidden,true);
  h.api.dispose();
  } finally { for(const restore of restores)restore(); }
});

test('provider-required Preview needs this context\'s attempt confirmed connected; recovery states are not readiness',async()=>{
  const states=[['none',null],['unknown','unknown'],['cancelled','cancelled'],['needs_attention','needs_attention'],['connected','connected']];
  for(const [name,state] of states){
    const h=setupHarness();h.context.provider_signin=true;
    h.context.signin_attempt=state===null?null:{generation:id(9),attempt_id:id(12),state,reason:null};
    h.api.setGoal('Ship it');await h.api.run('context');
    assert.equal(await h.api.run('preview'),state==='connected',name);
    assert.equal(h.calls.some(c=>c[0]==='preview'),state==='connected',name+' reached the host');
    h.api.dispose();
  }
  const retained=setupHarness();retained.saved.set('test:claude',JSON.stringify({generation:id(9),attempt_id:id(12)}));
  retained.context.provider_signin=true;retained.api.setGoal('Ship it');await retained.api.run('context');
  assert.equal(await retained.api.run('preview'),false,'a retained attempt with unknown state is not readiness');retained.api.dispose();
});

const stepsOf=panel=>descendants(panel).filter(n=>n.tagName==='LI'&&n.className==='setup-step');
const filledIn=node=>descendants(node).filter(n=>/\bsetup-primary\b/.test(n.className||'')&&visible(n)).map(n=>n.textContent);
const stepPart=(panel,name,className)=>stepsOf(panel).find(n=>n.dataset.step===name).children.find(n=>n.className===className);
const stepShape=panel=>stepsOf(panel).filter(visible).map(n=>[n.dataset.step,n.dataset.state]);
// workspace.css draws each step's mark from data-state; the mark itself never carries a number or any text.
const unnumbered=panel=>stepsOf(panel).every(n=>n.children.find(c=>c.className==='setup-step-mark').textContent==='');
test('first-team setup is unnumbered steps in order, each with one filled action, collapsing to check lines',async()=>{
  const p=pollHarness(['waiting','connected']),{h,panel}=p;
  const shown=()=>stepsOf(panel).filter(visible);
  const shape=()=>stepShape(panel);
  const filled=()=>Object.fromEntries(shown().map(n=>[n.dataset.step,filledIn(n)]));
  const says=text=>descendants(panel).some(n=>n.textContent===text&&visible(n));
  assert.deepEqual(shape(),[['approve','current'],['signin','upcoming'],['goal','upcoming'],['launch','upcoming']]);assert.ok(unnumbered(panel),'no step is numbered');
  assert.deepEqual(filled(),{approve:['Check setup approval'],signin:[],goal:[],launch:[]});
  assert.ok(descendants(panel).filter(n=>n.tagName==='BUTTON').every(n=>/\bpill\b/.test(n.className)),'every action is a real button, none a text link');
  await h.api.run('context');
  assert.deepEqual(shape().map(r=>r[1]),['done','current','upcoming','upcoming']);assert.ok(unnumbered(panel));
  assert.ok(says('Setup approved'));
  const underDetails=new Set(descendants(panel.children.find(n=>n.tagName==='DETAILS')));
  assert.deepEqual(descendants(panel).filter(n=>visible(n)&&!underDetails.has(n)&&!n.children.length&&/\bteam shop\b/i.test(n.textContent)),[],
    'the team\'s internal name stays under Details');
  assert.deepEqual(filled(),{approve:[],signin:['Sign in to Claude'],goal:[],launch:[]});
  await h.api.run('signin-begin');
  assert.deepEqual(filled().signin,[],'while starting, progress is checked automatically');
  const starting=stepPart(panel,'signin','setup-signin-status'),note=stepPart(panel,'signin','setup-progress');
  assert.equal(starting.textContent,'Starting Claude sign-in…');assert.equal(starting.dataset.tone,'working','a line that waits on the computer turns a ring');
  assert.equal(note.textContent,'This page checks for progress automatically.');assert.equal(note.dataset.tone,undefined,'the passive note does not');
  const actions=()=>descendants(stepsOf(panel)[1]).filter(n=>['A','BUTTON'].includes(n.tagName)&&visible(n)).map(n=>n.textContent);
  assert.deepEqual(actions(),['Sign in to Claude','Cancel sign-in'],'no manual check while the page checks by itself');assert.equal(p.live().length,1,'because a poll is scheduled');
  const held=descendants(stepsOf(panel)[1]).find(n=>n.tagName==='BUTTON'&&n.textContent==='Sign in to Claude'),cancelling=descendants(stepsOf(panel)[1]).find(n=>n.tagName==='BUTTON'&&n.textContent==='Cancel sign-in');
  assert.equal(held.disabled,true);assert.equal(held.className,'pill ghost','held in place, not a second lead');
  const rowOf=node=>descendants(stepsOf(panel)[1]).find(n=>n.className==='setup-actions'&&n.children.includes(node));
  assert.ok(rowOf(held)&&rowOf(cancelling));assert.notEqual(rowOf(held),rowOf(cancelling),'a second click on Sign in to Claude never lands on Cancel sign-in');
  await p.fire();
  assert.deepEqual(filled().signin,['Open Claude sign-in']);assert.equal(p.link.attributes.target,'_blank');
  assert.deepEqual(actions(),['Open Claude sign-in','Copy link','Submit code','Cancel sign-in']);
  const label=descendants(panel).find(n=>n.tagName==='LABEL'&&n.children.includes(p.code));
  assert.equal(label.children[0].textContent,'Code from Claude','the code field has a visible label');
  assert.equal(p.code.attributes['aria-label'],undefined,'the visible label names it');assert.equal(p.code.type,'password');
  assert.ok(visible(p.code));assert.equal(p.code.disabled,false);
  p.code.value='typed';p.code.handlers.input();
  assert.deepEqual(filled().signin,['Submit code'],'a typed code moves the filled action to Submit code');
  p.code.value='';p.code.handlers.input();
  await p.fire();
  assert.deepEqual(shape().map(r=>r[1]),['done','done','current','upcoming']);
  assert.ok(says('Claude connected'));assert.ok(says('Your team works in example-org/shop.'),'the goal step names the repository, not the team');
  assert.deepEqual(filled(),{approve:[],signin:[],goal:['Preview team'],launch:[]});
  const area=descendants(panel).find(n=>n.tagName==='TEXTAREA');
  assert.match(area.attributes.placeholder,/^For example: /);
  assert.equal(descendants(panel).find(n=>n.tagName==='LABEL'&&n.children.includes(area)).children[0].textContent,'What should your team work on first?');
  const recheck=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Check setup approval again');
  assert.equal(recheck.hidden,false);
  assert.ok(descendants(panel.children.find(n=>n.tagName==='DETAILS')).includes(recheck),'re-checking a held approval sits under Details');
  assert.ok(!descendants(panel).some(n=>n.className==='setup-step-done'&&descendants(n).some(c=>c.tagName==='BUTTON')),'"Setup approved" stands alone');
  h.api.setGoal('Ship the signup form');await h.api.run('preview');
  assert.equal(recheck.hidden,true,'re-checking approval would retire a fresh preview, so it is not offered while launchable');
  assert.deepEqual(shape().map(r=>r[1]),['done','done','done','current']);
  assert.ok(says('Goal: Ship the signup form'));
  assert.deepEqual(filled().launch,['Launch team']);
  assert.ok(says('Your team is ready. Launching changes 0 generated files and starts or restarts 1 bot.'));
  await h.api.run('launch');
  assert.deepEqual(filled().launch,['Check launch status'],'after one launch only its status check remains');
  assert.equal(recheck.hidden,false,'once the launch is spent, approval can be checked again');
  assert.ok(shown().every(n=>filledIn(n).length<=1));
  const details=panel.children.find(n=>n.tagName==='DETAILS');
  assert.equal(details.children[0].textContent,'Details');
  assert.ok(descendants(details).some(n=>n.textContent.includes('This approval covers')),'approval scope and identifiers sit under Details');
  h.api.dispose();
  const reopened=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000,setTimeout:()=>0,clearTimeout:()=>{}});
  const ui=dom(),again=new ui.Element();reopened.mountPanel({document:ui.document,element:again});
  const shownAgain=stepsOf(again).filter(visible);
  assert.deepEqual(shownAgain.map(n=>[n.dataset.step,n.dataset.state]),[['approve','current'],['goal','done'],['launch','current']]);
  assert.deepEqual(shownAgain.map(filledIn),[[],[],['Check launch status']],'a reopened launch leads with its status check');
  assert.equal(descendants(again).find(n=>n.tagName==='BUTTON'&&n.textContent==='Check setup approval again').hidden,true,'with approval not yet read, only the step\'s own check is offered');
  reopened.dispose();
});
test('Sign in to Claude says it is starting in one place, and a session renewal meanwhile moves nothing and offers no check',async()=>{
  const gate=deferred();
  const p=pollHarness(['waiting'],{begin:body=>gate.promise.then(()=>({...body,state:'starting',reason:null}))}),{h,panel}=p;
  await h.api.run('context');
  const button=text=>descendants(stepsOf(panel)[1]).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
  const actions=()=>descendants(stepsOf(panel)[1]).filter(n=>['A','BUTTON'].includes(n.tagName)&&visible(n)).map(n=>n.textContent);
  const status=stepPart(panel,'signin','setup-signin-status');
  assert.equal(status.textContent,'Connect your Claude account.');
  const progress=stepPart(panel,'signin','setup-progress');
  button('Sign in to Claude').click();await flush();
  assert.equal(status.textContent,'Starting Claude sign-in…','from the click on, above the buttons');assert.equal(status.dataset.tone,'working');
  assert.equal(progress.textContent,'','not a second time below them');
  h.change({signinRenewal:true});await flush(); // the client renews the session before its begin, which re-renders the panel
  assert.deepEqual(actions(),['Sign in to Claude','Cancel sign-in'],'no check for an attempt the computer has not answered');
  assert.equal(button('Sign in to Claude').className,'pill ghost');assert.equal(button('Sign in to Claude').disabled,true);
  assert.deepEqual(filledIn(stepsOf(panel)[1]),[]);
  assert.equal(status.textContent,'Starting Claude sign-in…','its line stays until the computer answers');assert.equal(progress.textContent,'');
  gate.resolve();await flush();
  assert.deepEqual(actions(),['Sign in to Claude','Cancel sign-in']);assert.equal(status.textContent,'Starting Claude sign-in…','the same line, in the same place');
  assert.equal(progress.textContent,'This page checks for progress automatically.');
  h.api.dispose();
});
test('an ended Claude sign-in says what happened and offers no second attempt, which the host refuses',async()=>{
  for(const [state,reason,said] of [['needs_attention','timeout',/^The sign-in took too long\. /],
      ['cancelled','effect_uncertain',/stopped before it finished; a sign-in Claude had already finished stays saved\./],
      ['unknown','interrupted',/restarted or stopped during sign-in\./],['needs_attention','constructor',/^Sign-in did not finish\. /],
      ['cancelled',null,/^Sign-in was cancelled; /]]){
    const calls=[];
    const h=setupHarness({client:{async setupSignin(action,body){calls.push(action);return {generation:body.generation,attempt_id:body.attempt_id,state,reason};}}});
    h.context.provider_signin=true;h.context.signin_attempt={generation:id(9),attempt_id:id(12),state,reason};
    const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
    await h.api.run('context');
    const status=stepPart(panel,'signin','setup-signin-status'),button=text=>descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
    assert.match(status.textContent,said,String(reason));
    assert.match(status.textContent,/ Trying again needs setup to be recovered on the computer itself\.$/);
    assert.equal(visible(button('Try again')),false,'without the host saying so, no Try again');
    assert.equal(status.dataset.tone,'attention');assert.equal(status.attributes.role,'status');
    assert.equal(visible(button('Sign in to Claude')),false);assert.equal(button('Sign in to Claude').disabled,true);
    assert.equal(visible(button('Cancel sign-in')),false);assert.equal(button('Check sign-in progress').className,'pill ghost');
    assert.deepEqual(filledIn(stepsOf(panel)[1]),[]);
    assert.equal(await h.api.run('signin-begin'),false);assert.deepEqual(calls,[],'no new attempt is sent');
    await h.api.run('signin-status');assert.deepEqual(calls,['status'],'checking only reads the original attempt');
    assert.deepEqual(JSON.parse(h.saved.get('test:claude')),{generation:id(9),attempt_id:id(12)});
    assert.equal(await h.api.run('preview'),false);h.api.dispose();
  }
});
test('a failed sign-in check explains itself and makes Check sign-in progress the one action',async()=>{
  const p=pollHarness([new Error('lost'),'waiting']);
  await p.h.api.run('context');await p.h.api.run('signin-begin');await p.fire();
  const status=stepPart(p.panel,'signin','setup-signin-status');
  assert.match(status.textContent,/^Could not reach this computer to check sign-in\. /);assert.equal(status.dataset.tone,'attention');
  assert.deepEqual(filledIn(stepsOf(p.panel)[1]),['Check sign-in progress']);
  await p.h.api.run('signin-status');
  assert.deepEqual(filledIn(stepsOf(p.panel)[1]),['Open Claude sign-in']);assert.equal(status.dataset.tone,undefined);
  assert.equal(p.calls.filter(c=>c[0]==='begin').length,1);p.h.api.dispose();
});
const refusal=()=>Object.assign(new Error('refused'),{status:403});
const REFUSED='This computer refused this browser’s Claude sign-in. Trying again needs setup to be recovered on the computer itself.';
test('a refused sign-in call says the computer refused it; only a lost answer says the computer could not be reached',async()=>{
  // Begin fails before its POST, yet the attempt is already retained; every check of it is then refused.
  const p=pollHarness([refusal(),refusal()],{begin(){throw new Error('Sign-in was not started. Connect again first.');}});
  await p.h.api.run('context');assert.equal(await p.h.api.run('signin-begin'),false);
  const status=stepPart(p.panel,'signin','setup-signin-status');
  assert.match(status.textContent,/^Could not reach this computer to check sign-in\. /,'an unknown begin outcome proves nothing');
  assert.deepEqual(filledIn(stepsOf(p.panel)[1]),['Check sign-in progress']);assert.equal(p.live().length,0);
  for(let i=0;i<2;i++){
    assert.equal(await p.h.api.run('signin-status'),false);
    assert.equal(status.textContent,REFUSED);assert.equal(status.dataset.tone,'attention');
    assert.deepEqual(filledIn(stepsOf(p.panel)[1]),[],'a refusal does not invite the same check again');
    assert.equal(p.live().length,0,'no automatic check after a refusal');
  }
  assert.deepEqual(p.calls.map(c=>c[0]),['begin','status','status'],'never a second begin');
  assert.deepEqual(JSON.parse(p.h.saved.get('test:claude')),{generation:p.calls[0][1].generation,attempt_id:p.calls[0][1].attempt_id});
  p.h.api.dispose();
});
const retryEnded={generation:id(9),attempt_id:id(12),state:'needs_attention',reason:'timeout'};
test('Try again appears for an ending the host marks retryable and starts one fresh attempt only on its click',async()=>{
  const timers=[],calls=[];let answer=null;
  const h=setupHarness({setupOptions:{setTimeout(fn,ms){const t={fn,ms,live:true};timers.push(t);return t;},clearTimeout(t){if(t)t.live=false;}},
    client:{async setupSignin(action,body){calls.push([action,body]);return answer(action,body);}}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const status=stepPart(panel,'signin','setup-signin-status'),button=text=>descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
  const retry=button('Try again'),live=()=>timers.filter(t=>t.live);
  assert.equal(status.textContent,'The sign-in took too long. Choose Try again for a new sign-in link.');
  assert.equal(status.dataset.tone,'attention');
  assert.equal(visible(retry),true);assert.equal(retry.disabled,false);
  assert.deepEqual(filledIn(stepsOf(panel)[1]),['Try again'],'Try again is the one filled action');
  assert.equal(visible(button('Sign in to Claude')),false);assert.equal(visible(button('Cancel sign-in')),false);
  assert.equal(visible(button('Check sign-in progress')),false,'the host already said this attempt may be replaced: nothing to check');
  // Nothing starts on its own: no timer, no begin from a resume, a re-check or the first-attempt door.
  h.api.resumeHandoff();await h.api.run('context');
  assert.equal(await h.api.run('signin-begin'),false);
  assert.equal(live().length,0);assert.deepEqual(calls,[]);
  const gate=deferred();answer=()=>gate.promise;
  retry.click();await flush();
  assert.equal(calls.length,1);assert.equal(calls[0][0],'begin','Try again uses the host begin door');
  const {replaces,...fresh}=JSON.parse(h.saved.get('test:claude'));
  assert.equal(replaces,retryEnded.attempt_id,'the ID it replaces is kept beside it until the host answers');
  assert.deepEqual(calls[0][1],fresh,'the new attempt is retained before its one begin');
  assert.notEqual(fresh.attempt_id,retryEnded.attempt_id);assert.equal(fresh.generation,retryEnded.generation);
  assert.equal(status.textContent,'Starting Claude sign-in…','from the click on, in the line above the buttons');assert.equal(status.dataset.tone,'working');
  assert.equal(stepPart(panel,'signin','setup-progress').textContent,'','not a second time below them');
  assert.equal(retry.disabled,true,'held while it starts');retry.click();await flush();
  assert.equal(calls.length,1,'a second click while it starts sends nothing');
  gate.resolve({...fresh,state:'starting',reason:null});await flush();
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),fresh,'an answer for the new ID settles it');
  assert.equal(status.textContent,'Starting Claude sign-in…');assert.equal(visible(retry),false);
  const held=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Sign in to Claude');
  assert.ok(visible(held));assert.equal(held.className,'pill ghost','the new attempt holds Sign in to Claude in place, too');
  // The new attempt is followed like a first one: one poll, for its own ID, to its new link.
  answer=(_action,body)=>({...body,state:'waiting',reason:null,url:authorizeURL});
  assert.equal(live().length,1);const t=live()[0];t.live=false;t.fn();await flush();
  assert.deepEqual(calls[1],['status',fresh]);
  assert.deepEqual(filledIn(stepsOf(panel)[1]),['Open Claude sign-in']);
  assert.equal(await h.api.run('signin-retry'),false,'an open attempt offers no Try again');assert.equal(calls.length,2);
  h.api.dispose();
});
test('Try again follows only the host\'s retryable answer, and a later answer or a failed check takes it away',async()=>{
  const button=(panel,text)=>descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
  // Shapes the host never sends are refused, so none of them can offer Try again.
  for(const bad of [{state:'needs_attention',reason:'timeout',retryable:false},{state:'connected',reason:null,retryable:true},
      {state:'unknown',reason:'interrupted',retryable:true},{state:'waiting',reason:null,url:authorizeURL,retryable:true}]){
    const h=setupHarness({client:{async setupSignin(_action,body){return {generation:body.generation,attempt_id:body.attempt_id,...bad};}}});
    h.context.provider_signin=true;h.context.signin_attempt=retryEnded;
    const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
    await h.api.run('context');
    assert.equal(await h.api.run('signin-status'),false,JSON.stringify(bad));
    assert.match(stepPart(panel,'signin','setup-signin-status').textContent,/^Could not reach this computer to check sign-in\. /);
    assert.equal(visible(button(panel,'Try again')),false);assert.equal(await h.api.run('signin-retry'),false);
    h.api.dispose();
  }
  const replies=[{retryable:true},{},{retryable:true},new Error('lost'),{retryable:true}],calls=[];
  const h=setupHarness({client:{async setupSignin(action,body){
    calls.push(action);const next=replies.shift();if(next instanceof Error)throw next;
    return {generation:body.generation,attempt_id:body.attempt_id,state:'needs_attention',reason:'timeout',...next};}}});
  h.context.provider_signin=true;h.context.signin_attempt=retryEnded;
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const retry=button(panel,'Try again'),status=stepPart(panel,'signin','setup-signin-status'),offered=[];
  assert.equal(visible(retry),false);
  for(let i=0;i<5;i++){await h.api.run('signin-status');offered.push([visible(retry),status.textContent.endsWith(' Choose Try again for a new sign-in link.')]);}
  assert.deepEqual(offered,[[true,true],[false,false],[true,true],[false,false],[true,true]]);
  assert.deepEqual(calls,['status','status','status','status','status'],'checking never begins');
  h.api.dispose();
});
const notStarted=()=>Object.assign(new Error('Sign-in was not started. Connect again first.'),{code:'signin_not_started'});
for(const [name,failure] of [['refused',refusal],['never sent',notStarted]])
test(`a Try again that was ${name} keeps following the ended attempt, here and after a reload`,async()=>{
  const calls=[];
  const h=setupHarness({client:{async setupSignin(action,body){calls.push([action,body]);throw failure();}}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const status=stepPart(panel,'signin','setup-signin-status'),retry=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again');
  assert.equal(await h.api.run('signin-retry'),false);
  assert.equal(calls.length,1);const [[action,sent]]=calls;
  assert.equal(action,'begin');assert.notEqual(sent.attempt_id,retryEnded.attempt_id,'the new attempt was the one asked for');
  const original={generation:retryEnded.generation,attempt_id:retryEnded.attempt_id};
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),original,'nothing was admitted, so the ended attempt is kept');
  if(name==='refused'){
    assert.equal(status.textContent,'This computer refused the new Claude sign-in. Choose Check sign-in progress to look again.');
    assert.equal(visible(descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Check sign-in progress')),true,'the check it names is there');
    assert.equal(visible(retry),false,'only the host\'s next answer offers it again');
    assert.equal(await h.api.run('signin-retry'),false);assert.equal(calls.length,1);
  } else {
    assert.equal(status.textContent,'Sign-in was not started. Connect again first.');
    assert.equal(visible(retry),true,'the host\'s last answer still stands');
  }
  assert.equal(await h.api.run('signin-begin'),false,'never a first begin over a kept attempt');
  // The host still names the ended attempt: a context check (as on a reload) shows it and offers Try again.
  assert.equal(await h.api.run('context'),true);
  assert.equal(status.textContent,'The sign-in took too long. Choose Try again for a new sign-in link.');
  assert.equal(visible(retry),true);assert.equal(calls.length,1,'nothing more was sent');
  // A reload: a new panel over the same storage reads the kept attempt and accepts the host's answer.
  const reloaded=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.equal(await reloaded.run('context'),true,'a later page load is not stranded');
  assert.equal(reloaded.snapshot().context.signin_attempt.attempt_id,retryEnded.attempt_id);
  h.api.dispose();reloaded.dispose();
});
for(const admitted of [false,true])
test(`a Try again whose answer was lost follows whichever attempt the host names next (${admitted?'the new one':'the ended one'})`,async()=>{
  const calls=[];
  const h=setupHarness({client:{async setupSignin(action,body){calls.push([action,body]);throw new Error('lost');}}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const retry=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again');
  assert.equal(await h.api.run('signin-retry'),false);
  const fresh={generation:retryEnded.generation,attempt_id:calls[0][1].attempt_id},original={generation:retryEnded.generation,attempt_id:retryEnded.attempt_id};
  assert.notEqual(fresh.attempt_id,original.attempt_id);
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),{...fresh,replaces:original.attempt_id},'the host may have started it');
  assert.equal(visible(retry),false);
  assert.equal(await h.api.run('signin-retry'),false);assert.equal(calls.length,1,'no second begin of any kind');
  // A later page load, then its context: the host names its one current attempt.
  const reloaded=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  const page=dom(),root=new page.Element();reloaded.mountPanel({document:page.document,element:root});
  h.context.signin_attempt=admitted ? {...fresh,state:'starting',reason:null} : {...retryEnded,retryable:true};
  assert.equal(await reloaded.run('context'),true,'never stranded on the unanswered ID');
  const line=()=>stepPart(root,'signin','setup-signin-status').textContent,again=descendants(root).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again');
  if(admitted){
    assert.deepEqual(JSON.parse(h.saved.get('test:claude')),fresh,'an answer naming the new ID settles it');
    assert.equal(line(),'Starting Claude sign-in…');assert.equal(visible(again),false);
  } else {
    // Not admitted yet: a begin may still be committing, so the new ID stays acceptable beside the ended one.
    assert.deepEqual(JSON.parse(h.saved.get('test:claude')),{...original,replaced_by:fresh.attempt_id});
    assert.equal(line(),'The sign-in took too long. Choose Try again for a new sign-in link.');assert.equal(visible(again),true);
    // The late begin commits and retires the ended attempt: the next context names the new ID, also after a reload.
    h.context.signin_attempt={...fresh,state:'waiting',reason:null,url:authorizeURL};
    const later=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
    const laterPage=dom(),laterRoot=new laterPage.Element();later.mountPanel({document:laterPage.document,element:laterRoot});
    assert.equal(await later.run('context'),true,'the new ID is followed after a reload');
    assert.deepEqual(JSON.parse(h.saved.get('test:claude')),fresh);
    assert.deepEqual(filledIn(stepsOf(laterRoot)[1]),['Open Claude sign-in']);
    assert.equal(await reloaded.run('context'),true,'and on the page that had followed the ended one');
    later.dispose();
  }
  // A third ID is never accepted, whichever one this browser holds.
  h.context.signin_attempt={...retryEnded,attempt_id:id(13),retryable:true};
  assert.equal(await reloaded.run('context'),false,'an ID this browser never started is refused');
  assert.equal(calls.length,1,'nothing was sent on its own');
  h.api.dispose();reloaded.dispose();
});
test('a status refusal during an unsettled Try again reads the context once and follows the attempt it names',async()=>{
  const calls=[];let lost=true;
  const h=setupHarness({client:{async setupSignin(action,body){
    calls.push([action,body]);
    if(action==='begin'&&lost)throw new Error('lost');
    throw Object.assign(new Error('refused'),{status:403}); // the new ID was never admitted
  }}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const status=stepPart(panel,'signin','setup-signin-status'),retry=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again');
  assert.equal(await h.api.run('signin-retry'),false);
  const contexts=()=>h.calls.filter(c=>c[0]==='context').length,before=contexts();
  assert.equal(await h.api.run('signin-status'),false);await flush();
  assert.deepEqual(calls.map(c=>c[0]),['begin','status'],'one check, then no further sign-in call');
  assert.equal(contexts(),before+1,'one context read in its place');
  assert.equal(status.textContent,'The sign-in took too long. Choose Try again for a new sign-in link.');
  assert.equal(visible(retry),true,'never "recover setup on the computer" for an unsettled Try again');
  h.api.dispose();
});
test('stored sign-in records with a malformed or doubled other ID are refused',async()=>{
  const a=id(12),b=id(13);
  for(const record of [{generation:id(9),attempt_id:a,replaces:a},{generation:id(9),attempt_id:a,replaced_by:a},
      {generation:id(9),attempt_id:a,replaces:'not-a-uuid'},{generation:id(9),attempt_id:a,replaced_by:7},
      {generation:id(9),attempt_id:a,replaces:b,replaced_by:b}]){
    const saved=new Map([['test:claude',JSON.stringify(record)]]);
    const storage={getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,value)};
    const h=setupHarness();
    const panel=createGuidedSetup({client:h.client,storage,storageKey:'test',now:()=>1800000000});
    const ui=dom(),root=new ui.Element();panel.mountPanel({document:ui.document,element:root});
    h.context.provider_signin=true;await panel.run('context');
    assert.match(stepPart(root,'signin','setup-signin-status').textContent,/cannot keep track of a Claude sign-in/,JSON.stringify(record));
    panel.dispose();h.api.dispose();
  }
});
test('an ended attempt the host will not replace because the approval is ending says so',async()=>{
  const h=setupHarness({client:{async setupSignin(){throw new Error('unused');}}});
  h.context.provider_signin=true;h.context.expires_at=1800000000+49;h.context.signin_attempt=retryEnded; // no retryable
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  assert.equal(stepPart(panel,'signin','setup-signin-status').textContent,
    'The sign-in took too long. Setup approval ends too soon for a new sign-in. Trying again needs setup to be recovered on the computer itself.');
  assert.equal(visible(descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again')),false);
  h.context.expires_at=1800000000+50;await h.api.run('context');
  assert.equal(stepPart(panel,'signin','setup-signin-status').textContent,
    'The sign-in took too long. Trying again needs setup to be recovered on the computer itself.','enough approval: the host\'s own reason applies');
  h.api.dispose();
});
test('a Try again that never left this browser is undone even when the connection changed meanwhile',async()=>{
  const calls=[];let h;
  h=setupHarness({client:{async setupSignin(action,body){
    calls.push([action,body]);
    h.change({state:'unavailable',epoch:2}); // a failed pre-begin renewal suspends the connection first
    throw Object.assign(new Error('Sign-in was not started. The session could not be renewed first.'),{code:'signin_not_started'});
  }}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  assert.equal(await h.api.run('signin-retry'),false);
  const original={generation:retryEnded.generation,attempt_id:retryEnded.attempt_id};
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),original,'restored although the epoch moved');
  h.change({state:'setup_only',epoch:3}); // reconnected
  assert.equal(await h.api.run('context'),true);
  assert.equal(stepPart(panel,'signin','setup-signin-status').textContent,'The sign-in took too long. Choose Try again for a new sign-in link.');
  assert.equal(visible(descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again')),true);
  assert.equal(calls.length,1);
  h.api.dispose();
});
test('a Try again whose storage write fails sends nothing, stays on the ended attempt, and the next Try again works',async()=>{
  const calls=[];
  const h=setupHarness({client:{async setupSignin(action,body){calls.push([action,body]);throw new Error('lost');}}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const original={generation:retryEnded.generation,attempt_id:retryEnded.attempt_id};
  const write=h.storage.setItem;let fail=1;
  h.storage.setItem=(key,value)=>{if(key==='test:claude'&&fail-->0)throw new Error('quota');return write(key,value);};
  const status=stepPart(panel,'signin','setup-signin-status'),retry=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again');
  assert.equal(await h.api.run('signin-retry'),false);
  assert.deepEqual(calls,[],'nothing was sent');
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),original);
  assert.equal(status.textContent,'This browser could not save the new sign-in, so nothing was sent. Choose Try again.');
  assert.equal(visible(retry),true);
  // The next answer naming the ended attempt keeps a clean record (never {X, replaced_by: X}).
  assert.equal(await h.api.run('context'),true);
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),original);
  const reloaded=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  const page=dom(),root=new page.Element();reloaded.mountPanel({document:page.document,element:root});
  assert.equal(await reloaded.run('context'),true);
  assert.equal(stepPart(root,'signin','setup-signin-status').textContent,'The sign-in took too long. Choose Try again for a new sign-in link.');
  reloaded.dispose();
  // The next Try again works: one begin with a new ID, stored beside the ended one.
  assert.equal(await h.api.run('signin-retry'),false);
  assert.equal(calls.length,1);assert.equal(calls[0][0],'begin');assert.notEqual(calls[0][1].attempt_id,original.attempt_id);
  assert.deepEqual(JSON.parse(h.saved.get('test:claude')),{generation:original.generation,attempt_id:calls[0][1].attempt_id,replaces:original.attempt_id});
  h.api.dispose();
});
test('a cancel before any code is offered Try again; only the host\'s exact retryable endings are accepted',async()=>{
  const calls=[];
  const h=setupHarness({client:{async setupSignin(action,body){calls.push([action,body]);return {...body,state:'starting',reason:null};}}});
  h.context.provider_signin=true;h.context.signin_attempt={...retryEnded,state:'cancelled',reason:'effect_uncertain',retryable:true};
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const status=stepPart(panel,'signin','setup-signin-status'),retry=descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Try again');
  assert.equal(status.textContent,'The sign-in stopped before a code was sent. Choose Try again for a new sign-in link.');
  assert.equal(visible(retry),true);assert.deepEqual(calls,[]);
  retry.click();await flush();
  assert.equal(calls.length,1);assert.equal(calls[0][0],'begin');assert.notEqual(calls[0][1].attempt_id,retryEnded.attempt_id);
  h.api.dispose();
  for(const ending of [{state:'cancelled',reason:'provider_failed'},{state:'cancelled',reason:null},{state:'needs_attention',reason:'effect_uncertain'},
      {state:'needs_attention',reason:'provider_failed'}]){
    const other=setupHarness({client:{async setupSignin(){throw new Error('unused');}}});
    other.context.provider_signin=true;other.context.signin_attempt={...retryEnded,...ending,retryable:true};
    assert.equal(await other.api.run('context'),false,JSON.stringify(ending));
    other.context.signin_attempt={...retryEnded,...ending};
    assert.equal(await other.api.run('context'),true,'the same ending without retryable is ordinary');
    assert.equal(await other.api.run('signin-retry'),false);
    other.api.dispose();
  }
});
test('while this page is hidden, a live sign-in keeps a slow read-only check so its conversation stays open',async()=>{
  const timers=[];let shown=true,reply;
  const h=setupHarness({setupOptions:{visible:()=>shown,setTimeout(fn,ms){const t={fn,ms,live:true};timers.push(t);return t;},clearTimeout(t){if(t)t.live=false;}},
    client:{async setupSignin(action,body){return reply(action,body);}}});
  h.context.provider_signin=true;
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const live=()=>timers.filter(t=>t.live),actions=[];
  reply=(action,body)=>{actions.push(action);return {...body,state:'waiting',reason:null,url:authorizeURL};};
  await h.api.run('signin-begin');
  assert.deepEqual(live().map(t=>t.ms),[2000]);
  shown=false; // Claude's tab is in front
  let t=live()[0];t.live=false;t.fn();await flush();
  assert.deepEqual(live().map(t=>t.ms),[25000],'well inside the 45-second idle bound');
  t=live()[0];t.live=false;t.fn();await flush();
  assert.deepEqual(live().map(t=>t.ms),[25000]);
  shown=true;h.api.resumeHandoff();
  assert.deepEqual(live().map(t=>t.ms),[2000],'back to the ordinary pace when shown');
  assert.deepEqual(actions,['begin','status','status'],'only status reads; never a begin, code or cancel');
  // An ended attempt schedules nothing, hidden or not.
  shown=false;reply=(action,body)=>{actions.push(action);return {...body,state:'needs_attention',reason:'timeout'};};
  t=live()[0];t.live=false;t.fn();await flush();
  assert.equal(live().length,0);
  h.api.dispose();
});
test('the sign-in link shows the host\'s own deadline in local time, as text, with no timer of its own',async()=>{
  const timers=[];let reply;
  const h=setupHarness({setupOptions:{setTimeout(fn,ms){const t={fn,ms,live:true};timers.push(t);return t;},clearTimeout(t){if(t)t.live=false;}},
    client:{async setupSignin(_action,body){return reply(body);}}});
  h.context.provider_signin=true;
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');
  const status=stepPart(panel,'signin','setup-signin-status'),live=()=>timers.filter(t=>t.live);
  const local=seconds=>new Date(seconds*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
  const ASK='Open Claude sign-in, then paste the code here.',by=time=>`Open Claude sign-in, then paste the code here by ${time}.`;
  const minute=1800000000-1800000000%60,deadline=minute+59; // 59 seconds into its minute
  reply=body=>({...body,state:'waiting',reason:null,url:authorizeURL,expires_at:deadline});
  await h.api.run('signin-begin');
  assert.equal(status.textContent,by(local(minute)),'the minute it falls in, never rounded up');
  assert.match(local(minute),/\d:\d\d/);assert.notEqual(status.dataset.tone,'attention');
  assert.deepEqual(filledIn(stepsOf(panel)[1]),['Open Claude sign-in']);
  assert.equal(live().length,1,'only the ordinary status check is scheduled');
  // Each answer replaces it: a link without expires_at shows no time, a later one shows the host's.
  reply=body=>({...body,state:'waiting',reason:null,url:authorizeURL});
  let t=live()[0];t.live=false;t.fn();await flush();
  assert.equal(status.textContent,ASK);
  reply=body=>({...body,state:'waiting',reason:null,url:authorizeURL,expires_at:minute+120});
  t=live()[0];t.live=false;t.fn();await flush();
  assert.equal(status.textContent,by(local(minute+120)));
  reply=body=>({...body,state:'needs_attention',reason:'timeout'});
  t=live()[0];t.live=false;t.fn();await flush();
  assert.equal(status.textContent.includes(local(minute+120)),false,'an ended attempt shows no deadline');
  h.api.dispose();
  // expires_at is refused away from a link and in any shape but whole positive seconds.
  for(const bad of [{state:'starting',reason:null,expires_at:deadline},{state:'waiting',reason:null,url:authorizeURL,expires_at:deadline+0.5},
      {state:'waiting',reason:null,url:authorizeURL,expires_at:String(deadline)},{state:'waiting',reason:null,url:authorizeURL,expires_at:0},
      {state:'waiting',reason:null,url:authorizeURL,expires_at:null}]){
    const other=setupHarness({client:{async setupSignin(_action,body){return {...body,...bad};}}});
    other.context.provider_signin=true;
    const page=dom(),root=new page.Element();other.api.mountPanel({document:page.document,element:root});
    await other.api.run('context');await other.api.run('signin-begin');
    const line=stepPart(root,'signin','setup-signin-status').textContent;
    assert.equal(line.startsWith('Open Claude sign-in, then paste'),false,JSON.stringify(bad));
    other.api.dispose();
  }
});
test('a refused code keeps the waiting attempt, its link and its automatic checks',async()=>{
  const p=pollHarness(['waiting',refusal(),'waiting','waiting']);
  await p.h.api.run('context');await p.h.api.run('signin-begin');await p.fire();
  const status=stepPart(p.panel,'signin','setup-signin-status');
  p.code.value='wrong#code';p.code.handlers.input();
  assert.equal(await p.h.api.run('signin-code'),false);assert.equal(p.code.value,'','the refused code is not kept');
  assert.match(status.textContent,/^The computer did not accept that code\. Copy the whole code Claude shows you and submit it again/);
  assert.equal(status.dataset.tone,'attention');
  assert.equal(visible(p.link),true,'the link stays');assert.equal(p.code.disabled,false,'a corrected code can still be sent');
  assert.equal(p.live().length,1,'automatic checks continue');
  await p.fire();assert.match(status.textContent,/^The computer did not accept that code\. /,'a later check keeps the explanation');
  p.code.value='good#code';p.code.handlers.input();
  assert.equal(await p.h.api.run('signin-code'),true);assert.equal(status.textContent,'Code sent, finishing sign-in…');
  p.h.api.dispose();
});
test('a taken code holds Submit code and the field until the attempt leaves waiting',async()=>{
  const p=pollHarness(['waiting','waiting','waiting','connected']);
  await p.h.api.run('context');await p.h.api.run('signin-begin');await p.fire();
  const status=stepPart(p.panel,'signin','setup-signin-status'),submit=descendants(p.panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Submit code');
  p.code.value='good#code';p.code.handlers.input();
  assert.equal(await p.h.api.run('signin-code'),true,'the host answers waiting until its worker uses the code');
  assert.equal(status.textContent,'Code sent, finishing sign-in…');assert.equal(status.dataset.tone,'working');
  assert.equal(visible(p.link),false,'sign-in is not offered again');
  assert.equal(p.code.disabled,true);assert.equal(submit.disabled,true);
  assert.deepEqual(filledIn(stepsOf(p.panel)[1]),[],'nothing to press while sign-in finishes');
  assert.equal(stepPart(p.panel,'signin','setup-progress').textContent,'This page checks for progress automatically.');
  p.code.value='second';assert.equal(await p.h.api.run('signin-code'),false);
  await p.fire();assert.equal(status.textContent,'Code sent, finishing sign-in…','still held while the host says waiting');
  assert.deepEqual(p.calls.map(c=>c[0]),['begin','status','code','status'],'no second code is sent');
  await p.fire();assert.equal(status.textContent,'Claude connected');
  assert.equal(p.code.value,'','leaving waiting empties the field');p.h.api.dispose();
});
test('the code field is emptied whenever the attempt is not waiting for one',async()=>{
  const p=pollHarness(['waiting','needs_attention']);
  await p.h.api.run('context');await p.h.api.run('signin-begin');await p.fire();
  p.code.value='half-typed';p.code.handlers.input();
  await p.fire();assert.equal(p.code.value,'');assert.equal(p.code.disabled,true);p.h.api.dispose();
});
test('buttons held by an automatic check look held once it takes a moment',async()=>{
  const css=await read('workspace.css'),rule=css.match(/\.setup-panel\[data-polling\] button:disabled\s*\{([^}]*)\}/);
  assert.ok(rule);assert.doesNotMatch(rule[1],/(^|;)\s*opacity\s*:/,'the shared disabled look applies');
  assert.match(rule[1],/transition:\s*opacity 0s \.4s/,'after a short delay, so quick checks do not flash');
  assert.match(css,/button:disabled\s*\{[^}]*opacity:\.5/);
});
test('a waiting line turns a ring beside its text, and stands still for reduced motion',async()=>{
  const css=await read('workspace.css');
  assert.match(css,/:is\(\.setup-panel \[data-tone="working"\], \.setup-only-status\[data-tone="working"\]\)::before\s*\{[^}]*animation:setup-spin /);
  assert.match(css,/\.setup-step\[data-state="done"\] \.setup-step-mark::before\s*\{\s*content:"✓";\s*\}/,'a done step is checked off');
  assert.match(css,/@keyframes setup-spin\s*\{\s*to\s*\{\s*transform:rotate\(360deg\)/);
  assert.match(css,/@media\(prefers-reduced-motion:reduce\)\s*\{\s*\*, ::before, ::after\s*\{[^}]*animation:none!important/,
    'the global reduced-motion rule reaches the ring, which then stands still');
});
test('every wait shows progress in its step; the automatic sign-in check neither flashes nor takes the code field',async()=>{
  const gate=deferred(),h=setupHarness({client:{setupContext:()=>gate.promise}});
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  const reading=h.api.run('context'),progress=stepPart(panel,'approve','setup-progress');
  assert.equal(progress.textContent,'Checking setup approval…');assert.equal(progress.attributes.role,'status');
  assert.equal(progress.dataset.tone,'working','an operation\'s wait turns a ring beside its text');
  gate.resolve(h.context);await reading;assert.equal(progress.textContent,'');assert.equal(progress.dataset.tone,undefined);h.api.dispose();
  const polls=[deferred(),deferred(),deferred()],p=pollHarness(polls.map(d=>d.promise));
  await p.h.api.run('context');await p.h.api.run('signin-begin');await p.fire();
  assert.equal(p.panel.dataset.polling,'true','an automatic check is marked; workspace.css decides how held buttons look');
  assert.equal(stepPart(p.panel,'signin','setup-progress').textContent,'This page checks for progress automatically.');
  polls[0].resolve('waiting');await flush();assert.equal(p.panel.dataset.polling,undefined);
  const checking=p.h.api.run('signin-status');
  assert.equal(stepPart(p.panel,'signin','setup-progress').textContent,'Checking sign-in…','an explicit check shows its progress');
  assert.equal(stepPart(p.panel,'signin','setup-progress').dataset.tone,'working');
  assert.equal(p.panel.dataset.polling,undefined);
  polls[1].resolve('waiting');await checking;
  await p.fire();assert.equal(p.panel.dataset.polling,'true');
  assert.equal(p.code.disabled,false,'typing a code keeps focus while a check is in flight');
  assert.equal(descendants(p.panel).find(n=>n.tagName==='BUTTON'&&n.textContent==='Submit code').disabled,true,'submission still waits');
  polls[2].resolve('waiting');await flush();p.h.api.dispose();
});
test('without a main area the first-team panel stays in the sidebar card and opens it',async()=>{
  const h=harness({count:1,rail:false});h.mount();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  await h.api.checkAccount();await h.api.act(0,'connect');
  const card=h.ui.get('owner-session').children[1],help=card.children.at(-1),panel=descendants(help).find(n=>n.className==='setup-focus-panel');
  assert.equal(panel.parentNode,help);assert.equal(help.open,true);assert.equal(card.children[1].children[1].hidden,true,'no Go to setup');
  assert.match(card.children[2].textContent,/Check setup approval below/);
  assert.equal(panel.children.find(n=>n.tagName==='H2').textContent,'First-team setup');h.api.dispose();
});
test('an account change removes the main-area setup panel with the rest of the private view',async()=>{
  const h=harness({count:1});h.mount();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  await h.api.checkAccount();await h.api.act(0,'connect');
  const rail=h.ui.get('rail-channel'),host=rail.children[0];
  assert.equal(rail.dataset.setupFocus,'only');assert.equal(rail.children.length,1);assert.equal(host.children.length,1);
  h.metadataStatus=403;await h.api.checkAccount();
  assert.equal(h.api.snapshot().phase,'denied');assert.equal(host.children.length,0,'no setup panel is left behind');
  assert.equal(host.hidden,true);assert.equal(rail.children.length,1,'only the emptied host remains');
  assert.equal(rail.dataset.setupFocus,undefined);h.api.dispose();
});

// Browser carrier exercises the real Fetch facade and MessagePort boundary.
const bridgeModule = async () => import(uri(await read('browser-owner-transport.js')));
function browserDocument() {
  return {...documentContext,shell_origin:'https://claudfather.ai',operational_origin:'https://plane.claudfather.ai',
    references:[{...refs[0],endpoint_origin:'https://first.example.ts.net'}],expires_at:Math.floor(Date.now()/1000)+90};
}
async function bridgeHarness(t, options={}) {
  const {document:selected,...rest}=options;options=rest;
  const {createBrowserOwnerTransport}=await bridgeModule(), doc=selected??browserDocument(), listeners=new Map(),sent=[],native=[];
  const parent={postMessage(data,origin){sent.push({data,origin});}};
  const window={parent,location:{origin:doc.operational_origin},addEventListener(k,v){listeners.set(k,v);},removeEventListener(k){listeners.delete(k);}};
  const bridge=createBrowserOwnerTransport({documentContext:doc,window,
    nativeFetch:async(...args)=>{native.push(args);return reply(200,{account:true});},...options});
  t.after(()=>bridge.dispose());
  const channel=new MessageChannel(), frames=[]; let seq=0;
  channel.port1.onmessage=event=>frames.push(event.data);channel.port1.start();
  t.after(()=>{channel.port1.close();channel.port2.close();});
  const hello=sent[0].data;
  const offer={type:'claudfather.owner-transport.offer',version:1,channelId:hello.channelId,document:hello.document,
    sessionId:'a'.repeat(64),reference:doc.references[0],targetHandle:'target-'+'b'.repeat(32)+'-1'};
  function admit(patch={},event={}) {listeners.get('message')?.({source:parent,origin:doc.shell_origin,data:{...offer,...patch},ports:[channel.port2],...event});}
  function send(type,body={}) {channel.port1.postMessage({version:1,channelId:offer.channelId,sessionId:offer.sessionId,seq:++seq,type,...body});}
  const host=(path,body,token=true)=>bridge.fetch(doc.references[0].endpoint_origin+path,{method:body===undefined?'GET':'POST',
    credentials:'omit',cache:'no-store',redirect:'error',headers:{...(token?{Authorization:'Bearer '+'A'.repeat(43)}:{}),
      ...(body===undefined?{}:{'Content-Type':'application/json','X-Claudlobby-Owner':'1'})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  return {bridge,doc,listeners,sent,native,frames,admit,send,host,offer,parent};
}
test('browser bridge binds current parent document and separates shell Fetch from finite host requests',async t=>{
  const h=await bridgeHarness(t);
  assert.equal(h.sent[0].origin,h.doc.shell_origin);assert.match(h.offer.channelId,/^[0-9a-f]{64}$/);
  h.admit({sessionId:'c'.repeat(64)},{origin:'https://wrong.example.test'});
  h.admit({document:{...h.offer.document,context_generation:id(9)}});
  h.admit({targetHandle:'target-'+'b'.repeat(32)+'-1'+'0'.repeat(65536)});
  await flush();assert.equal(h.frames.length,0);
  h.admit();await h.bridge.ready;await flush();assert.equal(h.frames[0].type,'ready');
  const account=await h.bridge.fetch(h.doc.shell_origin+'/api/plane/context?workspace_id='+h.doc.workspace_id+'&include=computer_names',{credentials:'include'});
  assert.deepEqual(await account.json(),{account:true});assert.equal(h.native.length,1);
  const pending=h.host('/api/owner/connected/setup/preview',{generation:id(8),goal:'One useful team'});
  await flush();const request=h.frames.find(f=>f.type==='request');
  assert.equal(request.credit,4);assert.deepEqual(request.request,{targetHandle:h.offer.targetHandle,operation:'setup-preview',
    input:{generation:id(8),goal:'One useful team'},token:'A'.repeat(43)});
  assert.equal('url' in request.request,false);assert.equal('headers' in request.request,false);
  h.send('headers',{id:request.id,status:200,headers:{'content-type':'application/json'}});
  h.send('chunk',{id:request.id,data:Buffer.from('{"ok":true}').toString('base64')});h.send('end',{id:request.id});
  assert.deepEqual(await (await pending).json(),{ok:true});assert.equal(h.native.length,1);
});
test('browser bridge rejects arbitrary URL/header/action/input and never falls back after retirement',async t=>{
  const h=await bridgeHarness(t);h.admit();await h.bridge.ready;
  for(const [path,body] of [['/api/owner/connected/actions/context',{room:'team'}],['/api/owner/connected/setup/preview',{generation:id(8),goal:'g',env:{}}],
    ['/api/owner/connected/read/summary?extra=1',undefined],['/api/owner/connected/read/stream?cursor=01',undefined],['/api/owner/connected/read/tasks/wi_no',undefined]])
    await assert.rejects(h.host(path,body));
  await assert.rejects(h.bridge.fetch('https://other.example.ts.net/api/owner/connected/status'));
  await assert.rejects(h.bridge.fetch(h.doc.references[0].endpoint_origin+'/api/owner/connected/status',{headers:{Authorization:'Bearer '+'A'.repeat(43),Cookie:'bad'}}));
  h.bridge.dispose();await assert.rejects(h.host('/api/owner/connected/status'));
  assert.equal(h.native.length,0);
});
test('browser bridge cancellation makes late replies inert and preserves other request identity',async t=>{
  const h=await bridgeHarness(t);h.admit();await h.bridge.ready;
  const abort=new AbortController();const cancelled=h.bridge.fetch(h.doc.references[0].endpoint_origin+'/api/owner/connected/read/summary',
    {signal:abort.signal,credentials:'omit',redirect:'error',headers:{Authorization:'Bearer '+'A'.repeat(43)}});
  await flush();const first=h.frames.find(f=>f.type==='request');abort.abort();await assert.rejects(cancelled);await flush();
  assert.ok(h.frames.some(f=>f.type==='cancel'&&f.id===first.id));
  h.send('headers',{id:first.id,status:200,headers:{'content-type':'application/json'}});h.send('chunk',{id:first.id,data:'e30='});h.send('end',{id:first.id});
  const live=h.host('/api/owner/connected/status');await flush();const second=h.frames.filter(f=>f.type==='request').at(-1);assert.ok(second.id>first.id);
  h.send('headers',{id:second.id,status:200,headers:{'content-type':'application/json'}});h.send('chunk',{id:second.id,data:'e30='});h.send('end',{id:second.id});
  assert.deepEqual(await (await live).json(),{});assert.equal(h.native.length,0);
});
test('browser bridge bounds chunks, sequence and streaming credit and retires every active response',async t=>{
  const h=await bridgeHarness(t);h.admit();await h.bridge.ready;
  const pending=h.host('/api/owner/connected/read/stream?cursor=0&once=0');await flush();const request=h.frames.find(f=>f.type==='request');
  h.send('headers',{id:request.id,status:200,headers:{'content-type':'text/event-stream'}});const response=await pending;
  const reader=response.body.getReader();h.send('chunk',{id:request.id,data:Buffer.from('id: 1\ndata: {}\n\n').toString('base64')});
  assert.match(new TextDecoder().decode((await reader.read()).value),/id: 1/);await flush();assert.ok(h.frames.some(f=>f.type==='pull'&&f.credit===1));
  h.send('chunk',{id:request.id,data:Buffer.alloc(16385).toString('base64')});await assert.rejects(reader.read());
  await assert.rejects(h.host('/api/owner/connected/status'));assert.equal(h.native.length,0);
});
test('browser bridge explicit missing offer and wrong origins fail closed without native host Fetch',async t=>{
  const timers=[];const h=await bridgeHarness(t,{setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{}});
  const ready=assert.rejects(h.bridge.ready);timers.find(t=>t.delay===8000).fn();await ready;
  await assert.rejects(h.host('/api/owner/connected/status'));assert.equal(h.native.length,0);
});
test('browser bridge maps exact carrier operation names, read task and canonical stream cursor',async t=>{
  const h=await bridgeHarness(t);h.admit();await h.bridge.ready;
  const promise=h.host('/api/owner/connected/read/tasks/'+wi+'?fleet=team');await flush();let req=h.frames.find(f=>f.type==='request');
  assert.deepEqual(req.request.input,{task_id:wi,fleet:'team'});assert.equal(req.request.operation,'read-task');
  h.send('headers',{id:req.id,status:200,headers:{'content-type':'application/json'}});h.send('end',{id:req.id});await (await promise).text();
  const pair=h.host('/api/owner/bootstrap/pair',{},false);await flush();req=h.frames.filter(f=>f.type==='request').at(-1);
  assert.equal(req.request.operation,'pair');assert.equal('token' in req.request,false);
  h.send('error',{id:req.id,outcome:'unknown'});await assert.rejects(pair);assert.equal(h.native.length,0);
});

test('explicit invalid browser selection cannot use legacy native reads without a document marker',async()=>{
  const original={document:globalThis.document,location:globalThis.location,fetch:globalThis.fetch};
  const apiText=(await read('api-client.js')).replace('"./connected-workspace.js"',JSON.stringify(workspaceURL))
    .replace('"./browser-owner-transport.js"',JSON.stringify(uri(await read('browser-owner-transport.js'))));
  try {
    let native=0;globalThis.document={getElementById:()=>null};globalThis.location={origin:'https://plane.claudfather.ai',search:'?owner_transport=invalid'};
    globalThis.fetch=async()=>{native++;return reply(200,{unexpected:true});};
    const api=await import(uri(apiText+'\n//invalid-explicit-browser'));
    assert.equal((await api.jget('/api/tasks')).state,'denied');assert.equal(native,0);api.dispose();
  } finally {for(const [k,v]of Object.entries(original)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
for (const corruption of ['sequence','session','headers','credit','early-end']) test('browser bridge retires on '+corruption,async t=>{
  const h=await bridgeHarness(t);h.admit();await h.bridge.ready;
  const pending=h.host('/api/owner/connected/read/stream');await flush();const req=h.frames.find(f=>f.type==='request');
  if(corruption==='early-end') {h.send('end',{id:req.id});await assert.rejects(pending);}
  else if(corruption==='sequence'||corruption==='session') {
    const rejection=assert.rejects(pending);
    h.send('headers',{id:req.id,status:200,headers:{'content-type':'application/json'},...(corruption==='sequence'?{seq:99}:{sessionId:'f'.repeat(64)})});await rejection;
  } else if(corruption==='headers') {
    const rejection=assert.rejects(pending);h.send('headers',{id:req.id,status:200,headers:{'set-cookie':'secret'}});await rejection;
  } else {
    h.send('headers',{id:req.id,status:200,headers:{'content-type':'text/event-stream'}});const response=await pending;
    for(let i=0;i<5;i++)h.send('chunk',{id:req.id,data:'e30='});await flush();await assert.rejects(response.text());
  }
  await assert.rejects(h.host('/api/owner/connected/status'));assert.equal(h.native.length,0);
});
test('admitted browser bridge survives redemption expiry while canonical account retirement cancels live work',async t=>{
  let clock=Date.now()/1000;
  const timers=[];const h=await bridgeHarness(t,{now:()=>clock,setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{}});
  h.admit();await h.bridge.ready;
  clock=h.doc.expires_at+120;
  const account=await h.bridge.fetch(h.doc.shell_origin+'/api/plane/context?workspace_id='+h.doc.workspace_id,{credentials:'include'});
  assert.deepEqual(await account.json(),{account:true});assert.equal(h.native.length,1);
  assert.equal(timers.some(timer=>timer.delay>45000),false,'redemption expiry is not an active workspace timer');
  const original={generation:id(11),request_id:id(12),config_fingerprint:'d'.repeat(64),source_revision:'e'.repeat(40)};
  const receipt=h.host('/api/owner/connected/setup/receipt',original);await flush();let req=h.frames.find(f=>f.type==='request');
  assert.deepEqual(req.request.input,original);
  h.send('headers',{id:req.id,status:200,headers:{'content-type':'application/json'}});h.send('chunk',{id:req.id,data:'e30='});h.send('end',{id:req.id});
  assert.deepEqual(await (await receipt).json(),{});
  const pending=h.host('/api/owner/connected/setup/receipt',original);await flush();req=h.frames.filter(f=>f.type==='request').at(-1);
  assert.deepEqual(req.request.input,original);const rejection=assert.rejects(pending);
  let retired=0;
  const native=harness({count:1,dependencies:{onRetire:()=>{retired++;h.bridge.dispose();}}});t.after(()=>native.api.dispose());
  await native.api.checkAccount();native.metadata={...native.metadata,context_generation:id(99)};
  assert.equal(await native.api.checkAccount(),false);assert.equal(retired,1);await rejection;
  await assert.rejects(h.host('/api/owner/connected/status'));assert.equal(h.native.length,1);
});

test('browser bridge waits for Preview 95 s (past the listener\'s 90 s), Launch 45 s and a setup read 15 s',async t=>{
  const timers=[];const h=await bridgeHarness(t,{setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{}});h.admit();await h.bridge.ready;
  const waits={};
  for(const [operation,body] of [['context',{}],['preview',{generation:id(8),goal:'g'}],['launch',{generation:id(8),request_id:id(10),config_fingerprint:'a'.repeat(64),source_revision:'b'.repeat(40)}]]){
    void h.host('/api/owner/connected/setup/'+operation,body).catch(()=>{});await flush();
    waits[operation]=timers.at(-1).delay;
  }
  assert.deepEqual(waits,{context:15000,preview:95000,launch:45000});
});
const ordinaryMetadata=()=>({request_id:id(81),kind:'message',scope:{workspace:'workspace',host:'host_'+'a'.repeat(32),fleet:'team',viewer:'original viewer'},target:{recipient:'actor_'+'c'.repeat(32),task_id:null},submitted_at:'2026-11-15T20:30:00.123Z'});
test('browser ordinary message facade preserves exact original metadata, finite bounds and unknown receipts',async t=>{
  const timers=[];const h=await bridgeHarness(t,{setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{}});h.admit();await h.bridge.ready;
  const context=h.host('/api/owner/connected/actions/context',{room:'team'});await flush();let req=h.frames.filter(f=>f.type==='request').at(-1);assert.equal(req.request.operation,'action-context');assert.equal(timers.at(-1).delay,15000);
  const large=JSON.stringify({padding:'x'.repeat(32769)});h.send('headers',{id:req.id,status:200,headers:{'content-type':'application/json'}});
  for(let i=0;i<large.length;i+=16384)h.send('chunk',{id:req.id,data:Buffer.from(large.slice(i,i+16384)).toString('base64')});h.send('end',{id:req.id});assert.equal((await (await context).json()).padding.length,32769);
  const original=ordinaryMetadata(),body='🙂'.repeat(2000);const sending=h.host('/api/owner/connected/actions/send',{...original,body});await flush();req=h.frames.filter(f=>f.type==='request').at(-1);
  assert.equal(req.request.operation,'action-send');assert.deepEqual(req.request.input,{...original,body});assert.equal(timers.at(-1).delay,45000);
  h.send('headers',{id:req.id,status:503,headers:{'content-type':'application/json'}});h.send('chunk',{id:req.id,data:Buffer.from('{"state":"unavailable","effect":"not_started"}').toString('base64')});h.send('end',{id:req.id});const refused=await sending;assert.equal(refused.status,503);assert.deepEqual(await refused.json(),{state:'unavailable',effect:'not_started'});
  const receipt=h.host('/api/owner/connected/actions/receipt',original);await flush();req=h.frames.filter(f=>f.type==='request').at(-1);assert.equal(req.request.operation,'action-receipt');assert.deepEqual(req.request.input,original);assert.equal(timers.at(-1).delay,15000);
  h.send('error',{id:req.id,outcome:'unknown'});await assert.rejects(receipt);await flush();assert.equal(h.frames.filter(f=>f.type==='request').length,3,'no automatic retry or replacement UUID');
  for(const [path,input] of [['prepare',{...original,body}],['context',{room:'team',kind:'nudge'}],['send',{...original,kind:'nudge',body}],['send',{...original,body,version:1}],['send',{...original,target:{...original.target,task_id:wi},body}],['send',{...original,scope:{...original.scope,extra:'no'},body}],['send',{...original,body:'\ud800'}],['send',{...original,body:'\u0085\u001c'}],['receipt',{...original,body}]])await assert.rejects(h.host('/api/owner/connected/actions/'+path,input));
  await assert.rejects(h.host('/api/owner/connected/actions/send?extra=1',{...original,body}));assert.equal(h.native.length,0);
});
test('explicit browser transport shows canonical guide recovery without native terminal enrollment',async t=>{
  const bridge=await bridgeHarness(t);assert.equal(bridge.bridge.fetch.browserOwnerTransport,true);
  const h=harness({count:1}),browserFetch=Object.defineProperty((...args)=>h.dependencies.fetch(...args),'browserOwnerTransport',{value:true});
  const workspace=createConnectedWorkspace({...h.dependencies,fetch:browserFetch});t.after(()=>workspace.dispose());
  workspace.mountSessionControls({document:h.ui.document,element:h.ui.get('owner-session')});await workspace.checkAccount();await flush();
  const nodes=descendants(h.ui.get('owner-session')),text=nodes.map(n=>n.textContent).join(' ');
  assert.match(text,/guided setup/);assert.match(text,/optional message approval/);
  assert.match(text,/finish first-team setup in the setup panel\./);assert.doesNotMatch(text,/setup here/,'the panel may be in the main area');assert.doesNotMatch(text,/DATA_ROOT|local terminal|confirm-workspace/);
  assert.equal(nodes.some(n=>n.dataset.operation==='enroll'),false);assert.equal(await workspace.act(0,'enroll'),false);
  assert.ok(nodes.filter(n=>n.tagName==='A').every(n=>n.hidden));
  const native=harness({count:1});native.mount();await native.api.checkAccount();assert.match(descendants(native.ui.get('owner-session')).map(n=>n.textContent).join(' '),/DATA_ROOT/);native.api.dispose();
});

// Saved-reference transport: absent is the historical HTTPS reference, the
// explicit SSH value is carried only by the browser owner transport.
const sshReference={...refs[0],endpoint_origin:'https://first.example.ts.net',transport:'mac_openssh'};
const sshDocument=()=>({...browserDocument(),references:[sshReference]});
test('browser bridge carries an explicit SSH reference only through its port, never a native fetch',async t=>{
  const h=await bridgeHarness(t,{document:sshDocument()});
  assert.deepEqual(h.sent[0].data.document.references,[sshReference],'the request names the exact saved transport');
  h.admit({reference:{...refs[0],endpoint_origin:sshReference.endpoint_origin}});await flush();
  assert.equal(h.frames.length,0,'an HTTPS-shaped offer cannot satisfy an SSH reference');
  h.admit();await h.bridge.ready;await flush();assert.equal(h.frames[0].type,'ready');
  const pending=h.host('/api/owner/connected/status');await flush();
  const request=h.frames.find(f=>f.type==='request');
  assert.deepEqual(request.request,{targetHandle:h.offer.targetHandle,operation:'session-status',input:{},token:'A'.repeat(43)});
  h.send('headers',{id:request.id,status:200,headers:{'content-type':'application/json'}});
  h.send('chunk',{id:request.id,data:Buffer.from('{"ok":true}').toString('base64')});h.send('end',{id:request.id});
  assert.deepEqual(await (await pending).json(),{ok:true});assert.equal(h.native.length,0);
  h.bridge.dispose();await assert.rejects(h.host('/api/owner/connected/status'));assert.equal(h.native.length,0,'no HTTPS fallback after retirement');
});
test('browser bridge keeps a legacy reference HTTPS-shaped and refuses an SSH offer for it',async t=>{
  const h=await bridgeHarness(t);
  h.admit({reference:{...h.doc.references[0],transport:'mac_openssh'}});await flush();assert.equal(h.frames.length,0);
  h.admit();await h.bridge.ready;await flush();assert.equal(h.frames[0].type,'ready');
});
for(const transport of ['https','ssh','MAC_OPENSSH','',null,1])
  test(`browser bridge refuses unknown saved transport ${JSON.stringify(transport)}`,async t=>{
    const {createBrowserOwnerTransport}=await bridgeModule(),doc={...browserDocument(),references:[{...sshReference,transport}]},sent=[],native=[];
    const parent={postMessage(data){sent.push(data);}};
    const window={parent,location:{origin:doc.operational_origin},addEventListener(){},removeEventListener(){}};
    const bridge=createBrowserOwnerTransport({documentContext:doc,window,nativeFetch:async(...args)=>{native.push(args);return reply(200,{});}});
    t.after(()=>bridge.dispose());
    assert.equal(sent.length,0,'no transport request is made for an unknown reference');
    await assert.rejects(bridge.ready);
    await assert.rejects(bridge.fetch(doc.references[0].endpoint_origin+'/api/owner/connected/status',{headers:{Authorization:'Bearer '+'A'.repeat(43)}}));
    assert.equal(native.length,0);
  });
test('connected workspace refuses an SSH computer without the browser carrier and labels it with one',async t=>{
  const references=[{...refs[0],transport:'mac_openssh'}];
  const native=harness({references});native.mount();
  assert.equal(native.api.snapshot().phase,'denied');await native.api.checkAccount();await flush();
  assert.equal(native.calls.length,0,'an SSH reference never reaches a native HTTPS fetch');native.api.dispose();
  for(const transport of ['https','ssh',null]){
    const unknown=harness({references:[{...refs[0],transport}]});assert.equal(unknown.api.snapshot().phase,'denied');unknown.api.dispose();
  }
  const h=harness({references}),browserFetch=Object.defineProperty((...args)=>h.dependencies.fetch(...args),'browserOwnerTransport',{value:true});
  h.api.dispose();
  const workspace=createConnectedWorkspace({...h.dependencies,fetch:browserFetch});t.after(()=>workspace.dispose());
  workspace.mountSessionControls({document:h.ui.document,element:h.ui.get('owner-session')});await workspace.checkAccount();await flush();
  const computer=workspace.snapshot().computers[0];
  assert.equal(computer.transport,'mac_openssh');assert.equal(computer.label,'a.example.test (SSH)');
  assert.equal(descendants(h.ui.get('owner-session')).some(node=>String(node.href||'').startsWith(refs[0].endpoint_origin)),false,'no HTTPS page is offered for an SSH computer');
  h.metadata=context([refs[0]]);await workspace.checkAccount();await flush();
  assert.notEqual(workspace.snapshot().phase,'ready','a saved reference that lost its transport is not the same computer');
});
test('connected read client admits an SSH reference only with the browser carrier',()=>{
  const reference={...refs[0],transport:'mac_openssh'};
  const base={reference,documentContext:{issuer:documentContext.shell_origin,operational_origin:documentContext.operational_origin,
    subject:id(1),workspace_id:id(2),context_generation:id(3)},documentOrigin:documentContext.operational_origin,
    currentContext:()=>null,obtainSessionAssertion:async()=>{throw new Error('unused');},monotonicNow:()=>0,setTimeout(){},clearTimeout(){}};
  const nativeFetch=async()=>{throw new Error('native fetch reached');};
  assert.throws(()=>createConnectedReadClient({...base,fetch:nativeFetch}));
  assert.throws(()=>createConnectedReadClient({...base,reference:{...refs[0],transport:'https'},fetch:Object.defineProperty(async()=>{},'browserOwnerTransport',{value:true})}));
  const client=createConnectedReadClient({...base,fetch:Object.defineProperty(async()=>{throw new Error('unused');},'browserOwnerTransport',{value:true})});
  assert.equal(typeof client.snapshot().state,'string');client.dispose();
});

// Browser carrier landing: the parent binds one saved reference, so the workspace opens on that computer.
test('browser bridge names its bound reference only after the parent offer, and its settle marker never rejects',async t=>{
  const h=await bridgeHarness(t),settled=[];
  assert.equal(h.bridge.fetch.browserOwnerReference,null,'no reference before the offer');
  void h.bridge.fetch.browserOwnerSettled.then(()=>settled.push('settled'));
  h.admit({reference:{...h.doc.references[0],revision:2}});await flush();
  assert.equal(h.bridge.fetch.browserOwnerReference,null,'a refused offer binds nothing');assert.deepEqual(settled,[]);
  h.admit();await h.bridge.ready;await flush();
  assert.equal(h.bridge.fetch.browserOwnerReference,h.doc.references[0].reference_id);assert.deepEqual(settled,['settled']);
  assert.throws(()=>{h.bridge.fetch.browserOwnerReference='other';},TypeError,'the marker cannot be retargeted');
  h.bridge.dispose();await assert.rejects(h.host('/api/owner/connected/status'));
  assert.equal(h.bridge.fetch.browserOwnerReference,h.doc.references[0].reference_id,'retirement keeps the name but no request goes anywhere');
  assert.equal(h.native.length,0);
  const timers=[],lost=await bridgeHarness(t,{setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{}});
  timers.find(timer=>timer.delay===8000).fn();await lost.bridge.fetch.browserOwnerSettled;
  assert.equal(lost.bridge.fetch.browserOwnerReference,null,'a carrier that never bound names no computer');
});
const landingRefs=['a','b','c'].map((n,i)=>({reference_id:id(i+4),endpoint_origin:`https://${n}.example.test`,revision:1}));
const landingContext={version:2,template:'revenue-web-app',repository:'example-org/shop',fleet:'shop',source_revision:'b'.repeat(40),
  generation:id(9),expires_at:1800003600,request:null,launch_reserved:false,provider_signin:true,signin_attempt:null};
// A browser-carrier fetch double: the same markers as browser-owner-transport.js over the harness fetch.
function browserCarrier(h,bound=null) {
  const settled=deferred();let reference=bound;
  const fetch=Object.defineProperties((...args)=>h.dependencies.fetch(...args),{browserOwnerTransport:{value:true},
    browserOwnerReference:{get:()=>reference},browserOwnerSettled:{value:settled.promise}});
  return {fetch,bind(value){reference=value;settled.resolve();},settle(){settled.resolve();}};
}
// The frame's documentElement and window as a browser gives an embedded page; posts are recorded.
// resize() delivers one ResizeObserver notification, as a browser does at most once per rendering frame.
function framedPage(ui) {
  const page={attributes:{},height:640,setAttribute(k,v){this.attributes[k]=String(v);},getAttribute(k){return this.attributes[k]??null;},
    getBoundingClientRect(){return {height:this.height};}};
  const posts=[],observers=[];
  class ResizeObserver {constructor(fn){this.fn=fn;this.targets=[];observers.push(this);}observe(target){this.targets.push(target);}disconnect(){this.closed=true;}}
  const view={parent:{postMessage:(...args)=>posts.push(args)},ResizeObserver};
  ui.document.documentElement=page;ui.document.defaultView=view;
  return {page,view,posts,observers,
    sizes:()=>posts.filter(([message])=>message.type==='claudfather.workspace.size'),
    resize(height){page.height=height;for(const o of observers)if(!o.closed)o.fn([{target:o.targets[0]}],o);}};
}
// One localStorage per test, restored once after every landing in it closes (t.after runs first-in, first-out).
const landingStorage=new WeakMap();
function landing(t,{bound=landingRefs[0].reference_id,browser=true,attention=[],references=landingRefs,metadata}={}) {
  if(!landingStorage.has(t)){const restore=stubLocalStorage();landingStorage.set(t,[]);t.after(()=>{for(const close of landingStorage.get(t))close();restore();});}
  const h=harness({references}),frame=framedPage(h.ui),carrier=browser?browserCarrier(h,bound):null;
  if(metadata)h.metadata=metadata;
  if(browser)h.api.dispose();
  const workspace=browser?createConnectedWorkspace({...h.dependencies,fetch:carrier.fetch}):h.api;
  landingStorage.get(t).push(()=>workspace.dispose()); // the guide keeps its saved setup in localStorage, as in a browser
  workspace.mountSessionControls({document:h.ui.document,element:h.ui.get('owner-session'),onConnectionAttention:event=>attention.push(event)});
  const cards=()=>h.ui.get('owner-session').children.filter(e=>e.className==='connection-card');
  const rail=h.ui.get('rail-channel'),host=rail.children[0];
  return {h,frame,carrier,workspace,cards,rail,host,attention,
    panel:()=>host.children.find(n=>n.className==='setup-focus-panel'),
    status:()=>host.children.find(n=>n.className==='setup-only-status'),
    shownCards:()=>cards().filter(visible).map(card=>card.children[0].textContent),
    // Every visible line of text in the setup view.
    shownText:()=>descendants(host).filter(n=>visible(n)&&!n.children.length&&n.textContent).map(n=>n.textContent),
    mutations:()=>h.calls.filter(c=>/\/api\/owner\/connected\/(?:challenge|issue|renew|logout|setup\/)/.test(c.url))};
}
const stepNode=(panel,name,className)=>descendants(stepsOf(panel).find(n=>n.dataset.step===name)).find(n=>n.className===className);
// Plain sentences only: no raw state names (not_connected, setup_only) and no builder vocabulary.
const assertPlain=texts=>{for(const text of texts)assert.doesNotMatch(text,/\b[a-z]+_[a-z_]+\b|snapshot|summar(?:y|ies) are|remediation|host data/i,text);};
test('browser carrier opens on its bound computer: a focused panel whose first step is an explicit Connect',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  assert.deepEqual(l.shownCards(),['a.example.test · Not connected'],'computers this carrier cannot reach are not listed');
  assert.equal(l.cards().length,3,'every saved computer is still admitted; only the bound one is shown');
  assert.equal(l.frame.page.attributes['data-workspace-view'],'setup','workspace.css shows only the setup panel under this marker');
  assert.equal(l.host.hidden,false);assert.equal(l.rail.dataset.setupFocus,'only','the panel takes the main area before Connect');
  assert.equal(l.status().hidden,true,'no waiting line beside the panel');
  const panel=l.panel(),heading=panel.children.find(n=>n.tagName==='H2');
  assertPlain(l.shownText());
  assert.equal(heading.textContent,'Finish setting up a.example.test');
  assert.deepEqual(stepShape(panel),[['connect','current'],['approve','upcoming'],['signin','upcoming'],['goal','upcoming'],['launch','upcoming']]);assert.ok(unnumbered(panel));
  assert.deepEqual(stepsOf(panel).filter(visible).map(filledIn),[['Connect'],[],[],[],[]],'Connect is the one filled action');
  const connect=stepsOf(panel)[0],button=descendants(connect).find(n=>n.tagName==='BUTTON');
  assert.equal(connect.children.find(n=>n.tagName==='H3').textContent,'Connect this computer');
  assert.ok(descendants(connect).some(n=>n.textContent==='Connect so you can finish setup.'&&visible(n)));
  assert.equal(button.textContent,'Connect');assert.equal(button.type,'button');assert.equal(button.disabled,false);
  assert.equal(stepPart(panel,'connect','setup-progress').attributes.role,'status');
  assert.equal(descendants(panel).find(n=>n.className==='setup-note').textContent,'','the step says what to do; no second Connect note');
  const card=l.cards()[0];
  assert.match(card.children[1].children[0].className,/connection-primary-quiet/,'the card keeps Connect, quietly; Go to setup leads');
  assert.equal(card.children[1].children[1].hidden,false);assert.equal(card.children[2].textContent,'Finish setting up this computer in the setup panel.');
  assert.match(l.h.ui.get('owner-session').children[0].children[0].textContent,/^Connect this computer to view its teams\./);
  assert.deepEqual(l.attention.map(event=>event.state),['not_connected'],'only the listed computer asks for attention');
  // No automatic sign-in: account rechecks and timers never connect.
  await l.workspace.checkAccount();l.h.timer(30000);await flush();
  assert.deepEqual(l.mutations(),[],'nothing is sent to a computer before the click');
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/context',()=>reply(200,landingContext));
  button.focus();button.click();
  assert.equal(button.disabled,true);assert.equal(stepPart(panel,'connect','setup-progress').textContent,'Connecting…');
  assert.equal(stepPart(panel,'connect','setup-progress').dataset.tone,'working');
  await flush();
  assert.deepEqual(l.mutations().map(c=>new URL(c.url).pathname.split('/').at(-1)),['challenge','issue','context'],'one click, one connect, then one approval read');
  assert.equal(l.mutations().every(c=>c.url.startsWith(landingRefs[0].endpoint_origin)),true);
  assert.equal(l.workspace.snapshot().computers[0].state,'setup_only');
  assert.equal(l.panel(),panel,'the same panel continues the setup');assert.equal(l.rail.dataset.setupFocus,'only');
  assert.deepEqual(stepShape(panel).map(row=>row.slice(0,2)),[['connect','done'],['approve','done'],['signin','current'],['goal','upcoming'],['launch','upcoming']]);
  assert.ok(descendants(stepsOf(panel)[0]).some(n=>n.textContent==='Connected'&&visible(n)));
  assert.ok(descendants(stepsOf(panel)[1]).some(n=>n.textContent==='Setup approved'&&visible(n)));
  assert.deepEqual(stepsOf(panel).filter(visible).map(filledIn),[[],[],['Sign in to Claude'],[],[]]);
  assert.equal(l.h.ui.document.activeElement?.textContent,'Sign in to Claude','keyboard focus moves on to the next action');
  l.h.timer(30000);await flush();
  assert.deepEqual(l.h.calls.filter(c=>c.url.includes('/connected/setup/')).map(c=>c.url.split('/').at(-1)),['context'],
    'reading the approval is the only setup call; sign-in waits for its own click');
  assert.deepEqual(l.shownCards(),['a.example.test · First-team setup']);
});
test('a failed approval read after Connect keeps Check setup approval, focuses it and does not retry',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/context',()=>reply(503,{}));
  const panel=l.panel(),button=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON');
  button.focus();button.click();await flush();
  assert.deepEqual(stepShape(panel).map(row=>row.slice(0,2)).slice(0,3),[['connect','done'],['approve','current'],['signin','upcoming']]);
  assert.deepEqual(stepsOf(panel).filter(visible).map(filledIn),[[],['Check setup approval'],[],[],[]]);
  assert.equal(l.h.ui.document.activeElement?.textContent,'Check setup approval');
  assertPlain(l.shownText());
  assert.ok(l.shownText().includes('Could not confirm setup approval. Make sure setup was approved on this computer, then check again.'));
  for(let round=0;round<3;round++){for(const t of l.h.timers.filter(t=>t.active)){t.active=false;t.fn();}await flush();}
  assert.equal(l.h.calls.filter(c=>c.url.includes('/connected/setup/')).length,1,'one automatic read, never a retry');
});
test('the approval read after Connect does not take focus the visitor moved elsewhere',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  const gate=deferred();
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/context',()=>gate.promise.then(()=>reply(200,landingContext)));
  const panel=l.panel(),button=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON');
  button.focus();button.click();await flush();
  const details=descendants(panel).find(n=>n.tagName==='DETAILS');details.focus();
  gate.resolve();await flush();
  assert.equal(stepShape(panel)[2][1],'current');
  assert.equal(l.h.ui.document.activeElement,details,'focus stays where the visitor put it');
});
// A standalone panel whose own Connect was chosen; finish() makes that connection live as the entry does.
function connectedPanel({saved=[],context={},client={}}={}) {
  const h=setupHarness({saved,client,setupOptions:{setTimeout:()=>0,clearTimeout:()=>{}}});
  Object.assign(h.context,context);h.change({state:'not_connected',epoch:2});
  const ui=dom(),panel=new ui.Element();
  h.api.mountPanel({document:ui.document,element:panel,standalone:true,connect:()=>h.api.showConnect({operation:'connect',busy:'connect'})});
  h.api.showConnect({operation:'connect'});
  const connect=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON');connect.focus();connect.click();
  return {h,panel,focused:()=>ui.document.activeElement,reads:()=>h.calls.filter(c=>c[0]==='context').length,
    async finish(state='setup_only'){h.change({state,epoch:3});h.api.showConnect({operation:'connect'});await flush();}};
}
const settingUp={generation:id(9),request_id:id(10),config_fingerprint:'a'.repeat(64),source_revision:'b'.repeat(40)};
test('after a panel Connect, focus moves to whatever the next shown action is',async()=>{
  for(const [name,options,expected] of [
    ['no Claude sign-in in this approval',{},'TEXTAREA'],
    ['a sign-in started earlier in this browser',{saved:[['test:claude',JSON.stringify({generation:id(9),attempt_id:id(12)})]],
      context:{provider_signin:true,signin_attempt:{generation:id(9),attempt_id:id(12),state:'waiting',reason:null,url:authorizeURL}}},'Open Claude sign-in'],
    ['a setup already submitted from this browser',{saved:[['test',JSON.stringify({version:2,pending:settingUp,conflicts:[],legacy:[]})]],
      context:{request:settingUp,launch_reserved:true}},'Check launch status']]){
    const p=connectedPanel(options);await p.finish();
    assert.equal(p.reads(),1,name);
    const focused=p.focused();
    assert.equal(expected==='TEXTAREA'?focused?.tagName:focused?.textContent,expected,name);
    assert.ok(visible(focused)&&!focused.disabled,`${name}: focus is on a shown, enabled control`);
    p.h.api.dispose();
  }
});
test('a computer that is already set up gets no automatic approval read after Connect',async()=>{
  const p=connectedPanel();await p.finish('ready');
  assert.equal(p.reads(),0,'only a computer in first-team setup is read automatically');
  assert.equal(p.focused()?.textContent,'Check setup approval','focus moves to the step\'s own action, as before');
  p.h.api.dispose();
});
test('a panel disposed during the approval read moves no focus',async()=>{
  const gate=deferred(),p=connectedPanel({client:{setupContext:()=>gate.promise}});
  await p.finish();assert.equal(p.reads(),0,'the gated read is in flight');
  const before=p.focused();p.h.api.dispose();gate.resolve(p.h.context);await flush();
  assert.equal(p.focused(),before);
});test('a failed approval read never leaves Approve setup marked done beside its error',async()=>{
  // The host names a different sign-in attempt from the one this browser kept, so the read is refused after parsing.
  const h=setupHarness({saved:[['test:claude',JSON.stringify({generation:id(9),attempt_id:id(12)})]]}),ui=dom(),panel=new ui.Element();
  Object.assign(h.context,{provider_signin:true,signin_attempt:{generation:id(9),attempt_id:id(13),state:'waiting',reason:null,url:authorizeURL}});
  h.api.mountPanel({document:ui.document,element:panel});
  assert.equal(await h.api.run('context'),false);
  assert.equal(h.api.snapshot().context,null);
  assert.equal(stepShape(panel)[0][1],'current','approve stays the current step');
  assert.deepEqual(filledIn(stepsOf(panel)[0]),['Check setup approval']);
  h.api.dispose();
});


test('a refused Connect from the panel keeps Connect, says why, and returns focus to it without retrying',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/challenge',()=>reply(503,{}));
  const panel=l.panel(),button=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON');
  button.focus();button.click();await flush();
  assert.equal(l.mutations().length,1,'one challenge, no retry');
  assert.equal(stepShape(panel)[0][1],'current');assert.equal(button.disabled,false);assert.equal(button.textContent,'Connect');
  assert.equal(stepNode(panel,'connect','setup-hint').textContent,'Could not connect to this computer. Make sure it is on and online, then choose Connect again.');
  assertPlain(l.shownText());
  assert.equal(l.h.ui.document.activeElement,button,'focus returns to Connect');
  assert.equal(l.rail.dataset.setupFocus,'only');
  l.h.timer(30000);await flush();assert.equal(l.mutations().length,1,'nothing retries automatically');
});
test('browser carrier reads and summaries leave out the computers it cannot reach',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  for(const path of ['/api/overview','/api/tasks','/api/channel?limit=120']) {
    const value=await l.workspace.jget(path);
    assert.equal(value.state,'unavailable',path);
    assert.match(value.remediation,/^a\.example\.test: Not connected\./,path);
    assert.doesNotMatch(value.remediation,/[bc]\.example\.test/,path+' names only the bound computer');
  }
  const overview=await l.workspace.jget('/api/overview');
  assert.deepEqual(overview.data.sources.map(s=>s.label),['a.example.test']);assert.equal(overview.data.coverage.total,1);
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  assert.equal(await l.workspace.act(0,'connect'),true);
  assert.equal(l.host.hidden,false,'the panel stays in the main area through setup');
  assert.equal(l.h.calls.filter(c=>/[bc]\.example\.test/.test(c.url)).length,0,'no request is sent toward a hidden computer');
});
test('an offer admitted after the account check collapses the list onto its computer and refreshes the boards',async t=>{
  const l=landing(t,{bound:null});await l.workspace.checkAccount();await flush();
  assert.equal(l.shownCards().length,3,'until the parent names a computer, every saved computer is listed');
  assert.equal(l.host.hidden,false,'the setup view keeps the main area');assert.equal(l.panel(),undefined,'no computer is chosen for the parent');
  assert.equal(l.status().hidden,false);assert.equal(l.status().textContent,'Opening the connection to your computer…');
  assert.equal(l.status().dataset.tone,'working','the first wait turns a ring too');
  assert.equal(l.status().attributes.role,'status');
  const stream=l.workspace.createEventSource('/api/stream'),events=[];stream.onmessage=event=>events.push(JSON.parse(event.data));
  l.carrier.bind(landingRefs[1].reference_id);await flush();
  assert.deepEqual(l.shownCards(),['b.example.test · Not connected']);
  assert.equal(l.status().hidden,true);assert.equal(l.panel().children.find(n=>n.tagName==='H2').textContent,'Finish setting up b.example.test');
  assert.equal(events.at(-1)?.coverage_changed,true,'the boards read again with the new presentation');
  assert.deepEqual(events.at(-1).overview.data.sources.map(s=>s.label),['b.example.test']);
  assert.deepEqual(l.mutations(),[]);stream.close();
  // A carrier that retires without an offer says so plainly, shows no computer and connects nothing.
  const lost=landing(t,{bound:null});await lost.workspace.checkAccount();lost.carrier.settle();await flush();
  assert.equal(lost.status().textContent,'This page could not reach your computer. Reopen setup from your workspace.');
  assert.equal(lost.panel(),undefined);assert.deepEqual(lost.mutations(),[]);
});
test('native workspace keeps every saved computer and its setup panel exactly as before',async t=>{
  const attention=[],l=landing(t,{browser:false,attention});await l.h.api.checkAccount();await flush();
  assert.deepEqual(l.shownCards(),['a.example.test · Not connected','b.example.test · Not connected','c.example.test · Not connected']);
  assert.equal(l.host.hidden,true);assert.equal(l.rail.dataset.setupFocus,undefined,'nothing takes the main area before setup-only');
  assert.equal(l.frame.page.attributes['data-workspace-view'],undefined,'native keeps the whole workspace');assert.equal(l.status(),undefined);
  assert.deepEqual(l.frame.posts,[[{type:'claudfather.workspace.ready',version:1},documentContext.shell_origin]],'native posts only its ready message');
  assert.equal(l.frame.observers.length,0,'native never measures itself for the parent');
  const panel=descendants(l.cards()[0]).find(n=>n.className==='setup-focus-panel');
  assert.deepEqual(stepShape(panel).map(row=>row[0]),['approve','signin','goal','launch'],'no Connect step');
  assert.match(l.h.ui.get('owner-session').children[0].children[0].textContent,/^Connect each computer/);
  assert.equal(attention.length,3);
  assert.match((await l.h.api.jget('/api/tasks')).remediation,/a\.example\.test.*b\.example\.test.*c\.example\.test/);
  // A reference marker without the browser-carrier marker is ignored.
  const marked=harness({references:landingRefs}),fetch=Object.defineProperty((...args)=>marked.dependencies.fetch(...args),'browserOwnerReference',{value:landingRefs[0].reference_id});
  const other=createConnectedWorkspace({...marked.dependencies,fetch});t.after(()=>other.dispose());
  other.mountSessionControls({document:marked.ui.document,element:marked.ui.get('owner-session')});await other.checkAccount();await flush();
  assert.equal(marked.ui.get('owner-session').children.filter(e=>e.className==='connection-card'&&visible(e)).length,3);
  assert.equal(marked.ui.get('rail-channel').children[0].hidden,true);
});
test('the setup-only view hides the header, sidebar, rooms, composer and diagnostics, and nothing in it scrolls on its own',async()=>{
  const css=(await read('workspace.css')).replace(/\/\*[\s\S]*?\*\//g,'').replace(/\s+/g,' '),html=await read('index.html');
  const rules=[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([,selectors,body])=>({selectors:selectors.split(',').map(s=>s.trim()),body}));
  const ruleFor=selector=>rules.filter(rule=>rule.selectors.includes(selector)).map(rule=>rule.body).join(';');
  const setup=':root[data-workspace-view="setup"]';
  for(const selector of [`${setup} .plane-workspace > header`,`${setup} #rail-fleet`,`${setup} #rail-channel > :not(#setup-focus)`])
    assert.match(ruleFor(selector),/display:none !important/,selector);
  for(const selector of [setup,`${setup} body.plane-workspace`,`${setup} #rail-channel`,`${setup} .plane-workspace .setup-focus`])
    assert.match(ruleFor(selector),/overflow:visible/,selector+' grows with its content');
  assert.match(ruleFor(`${setup} .plane-workspace main`),/height:auto/);assert.match(ruleFor(`${setup} .setup-panel`),/max-width:none/,'the panel fills the frame width');
  // What those rules hide, in the canonical page.
  assert.match(html.slice(html.indexOf('<header>'),html.indexOf('</header>')),/id="view-nav"/,'Rooms, Tasks and More sit in the header');
  const fleet=html.slice(html.indexOf('id="rail-fleet"'),html.indexOf('id="rail-channel"'));
  for(const part of ['id="fleet-tabs"','id="owner-session"','id="overview"','id="rail-debug"'])assert.ok(fleet.includes(part),part+' is in the sidebar');
  const channel=html.slice(html.indexOf('id="rail-channel"'),html.indexOf('</main>'));
  for(const part of ['id="room-title"','id="search"','id="channel"','id="rail-right"','id="trust"','id="work-loop"'])assert.ok(channel.includes(part),part+' is beside the setup host');
  assert.doesNotMatch(html,/id="setup-focus"/,'the workspace adds the setup host as the one shown child');
});
test('setup-only view shows only the bridged computer\'s panel and a plain line while it has none, and stays on the panel once connected',async t=>{
  const l=landing(t);
  assert.equal(l.status().textContent,'Checking your workspace…');assert.equal(l.status().dataset.tone,'working');assert.equal(l.panel(),undefined);
  assert.equal(l.rail.children.length,1,'the setup host is the only thing the view adds');
  await l.workspace.checkAccount();await flush();
  const panel=l.panel(),text=l.shownText();
  assert.deepEqual(l.host.children.filter(visible),[panel],'one panel; no other computer and no summary');
  assert.equal(descendants(l.host).filter(n=>n.className==='setup-focus-panel').length,1);
  for(const phrase of [/Message team/,/[Uu]navailable/,/Recorder/,/All teams/,/b\.example\.test|c\.example\.test/])
    assert.equal(text.some(line=>phrase.test(line)),false,String(phrase));
  assertPlain(text);
  // Already set up: Connect gives a readable computer, and the view still shows only its panel.
  descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON').click();await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  assert.equal(l.panel(),panel);assert.equal(l.rail.dataset.setupFocus,'only','the workspace is not revealed beside the panel');
  assert.equal(stepShape(panel)[0][1],'done');assert.equal(l.status().hidden,true);
  // An account change leaves a plain closing line, not the setup panel.
  l.h.metadata=context([landingRefs[1]]);await l.workspace.checkAccount();await flush();
  assert.equal(l.workspace.snapshot().phase,'denied');
  assert.equal(l.h.ui.get('main').children.length,1,'only the closing line remains in the main area');
  assertPlain(descendants(l.h.ui.get('main')).map(n=>n.textContent).filter(Boolean));
});
test('setup-only view posts its content height to the exact shell origin, once per observation and only when it changes',async t=>{
  const l=landing(t),{frame}=l,ready=[{type:'claudfather.workspace.ready',version:1},documentContext.shell_origin];
  assert.deepEqual(frame.posts,[ready],'the ready message comes first; no size before a measurement');
  assert.equal(frame.observers.length,1);assert.deepEqual(frame.observers[0].targets,[frame.page],'the observer watches the root element');
  await l.workspace.checkAccount();await flush();assert.equal(frame.sizes().length,0,'rendering alone posts nothing; the observer reports');
  frame.resize(900.4);
  assert.deepEqual(frame.sizes(),[[{type:'claudfather.workspace.size',version:1,height:901},documentContext.shell_origin]]);
  assert.deepEqual(Object.keys(frame.sizes()[0][0]),['type','version','height'],'exactly these keys');
  frame.resize(900.1);frame.resize(901);assert.equal(frame.sizes().length,1,'an unchanged height is not posted again');
  for(const height of [0,-5,50000,20001,Number.NaN,Infinity,640])frame.resize(height);
  assert.deepEqual(frame.sizes().map(([message])=>message.height),[901,1,20000,640],'whole CSS pixels between 1 and 20000');
  assert.ok(frame.sizes().every(([message])=>Number.isInteger(message.height)));
  assert.ok(frame.posts.every(([,origin])=>origin===documentContext.shell_origin),'never a wildcard target');
  l.workspace.dispose();assert.equal(frame.observers[0].closed,true,'disposal stops observing');
  frame.observers[0].fn([]);frame.page.height=700;frame.observers[0].fn([]);
  assert.equal(frame.sizes().length,4,'nothing is posted after the view closes');
});
test('a framed setup-only view without ResizeObserver still announces ready and posts no size',async t=>{
  const restore=stubLocalStorage(),h=harness({references:landingRefs}),frame=framedPage(h.ui),carrier=browserCarrier(h,landingRefs[0].reference_id);
  h.api.dispose();delete frame.view.ResizeObserver;
  const workspace=createConnectedWorkspace({...h.dependencies,fetch:carrier.fetch});t.after(()=>{workspace.dispose();restore();});
  workspace.mountSessionControls({document:h.ui.document,element:h.ui.get('owner-session')});await workspace.checkAccount();await flush();
  assert.deepEqual(frame.posts,[[{type:'claudfather.workspace.ready',version:1},documentContext.shell_origin]]);
  assert.equal(frame.page.attributes['data-workspace-view'],'setup');
});
test('a standalone panel ends on a done Launch step with a plain running line; the shared panel keeps its lead guidance',async()=>{
  for(const standalone of [true,false]){
    const h=setupHarness(),ui=dom(),panel=new ui.Element();
    h.api.mountPanel({document:ui.document,element:panel,...(standalone?{standalone:true,connect:()=>{}}:{})});
    assert.equal(await h.api.run('context'),true);h.api.setGoal('Improve checkout tests');assert.equal(await h.api.run('preview'),true);
    await h.api.run('launch');await h.api.run('receipt');
    const shape=stepShape(panel).map(row=>row.slice(0,2)),note=descendants(panel).find(n=>n.className==='setup-note').textContent;
    if(standalone){
      assert.deepEqual(shape,[['connect','done'],['approve','done'],['goal','done'],['launch','done']]);
      assert.ok(descendants(stepsOf(panel).at(-1)).some(n=>n.textContent==='Team launched'&&visible(n)));
      assert.equal(note,'Team launched. Your team is running on this computer.');
    } else {
      assert.deepEqual(shape,[['approve','done'],['goal','done'],['launch','current']],'the shared panel is unchanged');
      assert.match(note,/^Team launched\. Once its bots show as running, message the lead/);
    }
    assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
  }
});
test('a standalone managed launch ends on finished steps after its status check moves the connection on',async()=>{
  for(const standalone of [true,false]){
    const timers=[];let h=null;
    h=setupHarness({setupOptions:{setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}},
      client:{async setupReceipt(input){return {...input,version:2,state:'done',managed_owner:true,launch_reserved:true,config_committed:true,health_errors:{}};},
        // As the connected read client: the same session's status check goes setup_only, checking, then ready.
        async check(){h.change({state:'checking'});h.change({state:'ready',epoch:2});}}});
    h.context.managed_owner=true;
    const ui=dom(),panel=new ui.Element();
    h.api.mountPanel({document:ui.document,element:panel,...(standalone?{standalone:true,connect:()=>{}}:{})});
    await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');await h.api.run('launch');
    const handoff=stepPart(panel,'launch','setup-progress');
    assert.equal(handoff.textContent,'Checking automatically while your team connects…');assert.equal(handoff.dataset.tone,'working','the handoff wait turns a ring');
    while(timers.length){await timers.shift()();await flush();}
    assert.equal(h.client.snapshot().state,'ready','the handoff promoted the connection and cleared the approval context');
    const shape=stepShape(panel).map(row=>row.slice(0,2)),note=descendants(panel).find(n=>n.className==='setup-note').textContent;
    const buttons=descendants(panel).filter(n=>n.tagName==='BUTTON'&&visible(n)).map(n=>n.textContent);
    if(standalone){
      assert.deepEqual(shape,[['connect','done'],['approve','done'],['goal','done'],['launch','done']]);
      assert.deepEqual(buttons,[],'nothing is left to approve, check again or launch');
      assert.ok(descendants(stepsOf(panel)[1]).some(n=>n.textContent==='Setup approved'&&visible(n)));
      assert.ok(descendants(stepsOf(panel).at(-1)).some(n=>n.textContent==='Team launched'&&visible(n)));
      assert.equal(note,'Team launched. Your team is running on this computer.');
    } else {
      assert.deepEqual(shape[0],['approve','current'],'the shared panel keeps its existing steps');
      assert.match(note,/^Team launched\. Once its bots show as running/);
    }
    assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
  }
});
test('a rebound setup request is not shown as launched, so its launch status can be checked',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element();
  h.api.mountPanel({document:ui.document,element:panel,standalone:true,connect:()=>{}});
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');await h.api.run('launch');await h.api.run('receipt');
  assert.equal(stepShape(panel).at(-1)[1],'done');
  Object.assign(h.context,{request:{...h.pending,request_id:id(11)},launch_reserved:true});
  assert.equal(await h.api.run('context'),true);
  assert.deepEqual(stepShape(panel).map(row=>row.slice(0,2)),[['connect','done'],['approve','done'],['goal','done'],['launch','current']]);
  assert.deepEqual(filledIn(stepsOf(panel).at(-1)),['Check launch status'],'the new request leads with its status check');
  assert.equal(descendants(stepsOf(panel).at(-1)).some(n=>n.textContent==='Team launched'&&visible(n)),false);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
});
test('Connect moves keyboard focus on only while this frame has focus and nothing else took it',async t=>{
  for(const away of ['element','frame']){
    const l=landing(t);await l.workspace.checkAccount();await flush();
    l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
    const panel=l.panel(),button=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON'),heading=panel.children.find(n=>n.tagName==='H2');
    button.focus();button.click();
    if(away==='element')heading.focus();else l.h.ui.document.hasFocus=()=>false;
    await flush();
    assert.equal(l.workspace.snapshot().computers[0].state,'setup_only');
    assert.equal(l.h.ui.document.activeElement,away==='element'?heading:null,'focus stays where the person left it: '+away);
  }
  // A refused Connect does not pull focus back once the person moved on.
  const l=landing(t);await l.workspace.checkAccount();await flush();
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/challenge',()=>reply(503,{}));
  const panel=l.panel(),button=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON'),heading=panel.children.find(n=>n.tagName==='H2');
  button.focus();button.click();heading.focus();await flush();
  assert.equal(button.disabled,false);assert.equal(l.h.ui.document.activeElement,heading);
  assert.equal(l.mutations().length,1,'one challenge, no retry');
});
test('browser carrier: one saved computer opens its panel; an account listing only the bound computer of several is refused',async t=>{
  // The document and account both list one computer: the ordinary single-computer setup.
  const one=landing(t,{references:[landingRefs[0]]});await one.workspace.checkAccount();await flush();
  assert.equal(one.workspace.snapshot().phase,'ready');
  assert.equal(one.panel().children.find(n=>n.tagName==='H2').textContent,'Finish setting up a.example.test');
  assert.deepEqual(stepShape(one.panel())[0].slice(0,2),['connect','current']);assert.deepEqual(one.mutations(),[]);
  // The document saved three computers but the account lists only the bound one: the saved set changed, so nothing is admitted.
  const partial=landing(t,{metadata:context([landingRefs[0]])});await partial.workspace.checkAccount();await flush();
  assert.equal(partial.workspace.snapshot().phase,'denied','the complete saved-reference set is still required');
  assert.equal(partial.panel(),undefined);assert.deepEqual(partial.mutations(),[]);
  assert.equal(partial.h.calls.filter(c=>!c.url.startsWith(documentContext.shell_origin)).length,0,'no request reaches any computer');
  assert.equal(partial.status().textContent,'Your account or saved computers changed. Reopen setup from your workspace.');
  assert.equal(partial.h.ui.get('main').children.length,1,'only the closing line remains in the main area');
});
