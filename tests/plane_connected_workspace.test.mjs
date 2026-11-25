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
const partsURL = new URL('../claudlobby/plane/ui/workspace-parts.js',import.meta.url).href;
const workspaceURL = uri(workspaceText.replace('"./guided-setup.js"',JSON.stringify(new URL('../claudlobby/plane/ui/guided-setup.js',import.meta.url).href)).replace('"./workspace-parts.js"',JSON.stringify(partsURL)).replace('"./workspace-enrollment.js"',JSON.stringify(enrollmentURL)).replace('"./connected-read-client.js"',JSON.stringify(carrierURL)).replace('"./two-source-read.js"',JSON.stringify(adapterURL)));
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
  // The parts read a ready computer gets is neither an approval check nor a launch.
  // Only computer B, which is ready, is sent it.
  const setupCalls=()=>h.calls.filter(c=>c.url.includes('/api/owner/connected/setup/')&&c.url!==refs[1].endpoint_origin+'/api/owner/connected/setup/state');
  assert.equal(setupCalls().length,0,'revealing guidance never checks approval or launches setup');
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
  assert.equal(setupCalls().length,0);
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
  const storage={getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,value),...options.storage?.(saved)};
  const api=createGuidedSetup({client,storage,storageKey:'test',now:()=>1800000000,...options.setupOptions});
  return {api,client,calls,saved,pending,context,preview,storage,
    change(next){state={...state,...next};api.connectionChanged(state);for(const fn of listeners)fn(state);}};
}
// The words before Launch team, approved 2026-11-24.
const READY_BANNER='Your team is ready to launch.';
const LAUNCH_STARTS='Launch starts your lead, engineer and reviewer on this computer. The lead plans the work, the engineer does it and the reviewer checks it. Merging, deploying, extra spending and destructive changes each need your permission first.';
const OTHERS_RESTART='Bots already running here restart briefly.';
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
  assert.equal(h.api.snapshot().note,READY_BANNER);
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
test('a check that gets no answer or an unusable one invites looking again; the steps stay',async()=>{
  for (const [answer,note] of [[fail(timedOut()),'Could not get an answer from your computer about the preview. Choose Check preview to look again.'],
      [()=>({preview_state:'later'}),'Your computer’s answer about this setup did not match what this page expects. Choose Check preview to look again.']]) {
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
test('an approval that ends with no read in between replaces a note offering Check preview',async()=>{
  const p=previewHarness({answers:[fail(timedOut())]}),{h,panel}=p;
  await p.start();
  assert.match(h.api.snapshot().note,/Choose Check preview/);assert.equal(offered(panel,'Check preview'),true);
  p.clock.t=h.context.expires_at;h.api.showConnect({operation:'check'});
  assert.equal(h.api.snapshot().note,'Setup approval on your computer has ended, so this page cannot go on with this setup.');
  assert.equal(offered(panel,'Check preview'),false);h.api.dispose();
});
test('a refused check and an ended approval say this page cannot go on, and invite no retry',async()=>{
  for (const [answer,note] of [[fail(refused()),'Your computer refused this setup, so this page cannot go on with it.'],
      [()=>({expires_at:1800000000}),'Setup approval on your computer has ended, so this page cannot go on with this setup.']]) {
    const p=previewHarness({answers:[answer]}),{h,panel}=p;
    await p.start();
    assert.equal(h.api.snapshot().note,note);assert.doesNotMatch(note,/look again|try again|check it again/i);
    assert.equal(offered(panel,'Check preview'),false);assert.equal(offered(panel,'Preview team'),false);
    const hint=descendants(stepsOf(panel).find(n=>n.dataset.step==='approve')).find(n=>n.className==='setup-hint');
    assert.ok(hint);assert.equal(hint.textContent,'','no hint invites checking an ended approval again');
    assert.equal(p.live().length,0);h.api.dispose();
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
  assert.equal(snap.note,READY_BANNER);
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
  assert.ok(descendants(detail).includes(authority),'approval scope lives under Technical details');
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
const launchBodyOf=panel=>stepsOf(panel).find(n=>n.dataset.step==='launch').children.find(n=>n.className==='setup-step-body');
const drawerOf=panel=>panel.children.find(n=>n.tagName==='DETAILS');
// A shown line in the launch step itself, before the row that holds Launch team.
const aboveLaunch=(panel,text)=>{const order=launchBodyOf(panel).children;
  const row=order.findIndex(n=>descendants(n).some(c=>c.tagName==='BUTTON'&&c.textContent==='Launch team'));
  const at=order.findIndex(n=>n.textContent===text&&visible(n));return at>=0&&at<row;};
test('a ready Preview says the team is ready to launch and, above Launch team, who Launch starts; the counts sit under Technical details',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');
  const status=panel.children.find(n=>n.className==='setup-note'),drawer=drawerOf(panel),inside=new Set(descendants(drawer));
  assert.equal(h.api.snapshot().note,READY_BANNER);assert.equal(status.textContent,READY_BANNER);
  assert.ok(aboveLaunch(panel,LAUNCH_STARTS),'above Launch team, in the launch step');
  assert.ok(!descendants(drawer).some(n=>n.textContent===LAUNCH_STARTS),'outside the drawer');
  assert.equal(drawer.children[0].tagName,'SUMMARY');assert.equal(drawer.children[0].textContent,'Technical details');
  const counts=descendants(panel).filter(n=>/^Launching changes /.test(n.textContent));
  assert.deepEqual(counts.map(n=>[n.textContent,inside.has(n),visible(n)]),[['Launching changes 0 generated files and starts or restarts 1 bot.',true,true]],
    'the counts, once, under Technical details');
  assert.ok(!descendants(panel).some(n=>n.textContent.includes('Your team is ready.')),'the count sentence no longer opens with "Your team is ready."');
  assert.ok(descendants(drawer).some(n=>n.textContent.includes(`for app version ${h.pending.source_revision} only.`)),'the version code is in the drawer');
  assert.ok(!descendants(panel).some(n=>!inside.has(n)&&n.textContent.includes(h.pending.source_revision)),'and nowhere else');
  const raw=descendants(drawer).find(n=>n.tagName==='PRE');
  assert.deepEqual(JSON.parse(raw.textContent).effects,h.preview.effects,'so is the raw plan');
  assert.ok(!descendants(panel).some(n=>n.textContent===OTHERS_RESTART),'only the team\'s own bot starts, so no restart line');
  await h.api.run('launch');
  assert.ok(!descendants(panel).some(n=>visible(n)&&[LAUNCH_STARTS,READY_BANNER].includes(n.textContent)),'once Launch is sent, neither line stays');
  assert.equal(counts[0].hidden,true,'nor do the counts');
  h.api.dispose();
});
test('Bots already running here restart briefly shows above Launch team only when Launch restarts bots outside the team',async()=>{
  // Launch restarts every bot its plan names; this team's own are named shop/<bot>.
  for(const [restart_bots,shown] of [[['shop/lead','shop/engineer','shop/reviewer'],false],[[],false],
      [['shop/lead','other/lead'],true],[['other/lead'],true],[['shopping/lead'],true],[undefined,true]]){
    const name=JSON.stringify(restart_bots);
    const h=setupHarness({client:{async setupPreview(input){h.calls.push(['preview',input]);return {...h.preview,effects:{...h.preview.effects,restart_bots}};}}});
    const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
    await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');
    assert.ok(aboveLaunch(panel,LAUNCH_STARTS),name);
    assert.equal(aboveLaunch(panel,OTHERS_RESTART),shown,name);
    assert.equal(descendants(panel).some(n=>n.textContent===OTHERS_RESTART&&visible(n)),shown,name+': above Launch team or nowhere');
    await h.api.run('launch');
    assert.ok(!descendants(panel).some(n=>n.textContent===OTHERS_RESTART&&visible(n)),name+': gone once Launch is sent');
    h.api.dispose();
  }
  // The same for a Preview the computer kept, shown again by the approval read.
  const kept=setupHarness();signedIn(kept);
  Object.assign(kept.context,{request:{...kept.pending},preview_state:'ready',
    preview:{...kept.preview,effects:{...kept.preview.effects,restart_bots:['shop/lead','other/lead']},goal:'Improve tests'}});
  const ui=dom(),panel=new ui.Element();kept.api.mountPanel({document:ui.document,element:panel});
  await kept.api.run('context');
  assert.equal(kept.api.snapshot().note,READY_BANNER);assert.ok(aboveLaunch(panel,OTHERS_RESTART));kept.api.dispose();
});
test('the ready banner goes once the Preview runs out, where Launch team says it expired, and nothing is sent',async()=>{
  const clock={t:1800000000},h=setupHarness({setupOptions:{now:()=>clock.t}}),ui=dom(),panel=new ui.Element();
  h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');
  const status=panel.children.find(n=>n.className==='setup-note'),hint=launchBodyOf(panel).children.find(n=>n.className==='setup-hint');
  const expired='This preview expired. Inspect the update on the computer.';
  assert.equal(status.textContent,READY_BANNER);assert.equal(hint.textContent,'');
  clock.t=h.preview.expires_at-1;h.change({});
  assert.equal(status.textContent,READY_BANNER,'still ready inside its window');
  clock.t=h.preview.expires_at;h.change({}); // any repaint once the window has passed
  assert.equal(hint.textContent,expired);
  assert.equal(h.api.snapshot().note,'');assert.doesNotMatch(status.textContent,/ready/i,'no "ready" beside "expired"');
  assert.equal(buttonOf(panel,'Launch team').disabled,true);
  assert.equal(await h.api.run('launch'),false);assert.equal(h.calls.filter(c=>c[0]==='launch').length,0,'nothing is sent');
  assert.doesNotMatch(status.textContent,/ready/i);assert.equal(hint.textContent,expired);
  h.api.dispose();
  // With no repaint first, the Launch that finds the Preview ended takes the banner away the same way.
  const late={t:1800000000},l=setupHarness({setupOptions:{now:()=>late.t}});
  await l.api.run('context');l.api.setGoal('Improve tests');await l.api.run('preview');
  assert.equal(l.api.snapshot().note,READY_BANNER);
  late.t=l.preview.expires_at+60;assert.equal(await l.api.run('launch'),false);
  assert.equal(l.api.snapshot().note,'');assert.equal(l.calls.filter(c=>c[0]==='launch').length,0);l.api.dispose();
});
test('the ready banner goes once the page no longer holds the Preview: a connection change, one during Launch, and its end with no read',async()=>{
  const ready=async options=>{const h=setupHarness(options),ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
    await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');
    const status=panel.children.find(n=>n.className==='setup-note');assert.equal(status.textContent,READY_BANNER);return {h,panel,status};};
  // A connection change drops the plan and with it Launch team; the banner goes too.
  for(const next of [{epoch:2},{epoch:2,state:'unavailable'}]){
    const name=JSON.stringify(next),{h,panel,status}=await ready();
    h.change(next);
    assert.equal(h.api.snapshot().plan,null,name);assert.equal(offered(panel,'Launch team'),false,name);
    assert.equal(h.api.snapshot().note,'',name);assert.doesNotMatch(status.textContent,/ready/i,name);
    if(next.state)assert.equal(status.textContent,'Connect this computer to continue setup.','the connection line stands alone');
    h.api.dispose();
  }
  // The same while the one Launch request is in flight; its late answer is not used and nothing is sent again.
  const held=deferred(),f=await ready({client:{async setupLaunch(input){f.h.calls.push(['launch',input]);return held.promise;}}});
  const sending=f.h.api.run('launch');await flush();
  assert.deepEqual(f.h.calls.filter(c=>c[0]==='launch'),[['launch',f.h.pending]]);assert.equal(f.status.textContent,READY_BANNER);
  f.h.change({epoch:2});
  assert.equal(f.h.api.snapshot().note,'');assert.doesNotMatch(f.status.textContent,/ready/i);
  held.resolve({...f.h.pending,version:2,state:'quiesce',launch_reserved:true,config_committed:false,health_errors:{}});
  assert.equal(await sending,false,'an answer for the earlier connection is not used');
  assert.doesNotMatch(f.status.textContent,/ready/i);assert.equal(f.h.calls.filter(c=>c[0]==='launch').length,1);
  f.h.api.dispose();
  // Past the Preview's end, a repaint with no read takes it away.
  const clock={t:1800000000},e=await ready({setupOptions:{now:()=>clock.t}}),reads=e.h.calls.filter(c=>c[0]==='context').length;
  clock.t=e.h.preview.expires_at+1;e.h.change({});
  assert.equal(e.h.api.snapshot().note,'');assert.doesNotMatch(e.status.textContent,/ready/i);
  assert.equal(e.h.calls.filter(c=>c[0]==='context').length,reads,'with no read');
  e.h.api.dispose();
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
test('a null request under the same approval never clears the original request or permits another preview',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  await h.api.run('context');assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.match(h.api.snapshot().note,/^Setup was already submitted from this browser\./);
  assert.equal(await h.api.run('preview'),false);assert.equal(await h.api.run('launch'),false);
  assert.equal(h.calls.filter(c=>c[0]==='preview').length,1);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
});
// The computer cannot hold a new approval while it keeps a request (OwnerSetup.allow refuses it), so a new approval
// holding no request means it no longer has the original: it is set aside with the conflicting ones, never launched.
test('a new approval holding no request sets the original request aside and never launches it again',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  assert.equal(h.api.following(),true);
  h.context.generation=id(12);await h.api.run('context');
  assert.equal(h.api.snapshot().pending,null);assert.deepEqual(h.api.snapshot().conflicts,[h.pending]);
  assert.deepEqual(JSON.parse(h.saved.get('test')),{version:2,pending:null,conflicts:[h.pending],legacy:[]});
  assert.equal(h.api.following(),false,'the request set aside is no longer followed');
  assert.equal(await h.api.run('launch'),false);
  const next={...h.pending,generation:id(12),request_id:id(13)};
  h.client.setupPreview=async input=>{h.calls.push(['preview',input]);return {...h.preview,...next};};
  h.client.setupLaunch=async input=>{h.calls.push(['launch',input]);throw new Error('lost');};
  assert.equal(await h.api.run('preview'),true);
  assert.deepEqual(h.calls.filter(c=>c[0]==='preview').at(-1)[1],{generation:id(12),goal:'Improve tests'});
  assert.equal(await h.api.run('launch'),false);
  assert.deepEqual(h.calls.filter(c=>c[0]==='launch').map(c=>c[1]),[h.pending,next],'each request is launched once');
  const reloaded=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.deepEqual(reloaded.snapshot().pending,next);assert.deepEqual(reloaded.snapshot().conflicts,[h.pending]);
  reloaded.dispose();h.api.dispose();
});
test('historical sealed identifiers remain visible and block a replacement Git request',async()=>{
  const h=setupHarness(),legacy={generation:id(9),plan_id:'p-'+'a'.repeat(64),request_id:id(10)};
  h.saved.set('test',JSON.stringify(legacy));
  const restored=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  const ui=dom(),panel=new ui.Element();restored.mountPanel({document:ui.document,element:panel});
  assert.deepEqual(restored.snapshot().legacy,[legacy]);assert.equal(restored.snapshot().pending,null);
  await restored.run('context');restored.setGoal('Improve tests');assert.equal(await restored.run('preview'),false);
  assert.ok(descendants(panel).some(e=>/identifiers from an older setup, so it cannot launch/.test(e.textContent)));
  assert.ok(descendants(panel).some(e=>/they are listed under Technical details\./.test(e.textContent)),'it names the drawer by its label');
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
  assert.match(h.api.snapshot().note,/using the identifiers under Technical details\.$/,'it names the drawer by its label');
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
    'the team\'s internal name stays under Technical details');
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
  assert.ok(descendants(panel.children.find(n=>n.tagName==='DETAILS')).includes(recheck),'re-checking a held approval sits under Technical details');
  assert.ok(!descendants(panel).some(n=>n.className==='setup-step-done'&&descendants(n).some(c=>c.tagName==='BUTTON')),'"Setup approved" stands alone');
  h.api.setGoal('Ship the signup form');await h.api.run('preview');
  assert.equal(recheck.hidden,true,'re-checking approval would retire a fresh preview, so it is not offered while launchable');
  assert.deepEqual(shape().map(r=>r[1]),['done','done','done','current']);
  assert.ok(says('Goal: Ship the signup form'));
  assert.deepEqual(filled().launch,['Launch team']);
  assert.ok(says(LAUNCH_STARTS));assert.ok(says('Launching changes 0 generated files and starts or restarts 1 bot.'));
  await h.api.run('launch');
  assert.deepEqual(filled().launch,['Check launch status'],'after one launch only its status check remains');
  assert.equal(recheck.hidden,false,'once the launch is spent, approval can be checked again');
  assert.ok(shown().every(n=>filledIn(n).length<=1));
  const details=panel.children.find(n=>n.tagName==='DETAILS');
  assert.equal(details.children[0].textContent,'Technical details');
  assert.ok(descendants(details).some(n=>n.textContent.includes('This approval covers')),'approval scope and identifiers sit under Technical details');
  h.api.dispose();
  const reopened=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000,setTimeout:()=>0,clearTimeout:()=>{}});
  const ui=dom(),again=new ui.Element();reopened.mountPanel({document:ui.document,element:again});
  const shownAgain=stepsOf(again).filter(visible);
  assert.deepEqual(shownAgain.map(n=>[n.dataset.step,n.dataset.state]),[['approve','current'],['goal','done'],['launch','current']]);
  // Until an approval is read, Check setup approval leads: that read is what shows a computer set up again.
  assert.deepEqual(shownAgain.map(filledIn),[['Check setup approval'],[],[]],'a reopened launch leads with the approval check');
  assert.equal(descendants(again).find(n=>n.tagName==='BUTTON'&&n.textContent==='Check setup approval again').hidden,true,'with approval not yet read, only the step\'s own check is offered');
  assert.equal(await reopened.run('context'),true);
  assert.deepEqual(stepsOf(again).filter(visible).map(filledIn).at(-1),['Check launch status'],'once read, the launch\'s status check leads');
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
  assert.match(text,/guided setup/);assert.doesNotMatch(text,/does not allow messages/,'with the marked team card, setup does allow messages');
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
    removeAttribute(k){delete this.attributes[k];},
    getBoundingClientRect(){return {height:this.height};}};
  const posts=[],observers=[];
  class ResizeObserver {constructor(fn){this.fn=fn;this.targets=[];observers.push(this);}observe(target){this.targets.push(target);}disconnect(){this.closed=true;}}
  const listeners=new Map(),view={parent:{postMessage:(...args)=>posts.push(args)},ResizeObserver,listeners,
    addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
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
function connectedPanel({saved=[],context={},client={},storage,timers}={}) {
  const h=setupHarness({saved,client,storage,setupOptions:timers?{setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}}
    :{setTimeout:()=>0,clearTimeout:()=>{}}});
  Object.assign(h.context,typeof context==='function'?context(h):context);h.change({state:'not_connected',epoch:2});
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
  const form=[{type:'claudfather.workspace.form',version:1,form:'setup'},documentContext.shell_origin];
  assert.deepEqual(frame.posts,[ready,form],'the ready message comes first, then the form; no size before a measurement');
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
  assert.deepEqual(frame.posts,[[{type:'claudfather.workspace.ready',version:1},documentContext.shell_origin],
    [{type:'claudfather.workspace.form',version:1,form:'setup'},documentContext.shell_origin]]);
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
// A panel that sends one managed Launch. launch: the computer's answer to it (an Error is thrown); receipts: the
// answers to the launch status reads in order (an Error is thrown); check: what the hand-over's connection check does.
function managedLaunch({launch,receipts=[],check,standalone=true,storage}={}) {
  const timers=[],answer=value=>{if(value instanceof Error)throw value;return value;};let h=null;
  h=setupHarness({storage,setupOptions:{setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}},
    client:{...(launch?{async setupLaunch(input){h.calls.push(['launch',input]);return answer(launch(input));}}:{}),
      async setupReceipt(input){h.calls.push(['receipt',input]);
        return {...input,version:2,state:'quiesce',managed_owner:true,launch_reserved:true,config_committed:false,health_errors:{},...answer(receipts.shift())};},
      async check(){await check?.(h);}}});
  h.context.managed_owner=true;
  const ui=dom(),panel=new ui.Element();
  h.api.mountPanel({document:ui.document,element:panel,...(standalone?{standalone:true,connect:()=>{}}:{})});
  return {h,panel,timers,lede:()=>descendants(panel).find(n=>n.className==='setup-lede'),
    async start(){await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');await h.api.run('launch');},
    async next(){await timers.shift()();await flush();}};
}
const reserved=input=>({...input,version:2,state:'quiesce',managed_owner:true,launch_reserved:true,config_committed:false,health_errors:{}});
// As the connected read client when the session turns readable: setup_only, checking, then ready (a new epoch).
const promote=(h,seen=()=>{})=>{h.change({state:'checking'});seen();h.change({state:'ready',epoch:2});};
const stepButtons=panel=>stepsOf(panel).flatMap(descendants).filter(n=>n.tagName==='BUTTON'&&visible(n)&&!n.disabled).map(n=>n.textContent);
test('while a managed launch hands over, no finished step loses its tick or disappears and nothing offers approval again',async()=>{
  let whileChecking=null;
  const l=managedLaunch({receipts:[{},{state:'publish'},{state:'publish'},{state:'done',config_committed:true}],
    check:h=>promote(h,()=>{whileChecking=stepShape(l.panel);})});
  signedIn(l.h);await l.start();
  const during=[['connect','done'],['approve','done'],['signin','done'],['goal','done'],['launch','current']];
  assert.deepEqual(stepShape(l.panel),during);
  for(let read=1;read<=3;read++){
    await l.next();
    assert.equal(l.h.api.snapshot().context,null,'the hand-over moved the connection on and cleared the approval context');
    assert.deepEqual(stepShape(l.panel),during,'read '+read);
    assert.ok(descendants(stepsOf(l.panel)[1]).some(n=>n.textContent==='Setup approved'&&visible(n)),'read '+read);
    assert.deepEqual(stepButtons(l.panel),['Check launch status'],'no step offers approving again, read '+read);
    assert.equal(stepPart(l.panel,'launch','setup-progress').textContent,'Checking automatically while your team connects…');
  }
  assert.deepEqual(whileChecking,during,'the hand-over\'s own connection check keeps Connect done');
  assert.equal(l.h.api.snapshot().note,'Still in progress. Checking status never repeats or resumes setup.');
  await l.next();
  assert.deepEqual(stepShape(l.panel),during.map(([name])=>[name,'done']));
  assert.equal(l.h.api.snapshot().note,'Team launched. Your team is running on this computer.');
  assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
});
test('a person\'s own connection check beside a held launch shows Connect not done, with its checking line',async()=>{
  for(const lost of ['unavailable','denied']){
    const l=managedLaunch({launch:reserved});await l.start();
    l.h.change({state:lost,epoch:3});l.h.api.showConnect({operation:'check',busy:'check'});l.h.change({state:'checking'});
    assert.deepEqual(stepShape(l.panel)[0],['connect','current'],lost);
    assert.equal(stepPart(l.panel,'connect','setup-progress').textContent,'Checking connection…',lost);
    assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
  }
});
test('a hand-over that stops short of a finished launch lets go of the held steps, so approval can be checked again',async()=>{
  // Health errors after the connection check cleared the approval context; then a refused status check and a denial.
  const l=managedLaunch({receipts:[{state:'publish',health_errors:{'example.service':'failed'}}],check:h=>promote(h)});
  await l.start();await l.next();
  assert.equal(l.h.api.snapshot().context,null);
  assert.deepEqual(stepShape(l.panel)[1],['approve','current']);
  assert.ok(stepButtons(l.panel).includes('Check setup approval'));
  l.h.client.setupReceipt=async()=>{throw Object.assign(new Error('refused'),{status:403});};
  await l.h.api.run('receipt');l.h.change({state:'denied',epoch:3});l.h.change({state:'setup_only',epoch:4});
  assert.deepEqual(stepShape(l.panel)[1],['approve','current']);
  assert.equal(descendants(stepsOf(l.panel)[1]).some(n=>n.textContent==='Setup approved'&&visible(n)),false);
  assert.ok(stepButtons(l.panel).includes('Check setup approval'),'the approval can be checked again');
  l.h.api.dispose();
  // A refused hand-over read leaves the approval context: Check setup approval again is offered under Technical details.
  const refused=managedLaunch({receipts:[Object.assign(new Error('refused'),{status:403})]});
  await refused.start();await refused.next();
  assert.equal(offered(refused.panel,'Check setup approval again'),true);
  refused.h.api.dispose();
});
test('a denied or signed-out connection lets go of a held launch, so approval is checked again after reconnecting',async()=>{
  for(const [lost,held] of [['unavailable',true],['denied',false],['signed_out',false]]){
    const l=managedLaunch({launch:input=>({...reserved(input),managed_owner:undefined})});
    l.h.context.managed_owner=undefined;await l.start();
    l.h.change({state:lost,epoch:3});l.h.change({state:'setup_only',epoch:4});
    assert.deepEqual(stepShape(l.panel)[1],['approve',held?'done':'current'],lost);
    assert.equal(stepButtons(l.panel).includes('Check setup approval'),!held,lost);
    assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
  }
});
test('a launch status that says the launch is no longer reserved lets go of the held steps',async()=>{
  const l=managedLaunch({launch:input=>({...reserved(input),managed_owner:undefined})});
  l.h.context.managed_owner=undefined;await l.start();
  l.h.change({state:'unavailable',epoch:3});l.h.change({state:'setup_only',epoch:4});
  assert.deepEqual(stepShape(l.panel)[1],['approve','done'],'held while the computer holds the launch');
  assert.equal(visible(l.lede()),false);
  l.h.client.setupReceipt=async input=>({...input,version:2,state:'quiesce',launch_reserved:false,config_committed:false,health_errors:{}});
  await l.h.api.run('receipt');
  assert.match(l.h.api.snapshot().note,/^Prepared but not launched\./);
  assert.deepEqual(stepShape(l.panel)[1],['approve','current']);assert.ok(stepButtons(l.panel).includes('Check setup approval'));
  assert.equal(visible(l.lede()),false,'this request was reserved once, so the line that nothing runs yet stays gone');
  // Only once the computer offers this request's Preview again, which it keeps only while nothing was dispatched, is the line true again.
  Object.assign(l.h.context,said.ready(l.h),{launch_reserved:false});
  assert.equal(await l.h.api.run('context'),true);
  assert.equal(offered(l.panel,'Launch team'),true);assert.equal(visible(l.lede()),true);
  assert.equal(l.h.saved.get('test:launch'),'null','a page opened again does not keep the old word either');
  assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
});
test('an approval read that says the held request is no longer reserved lets go of the held steps',async()=>{
  const l=managedLaunch({launch:input=>({...reserved(input),managed_owner:undefined})});
  l.h.context.managed_owner=undefined;await l.start();
  l.h.change({state:'unavailable',epoch:3});l.h.change({state:'setup_only',epoch:4});
  assert.deepEqual(stepShape(l.panel)[1],['approve','done'],'held while the computer holds the launch');
  Object.assign(l.h.context,{request:{...l.h.pending},launch_reserved:false});
  assert.equal(await l.h.api.run('context'),true);
  l.h.change({state:'unavailable',epoch:5});l.h.change({state:'setup_only',epoch:6});
  assert.deepEqual(stepShape(l.panel)[1],['approve','current'],'nothing holds the approval once the computer said so');
  assert.ok(stepButtons(l.panel).includes('Check setup approval'));
  assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
});
test('after the pause, a refused Check setup approval lets go of the held steps and a lost answer keeps them',async()=>{
  for(const [name,error,held] of [['refused',Object.assign(new Error('refused'),{status:403}),false],['lost',new Error('lost'),true]]){
    const l=managedLaunch({check:h=>promote(h)});signedIn(l.h);await l.start();
    while(l.timers.length)await l.next();
    assert.match(l.h.api.snapshot().note,/^Automatic checks paused\./,name);
    l.h.client.setupContext=async()=>{throw error;};
    buttonOf(l.panel,'Check setup approval again').click();await flush();
    assert.equal(l.h.api.snapshot().context,null,name);
    assert.deepEqual(stepShape(l.panel)[1],['approve',held?'done':'current'],name);
    assert.equal(descendants(stepsOf(l.panel)[1]).some(n=>n.textContent==='Setup approved'&&visible(n)),held,name);
    assert.equal(stepShape(l.panel).some(([step])=>step==='signin'),held,name);
    assert.equal(stepButtons(l.panel).includes('Check setup approval'),!held,name);
    assert.equal(visible(l.lede()),false,'the line that nothing runs yet stays gone, '+name);
    assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
  }
});
test('a hand-over connection check that throws does not leave Connect held done',async()=>{
  const l=managedLaunch({check:h=>{h.change({state:'checking'});throw new Error('lost');}});await l.start();
  await l.next();
  assert.equal(l.h.client.snapshot().state,'checking');
  assert.deepEqual(stepShape(l.panel)[0],['connect','current'],'only the hand-over\'s own check in flight keeps Connect done');
  assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
});
test('the Launch team line stays gone for a request the computer reserved, whatever stops the hand-over, and returns for a new request',async()=>{
  const refused=()=>Object.assign(new Error('refused'),{status:403});
  for(const [name,options,after] of [
    ['health errors',{receipts:[{state:'publish',health_errors:{'example.service':'failed'}}],check:h=>promote(h)},async()=>{}],
    ['a refused status read',{receipts:[{},refused()]},async l=>{await l.next();}],
    ['denied',{receipts:[{}]},async l=>{l.h.change({state:'denied',epoch:3});}],
    ['the pause',{},async l=>{while(l.timers.length)await l.next();assert.match(l.h.api.snapshot().note,/^Automatic checks paused\./);}]]){
    const l=managedLaunch(options);await l.start();await l.next();await after(l);
    assert.equal(visible(l.lede()),false,name);
    if(name==='health errors'){
      Object.assign(l.h.context,{request:{...l.h.pending,request_id:id(30)},launch_reserved:false});
      assert.equal(await l.h.api.run('context'),true);
      assert.equal(visible(l.lede()),true,'a different request has launched nothing');
    }
    assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1,name);l.h.api.dispose();
  }
});
test('a slow but healthy hand-over that reaches its pause keeps every finished step, and the approval and launch status can still be checked',async()=>{
  const l=managedLaunch({check:h=>promote(h)});signedIn(l.h);await l.start();
  const during=[['connect','done'],['approve','done'],['signin','done'],['goal','done'],['launch','current']];
  while(l.timers.length)await l.next();
  assert.equal(l.h.calls.filter(c=>c[0]==='receipt').length,80,'every read said the launch is still reserved');
  assert.equal(l.h.api.snapshot().note,'Automatic checks paused. Choose Check launch status to look again. Do not launch again.');
  assert.deepEqual(stepShape(l.panel),during);
  assert.equal(offered(l.panel,'Check setup approval again'),true);
  l.h.client.setupReceipt=async input=>({...input,version:2,state:'done',managed_owner:true,launch_reserved:true,config_committed:true,health_errors:{}});
  buttonOf(l.panel,'Check launch status').click();await flush();
  assert.equal(l.h.api.snapshot().note,'Team launched. Your team is running on this computer.');
  assert.deepEqual(stepShape(l.panel),during.map(([name])=>[name,'done']));
  assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
});
test('after the pause, a refused Check launch status lets go of the held steps and a lost answer keeps them',async()=>{
  for(const [name,error,held] of [['refused',Object.assign(new Error('refused'),{status:409}),false],['lost',new Error('lost'),true]]){
    const l=managedLaunch({check:h=>promote(h)});signedIn(l.h);await l.start();
    while(l.timers.length)await l.next();
    assert.match(l.h.api.snapshot().note,/^Automatic checks paused\./,name);
    l.h.client.setupReceipt=async()=>{throw error;};
    buttonOf(l.panel,'Check launch status').click();await flush();
    assert.deepEqual(stepShape(l.panel)[1],['approve',held?'done':'current'],name);
    assert.equal(stepShape(l.panel).some(([step])=>step==='signin'),held,name);
    assert.equal(stepButtons(l.panel).includes('Check setup approval'),!held,name);
    assert.equal(visible(l.lede()),false,'the line that nothing runs yet stays gone, '+name);
    assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
  }
});
test('a managed Launch the computer refused holds nothing, even while its status reads get no answer',async()=>{
  const l=managedLaunch({launch:()=>Object.assign(new Error('refused'),{status:409}),receipts:[new Error('lost'),new Error('lost')]});
  await l.start();
  assert.equal(visible(l.lede()),true,'nothing was reserved');
  await l.next();
  assert.equal(l.h.api.snapshot().note,'Starting your team’s connection. The launch is not repeated.');
  l.h.change({state:'setup_only',epoch:3});
  assert.equal(visible(l.lede()),true);
  assert.deepEqual(stepShape(l.panel)[1],['approve','current']);assert.ok(stepButtons(l.panel).includes('Check setup approval'));
  assert.equal(l.h.calls.filter(c=>c[0]==='launch').length,1);l.h.api.dispose();
});
// This browser after a page whose Launch the computer said was reserved: the request, and the computer's word on it.
const keptLaunch=()=>[['test',JSON.stringify({version:2,pending:settingUp,conflicts:[],legacy:[]})],['test:launch',JSON.stringify(settingUp)]];
const reopenedLede=p=>descendants(p.panel).find(n=>n.className==='setup-lede');
test('a page whose Launch the computer said was reserved keeps that word in this browser, and a page opened again hides the Launch team line before Connect',async()=>{
  const l=managedLaunch({launch:input=>({...reserved(input),managed_owner:undefined})});l.h.context.managed_owner=undefined;
  await l.start();
  assert.deepEqual(JSON.parse(l.h.saved.get('test:launch')),l.h.pending,'kept beside the request');
  l.h.api.dispose();
  const p=connectedPanel({saved:[...l.h.saved]});
  assert.equal(visible(reopenedLede(p)),false,'the page opened again knows a Launch was reserved before it connects');
  assert.deepEqual(stepShape(p.panel),[['connect','current'],['approve','upcoming'],['goal','done'],['launch','upcoming']]);
  p.h.api.dispose();
  // A request kept from a Preview alone says nothing about a launch: the line shows, as before.
  const preview=connectedPanel({saved:[keptLaunch()[0]]});
  assert.equal(visible(reopenedLede(preview)),true);preview.h.api.dispose();
});
test('a page opened again after its Launch reads that launch\'s status once after Connect and lands on the true state, never launching',async()=>{
  const refused=Object.assign(new Error('refused'),{status:403});
  for(const [name,state,context,answer,note,last] of [
    ['finished, readable session','ready',{},null,'Team launched. Your team is running on this computer.','done'],
    ['finished, setup session','setup_only',{request:settingUp,launch_reserved:true},null,'Team launched. Your team is running on this computer.','done'],
    ['still starting','ready',{},{state:'publish',launch_reserved:true,config_committed:false},'Still in progress. Checking status never repeats or resumes setup.','current'],
    ['refused','ready',{},refused,'','current']]){
    const client=answer?{async setupReceipt(input){p.h.calls.push(['receipt',input]);if(answer instanceof Error)throw answer;
      return {...input,version:2,health_errors:{},...answer};}}:{};
    const p=connectedPanel({saved:keptLaunch(),context,client});
    await p.finish(state);await flush();
    assert.deepEqual(p.h.calls.filter(c=>c[0]==='receipt'),[['receipt',settingUp]],'one status read of the kept request: '+name);
    assert.equal(p.h.calls.filter(c=>['launch','preview'].includes(c[0])).length,0,'nothing is sent: '+name);
    assert.equal(descendants(p.panel).find(n=>n.className==='setup-note').textContent,note,name);
    assert.equal(stepShape(p.panel).at(-1)[1],last,name);
    if(last==='done')assert.deepEqual(stepShape(p.panel),[['connect','done'],['approve','done'],['goal','done'],['launch','done']],name);
    assert.equal(visible(reopenedLede(p)),false,name);
    p.h.api.dispose();
  }
  // A refused read (an approval that ended days ago refuses it) shows nothing, offers the check, and is never retried.
  const timers=[],refusedPage=connectedPanel({saved:keptLaunch(),timers,
    client:{async setupReceipt(input){refusedPage.h.calls.push(['receipt',input]);throw refused;}}});
  await refusedPage.finish('ready');await flush();
  for(let round=0;round<5&&timers.length;round++){for(const fn of timers.splice(0))await fn();await flush();}
  assert.equal(refusedPage.h.calls.filter(c=>c[0]==='receipt').length,1,'not retried');
  assert.equal(refusedPage.h.api.snapshot().note,'');
  assert.ok(stepButtons(refusedPage.panel).includes('Check launch status'));refusedPage.h.api.dispose();
  // Without the computer's word on a launch, Connect reads no launch status.
  const preview=connectedPanel({saved:[keptLaunch()[0]]});await preview.finish('ready');
  assert.equal(preview.h.calls.filter(c=>c[0]==='receipt').length,0);preview.h.api.dispose();
});
// The view after Connect of a page that kept the request and nothing about a launch.
const viewOf=p=>({steps:stepShape(p.panel),note:p.h.api.snapshot().note,lede:visible(reopenedLede(p)),
  receipts:p.h.calls.filter(c=>c[0]==='receipt').length});
test('a stored word on a launch that this page cannot trust, or storage that fails, reads no launch status and changes nothing',async()=>{
  const request=keptLaunch()[0],base=connectedPanel({saved:[request]});await base.finish('ready');await flush();
  const before=viewOf(base);base.h.api.dispose();
  assert.equal(before.receipts,0);assert.equal(before.lede,true);
  for(const [name,options] of [
    ['a word for another request',{saved:[request,['test:launch',JSON.stringify({...settingUp,request_id:id(11)})]]}],
    ['an unreadable word',{saved:[request,['test:launch','not json']]}],
    ['a forged word',{saved:[request,['test:launch',JSON.stringify({generation:'x'})]]}],
    ['a browser that refuses to read it',{saved:[...keptLaunch()],
      storage:saved=>({getItem:key=>{if(key==='test:launch')throw new Error('denied');return saved.get(key)??null;}})}]]){
    const p=connectedPanel(options);
    assert.equal(visible(reopenedLede(p)),true,name);
    await p.finish('ready');await flush();
    assert.deepEqual(viewOf(p),before,name);p.h.api.dispose();
  }
  // A browser that refuses to keep the word: the Launch is followed and shown as when it keeps it.
  const shown=async storage=>{
    const l=managedLaunch({launch:input=>({...reserved(input),managed_owner:undefined}),storage});l.h.context.managed_owner=undefined;
    await l.start();
    const view={steps:stepShape(l.panel),note:l.h.api.snapshot().note,lede:visible(l.lede()),kept:l.h.saved.has('test:launch'),
      launches:l.h.calls.filter(c=>c[0]==='launch').length};
    l.h.api.dispose();return view;
  };
  const keeps=await shown(),refuses=await shown(saved=>({setItem:(key,value)=>{if(key==='test:launch')throw new Error('full');saved.set(key,value);}}));
  assert.deepEqual({...refuses,kept:true},keeps);assert.equal(refuses.kept,false);
  assert.equal(keeps.lede,false);assert.equal(keeps.launches,1);
});
test('a page opened again reads no launch status while the hand-over is reading, or beside a Preview it can launch',async()=>{
  const handoff=connectedPanel({saved:keptLaunch(),context:{request:settingUp,launch_reserved:true,managed_owner:true}});
  await handoff.finish();await flush();
  assert.equal(handoff.reads(),1);
  assert.equal(handoff.h.calls.filter(c=>c[0]==='receipt').length,0,'the hand-over reads on its own schedule');
  handoff.h.api.dispose();
  const kept=connectedPanel({saved:keptLaunch(),context:h=>({...said.ready(h),launch_reserved:false})});
  await kept.finish();await flush();
  assert.equal(kept.reads(),1);
  assert.equal(kept.h.calls.filter(c=>c[0]==='receipt').length,0,'a Preview the computer kept: nothing was dispatched');
  assert.ok(stepButtons(kept.panel).includes('Launch team'));
  assert.equal(kept.h.saved.get('test:launch'),'null');kept.h.api.dispose();
});
test('a reopened page whose approval read says a launch is held hides the Launch team line',async()=>{
  const p=connectedPanel({saved:[['test',JSON.stringify({version:2,pending:settingUp,conflicts:[],legacy:[]})]],
    context:{request:settingUp,launch_reserved:true}});
  const lede=descendants(p.panel).find(n=>n.className==='setup-lede');
  assert.equal(visible(lede),true);
  await p.finish();assert.equal(p.reads(),1);
  assert.equal(visible(lede),false);
  assert.equal(p.h.calls.filter(c=>['launch','receipt'].includes(c[0])).length,0,'only the approval read');p.h.api.dispose();
});
test('a held launch keeps no Claude sign-in from another approval',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element();signedIn(h);
  h.api.mountPanel({document:ui.document,element:panel,standalone:true,connect:()=>{}});
  await h.api.run('context');
  // A new approval holds a launch, and its read names no sign-in: the connected one belongs to the old approval.
  const next={generation:id(20),request_id:id(21),config_fingerprint:'c'.repeat(64),source_revision:h.pending.source_revision};
  Object.assign(h.context,{generation:id(20),request:next,launch_reserved:true});delete h.context.signin_attempt;
  assert.equal(await h.api.run('context'),true);
  h.change({state:'setup_only',epoch:3});
  assert.equal(h.api.snapshot().context,null);
  assert.deepEqual(stepShape(panel).map(row=>row[0]),['connect','approve','goal','launch'],'no Sign in to Claude step is shown done');
  assert.deepEqual(stepShape(panel)[1],['approve','done']);
  h.api.dispose();
});
// 41-01: a computer set up again from the same browser after a reset. This browser still keeps its earlier
// installation's request, the computer's word that that Launch was reserved, and its Claude sign-in.
const earlierInstallation=()=>[...keptLaunch(),['test:claude',JSON.stringify({generation:settingUp.generation,attempt_id:id(11)})]];
test('records kept from a computer\'s earlier installation do not block Sign in, Preview or Launch on its new one',async()=>{
  for(const [name,saved] of [['request, launch and sign-in kept',earlierInstallation()],['sign-in kept',earlierInstallation().slice(-1)]]){
    const next={generation:id(20),request_id:id(21),config_fingerprint:'c'.repeat(64),source_revision:settingUp.source_revision};
    const p=connectedPanel({saved,context:{generation:next.generation,provider_signin:true,signin_attempt:null,preview_state:'none'},
      client:{async setupSignin(action,input){p.h.calls.push(['signin-'+action,input]);return {...input,state:'connected',reason:null};},
        async setupPreview(input){p.h.calls.push(['preview',input]);return {...p.h.preview,...next};},
        async setupLaunch(input){p.h.calls.push(['launch',input]);
          return {...input,version:2,state:'quiesce',launch_reserved:true,config_committed:false,health_errors:{}};},
        async setupReceipt(input){p.h.calls.push(['receipt',input]);throw new Error('lost');}}});
    await p.finish();await flush();
    assert.equal(p.reads(),1,name);
    assert.equal(p.h.api.snapshot().note,'',name);
    assert.equal(p.h.api.snapshot().pending,null,name);
    assert.deepEqual(p.h.api.snapshot().conflicts,saved.length>1?[settingUp]:[],'the earlier request stays under Technical details: '+name);
    assert.deepEqual(stepShape(p.panel),[['connect','done'],['approve','done'],['signin','current'],['goal','upcoming'],['launch','upcoming']],name);
    assert.equal(p.focused()?.textContent,'Sign in to Claude',name);
    buttonOf(p.panel,'Sign in to Claude').click();await flush();
    const begun=p.h.calls.filter(c=>c[0]==='signin-begin');
    assert.equal(begun.length,1,name);assert.equal(begun[0][1].generation,next.generation,name);
    assert.deepEqual(JSON.parse(p.h.saved.get('test:claude')),begun[0][1],'the new sign-in replaces the kept one: '+name);
    p.h.api.setGoal('Improve tests');assert.equal(offered(p.panel,'Preview team'),true,name);
    buttonOf(p.panel,'Preview team').click();await flush();
    assert.deepEqual(p.h.calls.filter(c=>c[0]==='preview').map(c=>c[1]),[{generation:next.generation,goal:'Improve tests'}],name);
    assert.equal(offered(p.panel,'Launch team'),true,name);
    buttonOf(p.panel,'Launch team').click();await flush();
    assert.deepEqual(p.h.calls.filter(c=>c[0]==='launch').map(c=>c[1]),[next],name);
    assert.equal(p.h.calls.filter(c=>c[0]==='receipt').length,0,'the earlier request\'s status is never read: '+name);
    assert.ok(p.h.calls.every(([,input])=>input?.request_id!==settingUp.request_id),name);
    assert.deepEqual(JSON.parse(p.h.saved.get('test')),{version:2,pending:next,conflicts:saved.length>1?[settingUp]:[],legacy:[]},name);
    const reloaded=createGuidedSetup({client:p.h.client,storage:p.h.storage,storageKey:'test',now:()=>1800000000});
    assert.deepEqual(reloaded.snapshot().pending,next,name);assert.equal(await reloaded.run('launch'),false,name);
    assert.equal(p.h.calls.filter(c=>c[0]==='launch').length,1,name);
    reloaded.dispose();p.h.api.dispose();
  }
});
test('on the same installation, a kept request still blocks Sign in, Preview and a second Launch, as before',async()=>{
  const saved=earlierInstallation(),before=new Map(saved);
  const p=connectedPanel({saved,context:{provider_signin:true,signin_attempt:null,preview_state:'none'},
    client:{async setupSignin(action,input){p.h.calls.push(['signin-'+action,input]);throw new Error('lost');},
      async setupReceipt(input){p.h.calls.push(['receipt',input]);throw new Error('lost');}}});
  await p.finish();await flush();
  assert.equal(p.reads(),1);
  assert.deepEqual(p.h.api.snapshot().pending,settingUp);assert.deepEqual(p.h.api.snapshot().conflicts,[]);
  assert.equal(p.h.api.snapshot().note,'Setup was already submitted from this browser. Use this installation’s stable CLI with host update continue. Reconnecting never launches again.');
  assert.deepEqual(stepButtons(p.panel),['Check launch status']);
  buttonOf(p.panel,'Check launch status').click();await flush();
  assert.match(p.h.api.snapshot().note,/^The launch could not be confirmed\. .*Do not launch again\.$/);
  for(const operation of ['signin-begin','preview','launch'])assert.equal(await p.h.api.run(operation),false,operation);
  assert.equal(p.h.calls.filter(c=>['preview','launch'].includes(c[0])||c[0].startsWith('signin-')).length,0);
  assert.deepEqual(p.h.calls.filter(c=>c[0]==='receipt').map(c=>c[1]),[settingUp,settingUp],'only the kept request\'s status is read');
  assert.deepEqual(new Map(p.h.saved),before,'nothing this browser kept is changed');
  p.h.api.dispose();
});
test('a full list of conflicting identifiers drops its oldest when a computer set up again sets its request aside',async()=>{
  const older=Array.from({length:16},(_,i)=>({generation:id(100+i),request_id:id(200+i),config_fingerprint:'d'.repeat(64),source_revision:settingUp.source_revision}));
  const p=connectedPanel({saved:[['test',JSON.stringify({version:2,pending:settingUp,conflicts:older,legacy:[]})]],
    context:{generation:id(20),preview_state:'none'}});
  assert.deepEqual(p.h.api.snapshot().conflicts,older);
  await p.finish();await flush();
  assert.equal(p.h.api.snapshot().note,'');assert.equal(p.h.api.snapshot().pending,null);
  const kept=[...older.slice(1),settingUp];
  assert.deepEqual(p.h.api.snapshot().conflicts,kept,'the oldest goes; the list stays at 16');
  assert.deepEqual(JSON.parse(p.h.saved.get('test')),{version:2,pending:null,conflicts:kept,legacy:[]});
  assert.deepEqual(stepShape(p.panel),[['connect','done'],['approve','done'],['goal','current'],['launch','upcoming']]);
  const details=descendants(p.panel).find(n=>n.tagName==='PRE').textContent;
  assert.ok(details.includes(settingUp.request_id),'the request set aside is listed under Technical details');
  assert.ok(!details.includes(older[0].request_id));
  const reloaded=createGuidedSetup({client:p.h.client,storage:p.h.storage,storageKey:'test',now:()=>1800000000});
  assert.deepEqual(reloaded.snapshot().conflicts,kept);assert.equal(reloaded.snapshot().pending,null);
  reloaded.dispose();p.h.api.dispose();
});
test('after a failed first approval read, Check setup approval leads; once the computer approved, the earlier request is set aside',async()=>{
  let reads=0;const refused=()=>Object.assign(new Error('refused'),{status:403});
  const p=connectedPanel({saved:earlierInstallation(),context:{generation:id(20),provider_signin:true,signin_attempt:null,preview_state:'none'},
    client:{async setupContext(){p.h.calls.push(['context']);if(++reads===1)throw refused();return p.h.context;},
      async setupReceipt(input){p.h.calls.push(['receipt',input]);throw refused();}}});
  await p.finish();await flush();
  assert.equal(reads,1);assert.deepEqual(p.h.api.snapshot().pending,settingUp);
  assert.deepEqual(stepsOf(p.panel).filter(visible).map(filledIn),[[],['Check setup approval'],[],[]],'the approval check leads');
  assert.equal(p.focused()?.textContent,'Check setup approval');
  assert.equal(p.h.api.snapshot().note,'Could not confirm setup approval. Make sure setup was approved on this computer, then check again.',
    'the note matches the leading approval check');
  // A failed launch status read keeps the launch's own note.
  buttonOf(p.panel,'Check launch status').click();await flush();
  assert.match(p.h.api.snapshot().note,/^The launch could not be confirmed\. Choose Check launch status/);
  assert.deepEqual(stepsOf(p.panel).filter(visible).map(filledIn),[[],['Check setup approval'],[],[]],'the approval check still leads');
  buttonOf(p.panel,'Check setup approval').click();await flush();
  assert.equal(reads,2);assert.equal(p.h.api.snapshot().pending,null);assert.deepEqual(p.h.api.snapshot().conflicts,[settingUp]);
  assert.deepEqual(stepShape(p.panel),[['connect','done'],['approve','done'],['signin','current'],['goal','upcoming'],['launch','upcoming']]);
  assert.equal(offered(p.panel,'Sign in to Claude'),true);
  assert.equal(p.h.calls.filter(c=>['preview','launch'].includes(c[0])).length,0);
  p.h.api.dispose();
});
test('a browser that cannot save keeps the earlier request as it was, shown once, when a new approval is read',async()=>{
  const p=connectedPanel({saved:[keptLaunch()[0]],context:{generation:id(20),preview_state:'none'},
    storage:saved=>({setItem:(key,value)=>{if(key==='test')throw new Error('full');saved.set(key,value);}})});
  const before=p.h.saved.get('test');
  await p.finish();await flush();
  assert.equal(p.h.api.snapshot().note,'This browser could not save the setup identifiers, so nothing was launched. Allow site storage for this page, then try again.');
  assert.deepEqual(p.h.api.snapshot().pending,settingUp);
  assert.deepEqual(p.h.api.snapshot().conflicts,[],'never both kept and conflicting');
  assert.equal(p.h.saved.get('test'),before);
  assert.equal(p.h.calls.filter(c=>['preview','launch'].includes(c[0])).length,0);
  p.h.api.dispose();
});
test('a new approval\'s read naming a sign-in this page cannot use is rejected before anything kept is set aside',async()=>{
  const saved=earlierInstallation(),before=new Map(saved);
  const p=connectedPanel({saved,context:{generation:id(20),provider_signin:true,preview_state:'none',
      signin_attempt:{generation:id(20),attempt_id:id(30),state:'not-a-state',reason:null}},
    client:{async setupReceipt(input){p.h.calls.push(['receipt',input]);throw new Error('lost');}}});
  await p.finish();await flush();
  assert.equal(p.reads(),1);assert.equal(p.h.api.snapshot().context,null,'the read is rejected');
  assert.deepEqual(p.h.api.snapshot().pending,settingUp);assert.deepEqual(p.h.api.snapshot().conflicts,[]);
  assert.deepEqual(new Map(p.h.saved),before,'no storage changed');
  p.h.api.dispose();
});
test('the Launch team reassurance goes once the launch is held and stays gone after it finished',async()=>{
  const l=managedLaunch({launch:input=>({...reserved(input),managed_owner:undefined})});l.h.context.managed_owner=undefined;
  await l.h.api.run('context');l.h.api.setGoal('Improve checkout tests');await l.h.api.run('preview');
  assert.equal(l.lede().textContent,'Nothing runs on this computer until you choose Launch team.');assert.equal(visible(l.lede()),true);
  await l.h.api.run('launch');assert.equal(visible(l.lede()),false,'a held launch');
  l.h.client.setupReceipt=async input=>({...input,version:2,state:'done',launch_reserved:true,config_committed:true,health_errors:{}});
  await l.h.api.run('receipt');
  assert.equal(l.h.api.snapshot().note,'Team launched. Your team is running on this computer.');
  assert.equal(visible(l.lede()),false,'a finished launch');
  l.h.api.dispose();
});
test('the shared panel keeps the Launch team line through a held launch and drops it once the launch finished',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element(),lede=()=>descendants(panel).find(n=>n.className==='setup-lede');
  h.api.mountPanel({document:ui.document,element:panel});
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');await h.api.run('launch');
  assert.equal(visible(lede()),true,'its steps and line are as before while the launch is unconfirmed');
  await h.api.run('receipt');
  assert.match(h.api.snapshot().note,/^Team launched\./);
  assert.deepEqual(stepShape(panel).map(row=>row.slice(0,2)),[['approve','done'],['goal','done'],['launch','current']]);
  assert.equal(visible(lede()),false);
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

// A synthetic first-team plan of the measured real shape (about 63 KB): 76 changes, 70 units without their
// working data, and the bot and fleet lists. The Context answer that repeats it carries it under `preview`.
const SETUP_LIMIT=131072;
const realisticPlan=pending=>{const sha='c'.repeat(64);
  return {...pending,version:2,expires_at:1800000600,fleets:['shop'],
    changes:Array.from({length:76},(_,i)=>({target:`/home/example/fleets/shop/units/unit-${i}/config.toml`,before:{kind:'file',mode:'0644',sha256:sha},
      after:{kind:'directory',mode:'0755',files:Object.fromEntries(Array.from({length:4},(_,f)=>[`file-${f}.toml`,sha]))}})),
    effects:{units:Array.from({length:70},(_,i)=>({bot:`shop/bot-${i}`,enroll:false,fleet:'shop',phase:'start',platform:'linux',scope:'user',
        service:`claudlobby-bot-${i}.service`,sha256:sha,source:`/home/example/fleets/shop/units/unit-${i}`})),
      restart_bots:['shop/a','shop/b','shop/c'],reload_supervision:true,fleet_sources:{shop:'/home/example/fleets/shop'},
      bot_paths:{a:'/home/example/a',b:'/home/example/b'},coverage:{bots:70,units:70},fleet_manifests:{shop:sha},restart_reason:'units changed'},
    dispatch:'prepared',phase:'quiesce'};};
const bytesOf=value=>Buffer.byteLength(JSON.stringify(value));
const padded=(value,size)=>({...value,pad:'x'.repeat(size-bytesOf({...value,pad:''}))});
test('the setup panel offers Launch for a realistic 63 KB plan in a ready Context answer',async()=>{
  const h=setupHarness(),plan=realisticPlan(h.pending),context={...h.context,...said.ready({...h,preview:plan})};
  assert.ok(bytesOf(plan)>55000&&bytesOf(plan)<75000,String(bytesOf(plan)));assert.ok(bytesOf(context)>55000&&bytesOf(context)<75000);
  const p=previewHarness({answers:[()=>context]}),{panel}=p;
  await p.start();
  assert.equal(p.h.api.snapshot().note,READY_BANNER);
  assert.equal(p.h.api.snapshot().plan.changes.length,76);
  assert.equal(offered(panel,'Launch team'),true);assert.deepEqual(filledIn(panel),['Launch team']);
  p.h.api.dispose();h.api.dispose();
});
for(const operation of ['setup-preview','setup-context']) test(`browser transport carries a realistic ${operation} answer, accepts exactly the ceiling and retires on one byte over`,async t=>{
  const plan=realisticPlan({generation:id(9),request_id:id(10),config_fingerprint:'a'.repeat(64),source_revision:'b'.repeat(40)}),
    answer=operation==='setup-preview'?plan:{version:2,preview_state:'ready',preview:plan,request:{generation:id(9)}},
    path=operation==='setup-preview'?'preview':'context',input=operation==='setup-preview'?{generation:id(9),goal:'One useful team'}:{};
  assert.ok(bytesOf(answer)>55000&&bytesOf(answer)<75000,String(bytesOf(answer)));
  const pass=async body=>{
    const h=await bridgeHarness(t);h.admit();await h.bridge.ready;
    const pending=h.host('/api/owner/connected/setup/'+path,input);await flush();const request=h.frames.find(f=>f.type==='request');
    assert.equal(request.request.operation,operation);
    h.send('headers',{id:request.id,status:200,headers:{'content-type':'application/json'}});
    const raw=Buffer.from(JSON.stringify(body)),response=await pending,reading=response.json().then(v=>({v}),error=>({error}));
    for(let at=0;at<raw.length;at+=16384){h.send('chunk',{id:request.id,data:raw.subarray(at,at+16384).toString('base64')});await flush();}
    return {h,request,reading,raw};
  };
  const intended=await pass(answer);intended.h.send('end',{id:intended.request.id});
  assert.deepEqual((await intended.reading).v,answer);
  const exact=await pass(padded(answer,SETUP_LIMIT));assert.equal(exact.raw.length,SETUP_LIMIT);exact.h.send('end',{id:exact.request.id});
  assert.equal(bytesOf((await exact.reading).v),SETUP_LIMIT);
  const over=await pass(padded(answer,SETUP_LIMIT+1));assert.equal(over.raw.length,SETUP_LIMIT+1);over.h.send('end',{id:over.request.id});
  assert.ok((await over.reading).error,'the answer is refused, not delivered');
  await assert.rejects(over.h.host('/api/owner/connected/setup/'+path,input),'the over-limit answer retires the transport');
});

// The parts view (workspace-parts.js): a started computer's parts, from the parts read (setup-state).
const {PART_TEXT,partNames}=await import(partsURL);
const partsAnswer=(changes={})=>({version:1,computer:'started',provider:{state:'not_linked'},team:{state:'none'},teams:0,bots:[],helper:{state:'asleep'},...changes});
const partsOf=host=>host.children.find(n=>n.className==='workspace-parts');
// Each shown part as [part, state, name, status].
const partRows=root=>root.children[0].children.filter(visible).map(n=>[n.dataset.part,n.dataset.state,
  n.children.find(c=>c.className==='part-name').textContent,n.children.find(c=>c.className==='part-status').textContent]);
const stateCalls=h=>h.calls.filter(c=>c.url.endsWith('/api/owner/connected/setup/state'));
// A native workspace for one computer whose parts read answers `answer`; document changes as given.
async function partsWorkspace(t,{answer=partsAnswer(),document={},view}={}) {
  const h=harness({count:1}),a=refs[0].endpoint_origin;h.api.dispose();
  if(view)h.ui.document.defaultView=view;
  const workspace=createConnectedWorkspace({...h.dependencies,documentContext:{...h.doc,...document}});t.after(()=>workspace.dispose());
  h.overrides.set(a+'/api/owner/connected/setup/state',init=>{assert.equal(init.body,'{}');return typeof answer==='function'?answer():reply(200,answer);});
  workspace.mountSessionControls({document:h.ui.document,element:h.ui.get('owner-session')});
  await workspace.checkAccount();await workspace.act(0,'connect');await flush();
  const rail=h.ui.get('rail-channel'),host=rail.children[0];
  return {h,workspace,rail,host,parts:()=>partsOf(host)};
}
test('the parts view says only the sentences the owner approved (slice A, with B15 and B17)',()=>{
  assert.deepEqual({...PART_TEXT},{linked:'Linked',provider:'Claude',notLinked:'Not linked',bots:'Bots',
    none:'None yet',create:'Create a bot',createReason:'You can’t create bots here yet.',
    asleep:'Asleep until Claude is linked',later:'Coming soon',oneTeam:'1 team set up'});
});
test('a started computer with no team shows its parts in order, with no working control',async t=>{
  const w=await partsWorkspace(t),parts=w.parts();
  assert.equal(w.host.hidden,false);assert.equal(parts.hidden,false);assert.equal(w.rail.dataset.setupFocus,'only','with no team the parts are the whole view');
  assert.deepEqual(partRows(parts),[['computer','linked','a.example.test','Linked'],['provider','not_linked','Claude','Not linked'],['bots','none','Bots','None yet']],
    'no helper row: the embedding page did not name it');
  assert.equal(parts.attributes['aria-label'],'a.example.test');
  const buttons=descendants(parts).filter(n=>n.tagName==='BUTTON');
  assert.deepEqual(buttons.map(b=>[b.textContent,b.disabled,b.attributes['aria-disabled'],visible(b)]),[['Create a bot',true,'true',true]],'one control, visibly not available');
  const reason=descendants(parts).find(n=>n.className==='part-reason');
  assert.equal(reason.textContent,'You can’t create bots here yet.');assert.equal(buttons[0].attributes['aria-describedby'],reason.id);
  buttons[0].click();
  assert.equal(stateCalls(w.h).length,1,'one parts read for the connection');
  assert.deepEqual(w.h.calls.filter(c=>c.url.includes('/connected/setup/')).map(c=>c.url.split('/').at(-1)),['state'],'no approval read, no setup call');
  for(const timer of w.h.timers.filter(t=>t.active)){timer.active=false;timer.fn();}await flush();
  assert.equal(stateCalls(w.h).length,1,'nothing reads the parts again on a timer');
  assert.ok(descendants(parts).every(n=>!/fleet|pane/i.test(n.textContent)));
});
test('the parts read is sent again when the window regains focus, and nowhere else',async t=>{
  const handlers=new Map(),view={addEventListener:(name,fn)=>handlers.set(name,fn),removeEventListener:name=>handlers.delete(name)};view.parent=view;
  let answer=partsAnswer();
  const w=await partsWorkspace(t,{answer:()=>reply(200,answer),view});
  assert.equal(stateCalls(w.h).length,1);
  answer=partsAnswer({provider:{state:'linked'},helper:{state:'later'}});
  handlers.get('focus')();await flush();
  assert.equal(stateCalls(w.h).length,2);
  assert.deepEqual(partRows(w.parts())[1],['provider','linked','Claude','Linked']);
  await w.workspace.act(0,'logout');await flush();
  assert.equal(w.parts().hidden,true,'a closed connection shows no parts');handlers.get('focus')();await flush();
  assert.equal(stateCalls(w.h).length,2,'a computer that is not connected is not read');
  w.workspace.dispose();assert.equal(handlers.has('focus'),false,'closing the view stops listening');
});
test('a computer with a team keeps its parts as a strip above the dashboard',async t=>{
  const w=await partsWorkspace(t,{answer:partsAnswer({teams:1})}),parts=w.parts();
  assert.equal(w.host.hidden,false);assert.equal(w.rail.dataset.setupFocus,'shared','its teams stay readable below');
  assert.equal(w.host.children[0],parts,'the parts lead the setup host');
  // The count is the saved configuration's (also true mid-Launch, after a failed Launch and after a stop): "set up".
  assert.deepEqual(partRows(parts)[2],['bots','teams','Bots','1 team set up']);assert.equal(parts.dataset.teams,'1');
  assert.ok(descendants(parts).every(n=>!/running/i.test(n.textContent)),'nothing says a team is running');
  assert.equal(descendants(parts).some(n=>n.tagName==='BUTTON'&&visible(n)),false,'Create a bot shows only with no team');
  const fleets=await w.workspace.jget('/api/fleets');assert.equal(fleets.data.fleets.length,1,'the dashboard still reads');
});
test('with more than one team the bots part says nothing, since only one team has an approved sentence',async t=>{
  for(const teams of [2,5]){
    const w=await partsWorkspace(t,{answer:partsAnswer({teams})}),parts=w.parts();
    assert.deepEqual(partRows(parts)[2],['bots','teams','Bots',''],String(teams));assert.equal(parts.dataset.teams,String(teams));
    assert.ok(descendants(parts).every(n=>!/teams? (?:set up|running)/.test(n.textContent)),'no invented plural');
    assert.equal(descendants(parts).some(n=>n.tagName==='BUTTON'&&visible(n)),false);
    w.workspace.dispose();
  }
});
test('a computer the parts read calls not started shows exactly what it showed before',async t=>{
  const w=await partsWorkspace(t,{answer:partsAnswer({computer:'not_started'})});
  assert.equal(stateCalls(w.h).length,1);assert.equal(w.parts(),undefined,'no parts join the host');
  assert.equal(w.host.hidden,true);assert.equal(w.rail.dataset.setupFocus,undefined);
});
test('a computer in first-team setup is never sent the parts read and keeps today\'s five steps',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/issue',setupOnlyIssue);
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/context',()=>reply(200,landingContext));
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer()));
  descendants(stepsOf(l.panel())[0]).find(n=>n.tagName==='BUTTON').click();await flush();
  assert.equal(stateCalls(l.h).length,0);assert.equal(partsOf(l.host),undefined);
  assert.deepEqual(stepShape(l.panel()).map(row=>row[0]),['connect','approve','signin','goal','launch']);
});
test('on a bridged page a started computer shows its parts instead of the five-step panel',async t=>{
  const l=landing(t);await l.workspace.checkAccount();await flush();
  const a=landingRefs[0].endpoint_origin,panel=l.panel(),connect=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON');
  l.h.overrides.set(a+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer()));
  connect.focus();connect.click();await flush();
  assert.deepEqual(l.mutations().map(c=>c.url.split('/').at(-1)),['challenge','issue','state'],'one click, one connect, one parts read');
  const parts=partsOf(l.host);
  assert.deepEqual(l.host.children.filter(visible),[parts],'the parts are the whole view');assert.equal(l.panel(),undefined,'the panel left the view');
  assert.equal(l.status().hidden,true);assert.equal(l.rail.dataset.setupFocus,'only');
  assert.deepEqual(partRows(parts).map(row=>row[0]),['computer','provider','bots']);
  assert.equal(l.h.ui.document.activeElement,parts,'focus that was in the panel moves to the parts');
  assertPlain(l.shownText());
  assert.ok(!l.shownText().some(text=>/Approve setup|Check setup approval/.test(text)));
  assert.equal(l.h.calls.filter(c=>/[bc]\.example\.test/.test(c.url)).length,0,'no request is sent toward a hidden computer');
  // A lost connection brings back the panel, whose first step is Connect again.
  await l.workspace.act(0,'logout');await flush();
  assert.equal(l.panel(),panel);assert.equal(parts.hidden,true);
});
// A bridged page that sends one Launch from its own panel; launch: the computer's answer to it. Optional answers: status,
// the session status check after it (ready by default); fleets, the fleets read; receipt, the launch status read.
// context, fields added to the approval read's answer; beforeCheck runs before the status check.
const landingBinding={generation:id(9),request_id:id(10),config_fingerprint:'a'.repeat(64),source_revision:'b'.repeat(40)};
async function bridgedLaunch(t,launch,{status,fleets,receipt,context={},beforeCheck}={}) {
  const l=landing(t);await l.workspace.checkAccount();await flush();
  const a=landingRefs[0].endpoint_origin,setup=a+'/api/owner/connected/setup/';
  const binding=landingBinding;
  l.h.overrides.set(a+'/api/owner/connected/issue',setupOnlyIssue);
  l.h.overrides.set(setup+'context',()=>reply(200,{...landingContext,provider_signin:false,...context}));
  l.h.overrides.set(setup+'preview',()=>reply(200,{...binding,version:2,fleets:['shop'],changes:[],effects:{restart_bots:['shop/lead'],reload_supervision:true},
    dispatch:'prepared',phase:'quiesce',expires_at:1800000600}));
  l.h.overrides.set(setup+'launch',()=>reply(200,{...binding,version:2,...launch}));
  // After the Launch the computer's session comes back ready, as the hand-off's status check finds it; its parts read
  // would say started with the saved configuration's team.
  l.h.overrides.set(a+'/api/owner/connected/status',status||(()=>reply(200,{version:1,state:'ready',expires_in:900,
    read_profile:{version:1,profile:'direct-owner-read-v1',host_uid:'host_'+'a'.repeat(32)},session_context:{issuer:documentContext.shell_origin,
      subject:documentContext.user_id,workspace_id:documentContext.workspace_id,operational_origin:documentContext.operational_origin,binding_revision:1,binding_generation:id(6)}})));
  l.h.overrides.set(setup+'state',()=>reply(200,partsAnswer({teams:1})));
  if(fleets)l.h.overrides.set(a+'/api/owner/connected/read/fleets',fleets);
  if(receipt)l.h.overrides.set(setup+'receipt',receipt);
  const panel=l.panel(),button=text=>descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
  descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON').click();await flush();
  const area=descendants(panel).find(n=>n.tagName==='TEXTAREA');area.value='Improve checkout tests';area.handlers.input();
  button('Preview team').click();await flush();button('Launch team').click();await flush();
  assert.equal(l.h.calls.filter(c=>c.url===setup+'launch').length,1,'one Launch');
  if(beforeCheck)await beforeCheck(l,panel);
  await l.workspace.act(0,'check');await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,status?(await (await status()).json()).state:'ready');
  return {l,panel,note:()=>descendants(panel).find(n=>n.className==='setup-note').textContent};
}
// Document 03 replaced this test's ending: the page that followed the Launch no longer keeps the five steps after it.
test('on a bridged page the five-step panel stays to its "Team launched" ending through a Launch this page sent, and the team\'s room follows once the session is ready',async t=>{
  const {l,panel,note}=await bridgedLaunch(t,{state:'done',launch_reserved:true,config_committed:true,health_errors:{}},{beforeCheck:(l,panel)=>{
    assert.equal(stateCalls(l.h).length,0,'nothing reads the parts while the session is in first-team setup');
    assert.equal(l.panel(),panel);assert.equal(partsOf(l.host),undefined);assert.equal(l.frame.page.attributes['data-workspace-view'],'setup');
    assert.ok(descendants(stepsOf(panel).at(-1)).some(n=>n.textContent==='Team launched'&&visible(n)),'the guide keeps its ending');
    assert.equal(descendants(panel).find(n=>n.className==='setup-note').textContent,'Team launched. Your team is running on this computer.');
  }});
  // The clean done receipt ended the follow: the ready session's parts read moves the page on to the team's room.
  assert.equal(stateCalls(l.h).length,1,'one parts read, for the ready session');
  assert.equal(l.panel(),undefined,'the panel left the view');assert.equal(partsOf(l.host)?.hidden,false);
  assert.equal(l.frame.page.attributes['data-workspace-view'],undefined);assert.equal(l.rail.dataset.setupFocus,'shared');
  assert.equal(note(),'Team launched. Your team is running on this computer.');
  // A later page has the same request in this browser's storage and follows nothing: it shows the parts.
  const later=landing(t);await later.workspace.checkAccount();await flush();
  later.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer({teams:1})));
  descendants(stepsOf(later.panel())[0]).find(n=>n.tagName==='BUTTON').click();await flush();
  assert.equal(stateCalls(later.h).length,1);
  assert.equal(partsOf(later.host)?.hidden,false,'a request kept from an earlier page does not hold the panel');assert.equal(later.panel(),undefined);
});
// A page opened again after this browser sent a Launch (or only a Preview): storage holds the request, the computer is
// ready, and its parts read says what became of the Launch.
async function reopened(t,{team,receipt,teams=1,stored=true,computer='started'}) {
  landing(t); // stubs this test's localStorage
  if(stored)globalThis.localStorage.setItem(`claudlobby:first-team:${id(1)}:${id(2)}:${id(3)}:${landingRefs[0].reference_id}:1`,
    JSON.stringify({version:2,pending:settingUp,conflicts:[],legacy:[]}));
  const l=landing(t);await l.workspace.checkAccount();await flush();
  const setup=landingRefs[0].endpoint_origin+'/api/owner/connected/setup/';
  l.h.overrides.set(setup+'state',()=>reply(200,partsAnswer({computer,teams,team:{state:team}})));
  if(receipt)l.h.overrides.set(setup+'receipt',()=>reply(200,{...settingUp,version:2,launch_reserved:true,config_committed:true,health_errors:{},...receipt}));
  const panel=l.panel();
  descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON').click();await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');assert.equal(stateCalls(l.h).length,1);
  return {l,panel,note:()=>descendants(panel).find(n=>n.className==='setup-note').textContent,
    button:text=>descendants(panel).find(n=>n.tagName==='BUTTON'&&n.textContent===text&&visible(n)),
    lede:()=>visible(descendants(panel).find(n=>n.textContent==='Nothing runs on this computer until you choose Launch team.'))};
}
for(const [name,receipt,said] of [['in progress',{state:'quiesce'},/^Launch received\./],['needing attention',{state:'unknown'},/^The launch needs attention/]])
  test(`a page opened again while the computer's Launch is ${name} keeps the five-step panel and Check launch status`,async t=>{
    const {l,panel,note,button,lede}=await reopened(t,{team:'launch_unfinished',receipt});
    assert.equal(l.panel(),panel,'the panel stays');assert.equal(partsOf(l.host)?.hidden??true,true,'the parts do not replace it');
    // Before any read, the steps say only what the computer said: a Launch is under way, so nothing waits on Launch team
    // and the approval it ran under is shown done. Nothing was read or sent for it.
    assert.equal(lede(),false,'no "Nothing runs on this computer" line while a Launch is under way');
    assert.deepEqual(stepShape(panel),[['connect','done'],['approve','done'],['goal','done'],['launch','current']]);
    assert.ok(descendants(panel).some(n=>n.textContent==='Setup approved'&&visible(n)));
    assert.deepEqual(l.h.calls.filter(c=>c.url.includes('/connected/setup/')).map(c=>c.url.split('/').at(-1)),['state'],'only the parts read');
    button('Check launch status').click();await flush();
    assert.equal(l.h.calls.filter(c=>c.url.endsWith('/setup/receipt')).length,1);assert.equal(l.h.calls.filter(c=>c.url.endsWith('/setup/launch')).length,0,'never a second Launch');
    assert.match(note(),said);assert.equal(l.panel(),panel);
    l.frame.view.listeners.get('focus')();await flush();
    assert.equal(stateCalls(l.h).length,1,'once this page follows the Launch, nothing reads the parts');
  });
test('another browser opening the computer mid-Launch is not stuck: Check setup approval, then Check launch status',async t=>{
  const {l,panel,note,button,lede}=await reopened(t,{team:'launch_unfinished',stored:false,receipt:{state:'quiesce'}});
  const setup=landingRefs[0].endpoint_origin+'/api/owner/connected/setup/';
  l.h.overrides.set(setup+'context',()=>reply(200,{...landingContext,provider_signin:false,request:settingUp,launch_reserved:true}));
  assert.equal(l.panel(),panel,'the panel stays');assert.equal(lede(),false,'no "Nothing runs" line while a Launch is under way');
  // With no request of its own, this browser shows approval as a step to check: Check setup approval leads.
  assert.deepEqual(stepShape(panel).slice(0,2),[['connect','done'],['approve','current']]);
  assert.equal(button('Check launch status'),undefined,'nothing to check a launch by yet');
  button('Check setup approval').click();await flush();
  assert.deepEqual(l.h.calls.filter(c=>c.url.includes('/connected/setup/')).map(c=>c.url.split('/').at(-1)),['state','context'],'one read; nothing sent');
  assert.deepEqual(stepShape(panel),[['connect','done'],['approve','done'],['goal','done'],['launch','current']]);
  assert.match(note(),/^Setup was already submitted from this browser\./,'the context answer names the request');
  button('Check launch status').click();await flush();
  assert.deepEqual(l.h.calls.filter(c=>c.url.includes('/connected/setup/')).map(c=>c.url.split('/').at(-1)),['state','context','receipt']);
  assert.match(note(),/^Launch received\./);assert.equal(l.h.calls.filter(c=>c.url.endsWith('/setup/launch')).length,0,'never a Launch');
});
test('a computer not started yet whose first Launch is unfinished keeps the panel and ticks only what is known',async t=>{
  for(const stored of [false,true]){
    const {l,panel,lede,button}=await reopened(t,{team:'launch_unfinished',teams:0,computer:'not_started',stored});
    assert.equal(l.panel(),panel);assert.equal(partsOf(l.host),undefined,'a computer that is not started shows no parts');
    assert.equal(lede(),false,'no "Nothing runs" line while its first Launch is under way');
    assert.deepEqual(stepShape(panel),stored?[['connect','done'],['approve','done'],['goal','done'],['launch','current']]
      :[['connect','done'],['approve','current'],['signin','upcoming'],['goal','upcoming'],['launch','upcoming']],
      stored?'with this browser\'s request: approval and goal, as the request shows':'with no request: nothing past Connect is ticked');
    assert.equal(!!button('Check launch status'),stored);
    l.workspace.dispose();
  }
});
test('the panel lets go of the Launch it showed under way when the next parts read no longer says so',async t=>{
  for(const [name,next] of [['finished',{computer:'started',teams:1,team:{state:'none'}}],['failed and cleared',{computer:'not_started',teams:0,team:{state:'none'}}]]){
    const {l,panel,lede,button}=await reopened(t,{team:'launch_unfinished'});
    assert.equal(lede(),false);assert.deepEqual(stepShape(panel).slice(0,2),[['connect','done'],['approve','done']]);
    l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer(next)));
    l.frame.view.listeners.get('focus')();await flush();
    assert.equal(stateCalls(l.h).length,2,name);
    if(name==='finished'){assert.equal(partsOf(l.host)?.hidden,false,name);assert.equal(l.panel(),undefined,'the parts replace the panel');}
    else{
      assert.equal(l.panel(),panel,name);
      assert.deepEqual(stepShape(panel),[['connect','done'],['approve','current'],['goal','done'],['launch','current']]);
      assert.deepEqual(stepsOf(panel).filter(visible).map(filledIn),[[],['Check setup approval'],[],[]],'with no approval known, Check setup approval leads');
      assert.equal(lede(),true,'the "Nothing runs" line is back');
    }
    assert.equal(l.h.calls.filter(c=>/\/setup\/(?:launch|preview|context|receipt)$/.test(c.url)).length,0,'nothing was sent');
    l.workspace.dispose();
  }
});
test('a page opened again after the computer finished the Launch shows the parts',async t=>{
  const {l}=await reopened(t,{team:'none'});
  assert.equal(partsOf(l.host)?.hidden,false);assert.equal(l.panel(),undefined);
});
test('a Preview kept in storage with nothing launched does not hold the panel once the computer is started',async t=>{
  const {l}=await reopened(t,{team:'none',teams:0});
  assert.equal(partsOf(l.host)?.hidden,false,'the stale Preview never holds the panel');assert.equal(l.panel(),undefined);
  assert.deepEqual(partRows(partsOf(l.host))[2],['bots','none','Bots','None yet']);
});
for(const [name,launch,said] of [['received',{state:'quiesce',launch_reserved:true,config_committed:true,health_errors:{}},/^Launch received\./],
  ['needing attention',{state:'unknown',launch_reserved:true,config_committed:true,health_errors:{}},/^The launch needs attention/]])
  test(`on a bridged page with a Launch ${name} the panel keeps its own words and no parts read is sent, even when the window regains focus`,async t=>{
    const {l,panel,note}=await bridgedLaunch(t,launch);
    assert.match(note(),said);
    l.frame.view.listeners.get('focus')();await flush();
    assert.equal(stateCalls(l.h).length,0);assert.equal(l.panel(),panel);assert.equal(partsOf(l.host),undefined);
    assert.ok(!l.shownText().some(text=>/running/i.test(text)),'nothing says a team is running');
  });
for(const [name,answer] of [['a refusal',()=>reply(403,{state:'denied'})],['no such operation (an older computer)',()=>reply(404,{state:'denied'})],
  ['a lost answer',()=>{throw new Error('lost');}],['an answer this view cannot use',()=>reply(200,partsAnswer({provider:{state:'unknown'}}))]])
  test(`on a bridged page ${name} to the parts read shows exactly what the page shows today`,async t=>{
    const shown=async override=>{
      const l=landing(t);await l.workspace.checkAccount();await flush();
      if(override)l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state',override);
      descendants(stepsOf(l.panel())[0]).find(n=>n.tagName==='BUTTON').click();await flush();
      return {l,text:l.shownText(),shape:stepShape(l.panel())};
    };
    const today=await shown(null),now=await shown(answer); // the harness has no parts read at all: today's computer
    assert.deepEqual(now.text,today.text);assert.deepEqual(now.shape,today.shape);
    assert.equal(partsOf(now.l.host),undefined);assert.equal(now.l.rail.dataset.setupFocus,'only');
    assert.equal(stateCalls(now.l.h).length,1,'one read, never retried');
  });
// The working form (document 03): on a bridged page the bound computer, started with a team and with a ready session,
// is the connected view a direct connection gets for that one computer, its parts as a strip above its teams.
const setupMarker=l=>l.frame.page.attributes['data-workspace-view'];
const formsPosted=l=>l.frame.posts.filter(([message])=>message.type==='claudfather.workspace.form').map(([message])=>message.form);
// Connect on a bridged page whose computer is already started; answer: its parts read.
async function workingPage(t,{teams=1,answer}={}) {
  const l=landing(t);await l.workspace.checkAccount();await flush();
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state',answer||(()=>reply(200,partsAnswer({teams}))));
  const panel=l.panel(),connect=descendants(stepsOf(panel)[0]).find(n=>n.tagName==='BUTTON');
  connect.focus();connect.click();await flush();
  return {l,panel,parts:()=>partsOf(l.host)};
}
// Every rule that hides part of the page applies only under the setup marker or the 'only' setup focus.
const hidingRules=async()=>{
  const css=(await read('workspace.css')).replace(/\/\*[\s\S]*?\*\//g,'').replace(/\s+/g,' ');
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([,,body])=>/display:none/.test(body))
    .flatMap(([,selectors])=>selectors.split(',').map(s=>s.trim()))
    // The All agents view (.agent-directory-view) is a view the person chooses, the same in a direct connected view.
    .filter(s=>/(?:^|[\s>])(?:header|#rail-fleet|#rail-channel|#channel|#work-loop|#tasks|#view-nav|main|:not\(#setup-focus\))$/.test(s)
      &&!/\.agent-directory-view/.test(s));
};
test('on a bridged page a started computer with a team and a ready session shows its rooms, tasks and message box, with its parts above',async t=>{
  const w=await workingPage(t),{l}=w,parts=w.parts();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  assert.equal(setupMarker(l),undefined,'no data-workspace-view: workspace.css shows the whole connected view');
  assert.equal(l.rail.dataset.setupFocus,'shared','the parts sit above the teams');
  assert.equal(l.host.hidden,false);assert.deepEqual(l.host.children.filter(visible),[parts],'the parts lead the setup host');
  assert.equal(l.status().hidden,true);assert.equal(l.panel(),undefined,'the five-step panel left the view');
  assert.deepEqual(partRows(parts)[2],['bots','teams','Bots','1 team set up']);
  assert.equal(l.h.ui.document.activeElement,parts,'focus that was in the panel moves to the parts, never into the message box');
  // Rooms, Tasks and All agents are on; Team details, Recorder, search and bot equipment stay off, as in a direct connected view.
  assert.deepEqual(Object.fromEntries(l.h.ui.nav.map(b=>[b.dataset.view,b.disabled])),{channel:false,tasks:false,grid:false,fleet:true,trust:true});
  assert.equal(l.h.ui.get('search').disabled,true);
  // Under the page's connection nothing offers enrolment or the computer's own access page.
  assert.equal(descendants(l.cards()[0]).some(n=>n.dataset.operation==='enroll'),false);
  // The room, tasks and message box are drawn by the dashboard from the same reads, through this computer only.
  const fleets=await l.workspace.jget('/api/fleets');assert.equal(fleets.state,'ok');assert.equal(fleets.data.fleets.length,1);
  for(const path of ['/api/channel?limit=120','/api/tasks'])assert.equal((await l.workspace.jget(path)).state,'ok',path);
  assert.equal(l.h.calls.filter(c=>/[bc]\.example\.test/.test(c.url)).length,0,'no request is sent toward a hidden computer');
  // Without the marker no rule hides the header, the sidebar, the room or the message box; the shared focus hides nothing.
  for(const selector of await hidingRules())
    assert.match(selector,/:root\[data-workspace-view="setup"\]|\[data-setup-focus="only"\]/,selector);
});
// The card's disclosure: only connection options in the working form; first-time setup with its paragraph otherwise.
const disclosure=l=>{const help=descendants(l.cards()[0]).find(n=>n.className==='connection-approval');
  return {summary:help.children[0].textContent,steps:help.children[1]};};
test('in the working form the card offers only connection options; in the setup form also first-time setup',async t=>{
  const w=await workingPage(t),{l}=w;
  assert.deepEqual(formsPosted(l),['setup','working']);
  const working=disclosure(l);
  assert.equal(working.summary,'Connection options');assert.equal(working.steps.hidden,true,'no guided-setup paragraph');
  const s=await workingPage(t,{teams:0}),setupForm=disclosure(s.l);
  assert.equal(setupMarker(s.l),'setup');
  assert.equal(setupForm.summary,'Connection options and first-time setup');assert.equal(setupForm.steps.hidden,false);
  assert.equal(setupForm.steps.textContent,'Use your workspace’s guided setup to approve this computer and workspace, then finish first-team setup in the setup panel.');
});
test('on a bridged page a started computer with no team keeps the setup form and its parts',async t=>{
  const w=await workingPage(t,{teams:0}),{l}=w,parts=w.parts();
  assert.equal(setupMarker(l),'setup');assert.equal(l.rail.dataset.setupFocus,'only','the parts are the whole view');
  assert.deepEqual(l.host.children.filter(visible),[parts]);assert.equal(l.panel(),undefined);
  assert.deepEqual(partRows(parts)[2],['bots','none','Bots','None yet']);
});
test('a session renewal on a bridged working page does not change the form',async t=>{
  const w=await workingPage(t),{l}=w,parts=w.parts(),a=landingRefs[0].endpoint_origin,renew=a+'/api/owner/connected/renew';
  const gate=deferred(),posts=l.frame.sizes().length;
  l.h.overrides.set(renew,init=>gate.promise.then(()=>{l.h.overrides.delete(renew);return l.h.dependencies.fetch(renew,init);}));
  const renewing=l.workspace.act(0,'renew');await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'renewing');
  assert.equal(setupMarker(l),undefined);assert.equal(l.rail.dataset.setupFocus,'shared');assert.equal(parts.hidden,false);
  gate.resolve();assert.equal(await renewing,true);await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  assert.equal(setupMarker(l),undefined);assert.equal(l.rail.dataset.setupFocus,'shared');assert.equal(w.parts(),parts);
  assert.equal(stateCalls(l.h).length,1,'a renewal sends no parts read');
  assert.equal(l.frame.sizes().length,posts,'and posts no size');
  assert.deepEqual(formsPosted(l),['setup','working'],'nor the form again');
});
test('the working form posts one fixed height and no measured height; going back to the setup form measures again',async t=>{
  const l=landing(t),{frame}=l;await l.workspace.checkAccount();await flush();
  frame.resize(900);
  l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer({teams:1})));
  descendants(stepsOf(l.panel())[0]).find(n=>n.tagName==='BUTTON').click();await flush();
  assert.equal(setupMarker(l),undefined);
  frame.resize(1200);frame.resize(640.5);
  await l.workspace.act(0,'logout');await flush();
  assert.equal(setupMarker(l),'setup','a lost connection brings back the setup form');
  frame.resize(700);
  assert.deepEqual(frame.sizes().map(([message])=>message.height),[900,640,641,700],
    'measured, then one fixed 640 on entering the working form and nothing measured in it, then measured again');
  assert.ok(frame.sizes().every(([message,origin])=>Object.keys(message).join()==='type,version,height'&&origin===documentContext.shell_origin));
});
test('on a bridged working page without a message grant the message box says Messages: not authorized. and sends nothing',async t=>{
  const w=await workingPage(t),{l}=w;await l.workspace.jget('/api/overview');
  l.h.grants.delete('message');
  await assert.rejects(l.workspace.interactionContext(sourceA+' / team'),{code:'action_not_authorized'},
    'the computer\'s answer reaches the message box unchanged; its words are pinned in plane_work_loop.test.mjs');
  assert.equal(l.h.calls.filter(c=>/\/actions\/(?:send|prepare|receipt)$/.test(c.url)).length,0,'nothing is sent');
  assert.equal(setupMarker(l),undefined,'reading stays: the form does not change');assert.equal(l.workspace.snapshot().computers[0].state,'ready');
});
// The page that sent a Launch and a page opened again end on the same view (document 03, run 39's first finding).
test('the page that sent a Launch and a reopened page land on the same working view after a clean done receipt',async t=>{
  const shape=l=>({marker:setupMarker(l),focus:l.rail.dataset.setupFocus,panel:l.panel()===undefined?'none':'shown',
    status:l.status().hidden,parts:partRows(partsOf(l.host)),nav:l.h.ui.nav.map(b=>b.disabled)});
  const {l:followed}=await bridgedLaunch(t,{state:'done',launch_reserved:true,config_committed:true,health_errors:{}});
  const {l:again}=await reopened(t,{team:'none'});
  assert.deepEqual(shape(followed),shape(again));
  assert.equal(shape(again).marker,undefined);assert.equal(shape(again).focus,'shared');assert.equal(shape(again).panel,'none');
});
for(const [name,launch] of [['done with a health error',{state:'done',launch_reserved:true,config_committed:true,health_errors:{lead:'not running'}}],
  ['done but not committed',{state:'done',launch_reserved:true,config_committed:false,health_errors:{}}]])
  test(`a receipt that is not clean (${name}) keeps the panel and Check launch status on the page that followed the Launch`,async t=>{
    const {l,panel,note}=await bridgedLaunch(t,launch);
    assert.equal(l.panel(),panel,name);assert.equal(setupMarker(l),'setup',name);assert.equal(l.rail.dataset.setupFocus,'only');
    assert.notEqual(note(),'Team launched. Your team is running on this computer.',name);
    assert.ok(descendants(panel).some(n=>n.tagName==='BUTTON'&&n.textContent==='Check launch status'&&visible(n)),name);
    l.frame.view.listeners.get('focus')();await flush();
    assert.equal(stateCalls(l.h).length,0,`${name}: the page still follows its Launch, so nothing reads the parts`);
    assert.equal(partsOf(l.host),undefined);
  });
// Proof A, the browser side: after a healthy Launch the session's status through the page's own connection answers
// "ready" with a read profile, the fleets read lists the launched team, and only then does the room show.
const launchedFleets=()=>reply(200,ok({fleets:[{alias:'shop',uid:fleetUID,bots:2,provisional:0}]}));
const cleanDone={state:'done',launch_reserved:true,config_committed:true,health_errors:{}};
test('after a clean done receipt the next session status through the page answering ready leads to the working form',async t=>{
  const {l}=await bridgedLaunch(t,cleanDone,{fleets:launchedFleets});
  const status=l.h.calls.filter(c=>c.url===landingRefs[0].endpoint_origin+'/api/owner/connected/status');
  assert.equal(status.length,1,'one status check, through the page\'s transport');
  const answer=await (await l.h.overrides.get(status[0].url)()).json();
  assert.equal(answer.state,'ready');assert.equal(answer.read_profile.profile,'direct-owner-read-v1');
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  const fleets=await l.workspace.jget('/api/fleets');
  assert.equal(fleets.state,'ok');assert.deepEqual(fleets.data.fleets.map(f=>f.alias.split(' / ').at(-1)),['shop'],'the fleets read lists the launched team');
  assert.equal(setupMarker(l),undefined);assert.equal(l.rail.dataset.setupFocus,'shared');assert.equal(stateCalls(l.h).length,1);
});
test('after a clean done receipt a session status through the page answering setup_only keeps the setup form and never shows the room',async t=>{
  const setupOnly={version:1,state:'setup_only',expires_in:900,setup_profile:{version:2,profile:'first-team-setup-v2',host_uid:'host_'+'a'.repeat(32)},
    session_context:{issuer:documentContext.shell_origin,subject:documentContext.user_id,workspace_id:documentContext.workspace_id,
      operational_origin:documentContext.operational_origin,binding_revision:1,binding_generation:id(6)}};
  const {l:held,panel}=await bridgedLaunch(t,cleanDone,{status:()=>reply(200,setupOnly)});
  assert.equal(held.workspace.snapshot().computers[0].state,'setup_only');
  held.frame.view.listeners.get('focus')();await flush();
  assert.equal(stateCalls(held.h).length,0,'a setup_only session is never sent the parts read');
  assert.equal(setupMarker(held),'setup');assert.equal(held.rail.dataset.setupFocus,'only');assert.equal(held.panel(),panel);
  assert.ok(descendants(stepsOf(panel).at(-1)).some(n=>n.textContent==='Team launched'&&visible(n)),'the panel keeps its ending');
});
test('a parts read held back while the page followed a managed hand-off is sent once its clean done receipt arrives',async t=>{
  const {l,panel}=await bridgedLaunch(t,{state:'quiesce',managed_owner:true,launch_reserved:true,config_committed:false,health_errors:{}},
    {context:{managed_owner:true},receipt:()=>reply(200,{...landingBinding,version:2,state:'done',managed_owner:true,launch_reserved:true,config_committed:true,health_errors:{}})});
  // The session is ready while the hand-over still follows the Launch: the parts read is held back.
  assert.equal(stateCalls(l.h).length,0);assert.equal(l.panel(),panel);assert.equal(setupMarker(l),'setup');
  // The hand-over's own timer (1.5 s for its first reads) reads the launch status.
  for(let round=0;round<4&&!l.h.calls.some(c=>c.url.endsWith('/setup/receipt'));round++){
    for(const timer of l.h.timers.filter(t=>t.active&&t.ms===1500)){timer.active=false;await timer.fn();}await flush();
  }
  assert.equal(l.h.calls.filter(c=>c.url.endsWith('/setup/receipt')).length,1,'the hand-over read the launch status once');
  assert.equal(stateCalls(l.h).length,1,'one parts read, once the follow ended');
  assert.equal(setupMarker(l),undefined);assert.equal(l.rail.dataset.setupFocus,'shared');assert.equal(l.panel(),undefined);
  assert.equal(l.h.calls.filter(c=>c.url.endsWith('/setup/launch')).length,1,'never a second Launch');
});
test('the embedding page names parts only with a well-formed part_names; anything else is dropped whole',async t=>{
  const named=await partsWorkspace(t,{document:{part_names:{helper:'Claudfather',bots:'Team bots'}}});
  assert.equal(named.workspace.snapshot().phase,'ready');
  assert.deepEqual(partRows(named.parts()),[['computer','linked','a.example.test','Linked'],['provider','not_linked','Claude','Not linked'],
    ['bots','none','Team bots','None yet'],['helper','asleep','Claudfather','Asleep until Claude is linked']]);
  const linked=await partsWorkspace(t,{document:{part_names:{helper:'Helper'}},answer:partsAnswer({provider:{state:'linked'},helper:{state:'later'}})});
  assert.deepEqual(partRows(linked.parts()).at(-1),['helper','later','Helper','Coming soon']);
  // A computer keeps its own saved name, so a computer key drops the field; and a name that could read as a status is refused.
  for(const bad of [{computer:'Office Mac'},{computer:'Office Mac',helper:'Helper'},{helper:'Claude - Linked'},{helper:'Claude: Linked'},
    {provider:'Claude · Linked'},{bots:'Bots (None yet)'},{provider:'Linked'},{provider:'Not linked'},{provider:'Claude Linked'},
    {provider:'claude-linked'},{bots:'None yet'},{bots:'My bots none yet'},{helper:'Coming soon'},{bots:'1 team set up'},{helper:'Asleep until Claude is linked'},{helper:'Helper.'},{helper:'Set--up'},{helper:'Set up '},{helper:'Set  up'},
    {helper:''},{helper:'x'.repeat(41)},{helper:' Helper'},{helper:'Hélper'},{helper:'Help\ner'},{assistant:'Helper'},
    {helper:'Helper',bots:7},['Helper'],'Helper',null]) {
    assert.equal(partNames(bad),null,JSON.stringify(bad));
    const w=await partsWorkspace(t,{document:{part_names:bad}});
    assert.equal(w.workspace.snapshot().phase,'ready','the document is still admitted');
    assert.deepEqual(partRows(w.parts()).map(row=>row[2]),['a.example.test','Claude','Bots'],'no name from a malformed field, and no helper row');
  }
  assert.deepEqual(partNames({helper:'x'.repeat(40),bots:'My bots',provider:'Claude 2'}),{helper:'x'.repeat(40),bots:'My bots',provider:'Claude 2'});
  assert.deepEqual(partNames({helper:'Set-up helper'}),{helper:'Set-up helper'},'a hyphen joins two words');
  assert.deepEqual(partNames({helper:'Claudfather',bots:'None',provider:'Unlinked Claude'}),{helper:'Claudfather',bots:'None',provider:'Unlinked Claude'},
    'only a whole status the view says is refused');
  // With two computers, each shows its own saved name.
  const h=harness();h.api.dispose();
  const two=createConnectedWorkspace({...h.dependencies,documentContext:{...h.doc,part_names:{helper:'Claudfather'}}});t.after(()=>two.dispose());
  for(const ref of refs)h.overrides.set(ref.endpoint_origin+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer()));
  two.mountSessionControls({document:h.ui.document,element:h.ui.get('owner-session')});
  await two.checkAccount();await two.act(0,'connect');await two.act(1,'connect');await flush();
  const host=h.ui.get('rail-channel').children[0],roots=host.children.filter(n=>n.className==='workspace-parts');
  assert.deepEqual(roots.map(root=>partRows(root)[0][2]).sort(),['a.example.test','b.example.test']);
  const other=harness({count:1});other.api.dispose();
  const refused=createConnectedWorkspace({...other.dependencies,documentContext:{...other.doc,names:{helper:'Helper'}}});t.after(()=>refused.dispose());
  assert.equal(refused.snapshot().phase,'denied','any other extra key still refuses the document');
});
test('renewing the session keeps the parts shown and keyboard focus in them, and sends no parts read',async t=>{
  const w=await partsWorkspace(t,{answer:partsAnswer({teams:1})}),parts=w.parts(),a=refs[0].endpoint_origin,renew=a+'/api/owner/connected/renew';
  const gate=deferred();
  w.h.overrides.set(renew,init=>gate.promise.then(()=>{w.h.overrides.delete(renew);return w.h.dependencies.fetch(renew,init);}));
  parts.focus();const layout=w.rail.dataset.setupFocus;
  const renewing=w.workspace.act(0,'renew');await flush();
  assert.equal(w.workspace.snapshot().computers[0].state,'renewing');
  assert.equal(parts.hidden,false,'the parts stay while the session renews');assert.equal(w.rail.dataset.setupFocus,layout);
  gate.resolve();assert.equal(await renewing,true);await flush();
  assert.equal(w.workspace.snapshot().computers[0].state,'ready');
  assert.equal(w.parts(),parts);assert.equal(parts.hidden,false);assert.equal(w.rail.dataset.setupFocus,layout,'the layout never moved');
  assert.equal(w.h.ui.document.activeElement,parts,'keyboard focus stays in the parts');
  assert.equal(stateCalls(w.h).length,1,'a renewal sends no parts read');
  assert.equal(w.h.calls.filter(c=>c.url===renew).length,2,'one renewal (the gate re-enters the harness once)');
});
test('a connection change while a parts read is in flight is followed by one read for the new connection',async t=>{
  for(const change of ['renew','reconnect']){
    const gate=deferred();let answered=0;
    const w=await partsWorkspace(t,{answer:()=>++answered===1?gate.promise.then(()=>reply(200,partsAnswer())):reply(200,partsAnswer())});
    assert.equal(stateCalls(w.h).length,1);assert.equal(w.parts(),undefined,'the first read has not answered');
    if(change==='renew')assert.equal(await w.workspace.act(0,'renew'),true,change);
    else{await w.workspace.act(0,'logout');assert.equal(await w.workspace.act(0,'connect'),true,change);}
    await flush();
    assert.equal(stateCalls(w.h).length,1,`${change}: the new connection waits for the read in flight`);
    gate.resolve();await flush();
    assert.equal(stateCalls(w.h).length,2,`${change}: one more read, for the new connection`);
    assert.equal(w.parts()?.hidden,false,`${change}: its answer is shown`);
    w.workspace.dispose();
  }
});
test('the browser carrier carries the parts read and keeps part_names out of what it tells the parent',async t=>{
  const h=await bridgeHarness(t,{document:{...browserDocument(),part_names:{helper:'Helper'}}});
  assert.equal('part_names' in h.sent[0].data.document,false);
  h.admit();await h.bridge.ready;await flush();
  const pending=h.host('/api/owner/connected/setup/state',{});await flush();
  const request=h.frames.find(f=>f.type==='request');
  assert.deepEqual(request.request,{targetHandle:h.offer.targetHandle,operation:'setup-state',input:{},token:'A'.repeat(43)});
  h.send('headers',{id:request.id,status:200,headers:{'content-type':'application/json'}});
  h.send('chunk',{id:request.id,data:Buffer.from(JSON.stringify(partsAnswer())).toString('base64')});h.send('end',{id:request.id});
  assert.deepEqual(await (await pending).json(),partsAnswer());
  await assert.rejects(h.host('/api/owner/connected/setup/state',{generation:id(8)}),'the parts read takes no input');
});
test('the parts view\'s styles use the existing colour names and no colour value',async()=>{
  const css=(await read('workspace.css')).replace(/\/\*[\s\S]*?\*\//g,'');
  const rules=[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([,selectors])=>/\.(?:workspace-parts|parts-list|part)\b|\.part-/.test(selectors));
  assert.ok(rules.length>=4,'the parts list and its states have rules');
  for(const [,selectors,body] of rules)assert.doesNotMatch(body,/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i,selectors.trim());
  assert.ok(rules.some(([selectors])=>/data-state/.test(selectors)),'each state has its mark');
});

// Gate review of document 03, A: one failed refresh of the parts read never takes the working form away.
test('on a bridged working page a refresh that is lost, refused or unusable keeps the working form and the room',async t=>{
  const url=landingRefs[0].endpoint_origin+'/api/owner/connected/setup/state';
  for(const [name,answer] of [['lost',()=>Promise.reject(new Error('lost'))],['refused',()=>reply(404,{})],
    ['unusable',()=>reply(200,{version:1,computer:'somewhere'})]]){
    const w=await workingPage(t),{l}=w,parts=w.parts(),posts=l.frame.sizes().length;
    l.h.overrides.set(url,answer);
    l.frame.view.listeners.get('focus')();await flush();
    assert.equal(stateCalls(l.h).length,2,`${name}: the refresh was sent`);
    assert.equal(setupMarker(l),undefined,name);assert.equal(l.rail.dataset.setupFocus,'shared',name);
    assert.equal(parts.hidden,false,name);assert.equal(l.panel(),undefined,name);
    assert.equal(l.frame.sizes().length,posts,`${name}: no new height`);
    assert.deepEqual(formsPosted(l),['setup','working'],`${name}: no form change`);
    l.workspace.dispose();
  }
});
// B: a session check is like a renewal when it ends ready on the computer the answer came from.
const readyStatus=(binding_generation=id(6),host='host_'+'a'.repeat(32))=>({version:1,state:'ready',expires_in:900,
  read_profile:{version:1,profile:'direct-owner-read-v1',host_uid:host},session_context:{issuer:documentContext.shell_origin,
  subject:documentContext.user_id,workspace_id:documentContext.workspace_id,operational_origin:documentContext.operational_origin,
  binding_revision:1,binding_generation}});
test('a session check that ends ready on the same computer keeps the working form and its height',async t=>{
  const w=await workingPage(t),{l}=w,parts=w.parts(),posts=l.frame.sizes().length,url=landingRefs[0].endpoint_origin+'/api/owner/connected/status';
  const gate=deferred(),forms=[];
  l.h.overrides.set(url,init=>gate.promise.then(()=>{l.h.overrides.delete(url);return l.h.dependencies.fetch(url,init);}));
  const checking=l.workspace.act(0,'check');await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'checking');
  forms.push(setupMarker(l),l.rail.dataset.setupFocus,parts.hidden);
  gate.resolve();await checking;await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  forms.push(setupMarker(l),l.rail.dataset.setupFocus,parts.hidden);
  assert.deepEqual(forms,[undefined,'shared',false,undefined,'shared',false],'the marker never comes back during the check');
  assert.equal(l.frame.sizes().length,posts,'no height posted');assert.equal(stateCalls(l.h).length,1,'no new parts read');
  assert.deepEqual(formsPosted(l),['setup','working'],'no form change');
  assert.equal(l.panel(),undefined);
});
test('a session check that ends anywhere but ready on the same computer brings back the setup form',async t=>{
  const url=landingRefs[0].endpoint_origin+'/api/owner/connected/status';
  const setupOnly={version:1,state:'setup_only',expires_in:900,setup_profile:{version:2,profile:'first-team-setup-v2',host_uid:'host_'+'a'.repeat(32)},
    session_context:readyStatus().session_context};
  for(const [name,answer] of [['denied',()=>reply(403,{state:'denied'})],['setup_only',()=>reply(200,setupOnly)],
    ['another binding',()=>reply(200,readyStatus(id(8)))],['another computer',()=>reply(200,readyStatus(id(6),'host_'+'d'.repeat(32)))]]){
    const {l}=await workingPage(t);
    l.h.overrides.set(url,answer);await l.workspace.act(0,'check');await flush();
    assert.notEqual(l.workspace.snapshot().computers[0].state,'ready',name);
    assert.equal(setupMarker(l),'setup',name);assert.notEqual(l.rail.dataset.setupFocus,'shared',name);
    assert.deepEqual(formsPosted(l),['setup','working','setup'],name);
    l.workspace.dispose();
  }
});
// C: the working form's computer already has a team, so its first-team panel is not offered anywhere.
test('in the working form the first-team panel is not offered, not even in the card, and comes back with the setup form',async t=>{
  const w=await workingPage(t),{l}=w,card=l.cards()[0],isPanel=n=>n.className==='setup-focus-panel';
  assert.equal(setupMarker(l),undefined);
  assert.equal(descendants(card).some(isPanel),false,'not in the card\'s disclosure');
  assert.equal(descendants(l.host).some(isPanel),false,'not in the setup host');
  assert.equal(descendants(card).some(n=>n.tagName==='BUTTON'&&['Check setup approval','Preview team','Launch team'].includes(n.textContent)),false);
  await l.workspace.act(0,'logout');await flush();
  assert.equal(setupMarker(l),'setup','a lost connection brings back the setup form');
  assert.ok(l.panel(),'and its first-team panel');
  assert.deepEqual(formsPosted(l),['setup','working','setup']);
});
// 41-07: a page left idle after a healthy Launch. No input reaches the frame, so the renewal window passes without a
// renewal and the 900-second session runs out. The working form stays, with its card's Connect as the way back.
const idleUntilExpiry=async l=>{
  l.h.time=780000;for(const timer of l.h.timers.filter(timer=>timer.active&&timer.ms===780000)){timer.active=false;timer.fn();}await flush();
  assert.equal(l.h.calls.filter(c=>c.url.endsWith('/renew')).length,0,'an idle page is not renewed');
  l.h.time=900000;l.h.timer(900000);await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'unavailable','the session ran out');
};
test('a healthy launched team left idle until its session runs out keeps the working form, and Connect brings it back',async t=>{
  const {l}=await bridgedLaunch(t,cleanDone),a=landingRefs[0].endpoint_origin,posts=l.frame.sizes().length;
  assert.equal(setupMarker(l),undefined);assert.deepEqual(formsPosted(l),['setup','working']);
  await idleUntilExpiry(l);
  assert.equal(setupMarker(l),undefined,'the working form stays');assert.equal(l.panel(),undefined,'the finished panel does not come back');
  assert.deepEqual(formsPosted(l),['setup','working']);assert.equal(l.frame.sizes().length,posts,'no new height');
  const card=l.cards()[0],primary=card.children[1].children[0];
  assert.ok(visible(card));assert.match(card.children[0].textContent,/Session expired$/);
  assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
  l.h.overrides.delete(a+'/api/owner/connected/issue'); // this time the computer issues a reading session
  primary.click();await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  assert.equal(partsOf(l.host)?.hidden,false,'the new session\'s parts read answers');
  assert.equal(setupMarker(l),undefined);assert.equal(l.rail.dataset.setupFocus,'shared');
  assert.deepEqual(formsPosted(l),['setup','working'],'the form never changed, not even while connecting');
  assert.equal(l.frame.sizes().length,posts);assert.equal(l.h.calls.filter(c=>c.url.endsWith('/setup/launch')).length,1,'never a second Launch');
});
test('after a lapse the next parts answer decides the form: a computer with no team goes back to the setup form',async t=>{
  const w=await workingPage(t),{l}=w,a=landingRefs[0].endpoint_origin;
  await idleUntilExpiry(l);
  assert.equal(setupMarker(l),undefined);
  l.h.overrides.set(a+'/api/owner/connected/setup/state',()=>reply(200,partsAnswer({teams:0})));
  await l.workspace.act(0,'connect');await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready');
  assert.equal(setupMarker(l),'setup');assert.deepEqual(formsPosted(l),['setup','working','setup']);
});
test('on a bridged working page a renewal whose answer is lost keeps the working form and offers Check connection',async t=>{
  const {l}=await workingPage(t),renew=landingRefs[0].endpoint_origin+'/api/owner/connected/renew',posts=l.frame.sizes().length;
  l.h.overrides.set(renew,()=>Promise.reject(new Error('lost')));
  assert.equal(await l.workspace.act(0,'renew'),false);await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'unavailable');
  assert.equal(setupMarker(l),undefined);assert.deepEqual(formsPosted(l),['setup','working']);assert.equal(l.panel(),undefined);
  assert.equal(l.frame.sizes().length,posts,'no new height');
  const primary=l.cards()[0].children[1].children[0];
  assert.equal(primary.textContent,'Check connection');assert.equal(primary.disabled,false);
  primary.click();await flush();
  assert.equal(l.workspace.snapshot().computers[0].state,'ready','the session the renewal kept is still good');
  assert.equal(setupMarker(l),undefined);assert.deepEqual(formsPosted(l),['setup','working']);
});
for(const [name,answer,offer] of [['a refusal',()=>reply(403,{state:'denied'}),'Connect'],['a lost answer',()=>Promise.reject(new Error('lost')),'Check connection']])
  test(`after a clean Launch a session check ending in ${name} brings back the setup form with its Connect step offered again`,async t=>{
    const {l,panel}=await bridgedLaunch(t,cleanDone);
    l.h.overrides.set(landingRefs[0].endpoint_origin+'/api/owner/connected/status',answer);
    await l.workspace.act(0,'check');await flush();
    assert.notEqual(l.workspace.snapshot().computers[0].state,'ready');
    assert.equal(setupMarker(l),'setup','a session check that ends anywhere but ready still brings back the setup form');
    assert.equal(l.panel(),panel);
    assert.deepEqual(stepShape(panel)[0],['connect','current'],'the Connect step is not ticked while this computer is not connected');
    assert.ok(offered(panel,offer),'a way back inside the frame');
    assert.equal(descendants(panel).some(n=>n.textContent==='Connect so you can finish setup.'&&visible(n)),false,
      'no setup is left to finish once the team is launched');
    assert.ok(descendants(stepsOf(panel).at(-1)).some(n=>n.textContent==='Team launched'&&visible(n)),'the Launch stays done');
  });
// An automatic renewal whose answer is lost keeps the working form, as Keep connected does: the transport reports
// unavailable before the renewal's outcome is recorded, and the renewal under way holds the lapse until it is.
for(const [how,answer] of [['rejected',()=>Promise.reject(new Error('lost'))],['answered 503',()=>reply(503,{state:'unavailable'})]])
  test(`on a working page an automatic renewal ${how} keeps the working form and offers Check connection`,async t=>{
    const {l}=await workingPage(t),a=landingRefs[0].endpoint_origin,posts=l.frame.sizes().length;
    l.h.overrides.set(a+'/api/owner/connected/renew',answer);
    l.h.time=780000;for(const timer of l.h.timers.filter(timer=>timer.active&&timer.ms===780000)){timer.active=false;timer.fn();}
    await flush();l.h.ui.listeners.get('pointerdown')({isTrusted:true});l.h.timer(0);await flush();
    assert.equal(l.h.calls.filter(c=>c.url.endsWith('/renew')).length,1,'one automatic renewal');
    assert.equal(l.workspace.snapshot().computers[0].state,'unavailable');
    assert.equal(setupMarker(l),undefined);assert.deepEqual(formsPosted(l),['setup','working'],'no form change');
    assert.equal(l.panel(),undefined);assert.equal(l.frame.sizes().length,posts,'no new height');
    const primary=l.cards()[0].children[1].children[0];
    assert.equal(primary.textContent,'Check connection');assert.equal(primary.disabled,false);
  });
// A renewal the computer refused is an answer, not a lapse: the setup form comes back whatever the follow-up check says.
for(const [how,status] of [['refused',()=>reply(403,{state:'denied'})],['lost',()=>Promise.reject(new Error('lost'))]])
  for(const automatic of [false,true])
    test(`on a working page a ${automatic?'automatic':'Keep connected'} renewal refused, then a status check ${how}, brings back the setup form`,async t=>{
      const {l}=await workingPage(t),a=landingRefs[0].endpoint_origin;
      l.h.overrides.set(a+'/api/owner/connected/renew',()=>reply(403,{state:'denied'}));
      l.h.overrides.set(a+'/api/owner/connected/status',status);
      if(automatic){
        l.h.time=780000;for(const timer of l.h.timers.filter(timer=>timer.active&&timer.ms===780000)){timer.active=false;timer.fn();}
        await flush();l.h.ui.listeners.get('pointerdown')({isTrusted:true});l.h.timer(0);
      }else assert.equal(await l.workspace.act(0,'renew'),false);
      await flush();
      assert.equal(l.h.calls.filter(c=>c.url.endsWith('/renew')).length,1,'one renewal was asked for');
      assert.notEqual(l.workspace.snapshot().computers[0].state,'ready');
      assert.equal(setupMarker(l),'setup','the refusal ends the team view');
      assert.deepEqual(formsPosted(l),['setup','working','setup']);
      assert.match(l.cards()[0].children[0].textContent,/Access denied$/);
    });
// A Connect from a lapsed page that reaches another computer or binding at the same address cannot bring the team back.
for(const [name,change] of [['another computer',{host_uid:'host_'+'d'.repeat(32)}],['a new binding',{binding_generation:id(8)}]])
  test(`a Connect from a lapsed working page whose challenge comes from ${name} brings back the setup form`,async t=>{
    const {l}=await workingPage(t),a=landingRefs[0].endpoint_origin,url=a+'/api/owner/connected/challenge';
    await idleUntilExpiry(l);
    assert.equal(setupMarker(l),undefined);
    const issued=l.h.calls.filter(c=>c.url.endsWith('/issue')).length;
    l.h.overrides.set(url,init=>{l.h.overrides.delete(url);return l.h.dependencies.fetch(url,init).then(r=>r.json()).then(c=>reply(202,{...c,...change}));});
    await l.workspace.act(0,'connect');await flush();
    assert.equal(l.h.calls.filter(c=>c.url.endsWith('/issue')).length,issued,'no session is issued');
    assert.notEqual(l.workspace.snapshot().computers[0].state,'ready');
    assert.equal(setupMarker(l),'setup');assert.deepEqual(formsPosted(l),['setup','working','setup']);
    // This page cannot reach that computer again, so nothing offers a Connect or check that would do nothing.
    const panel=l.panel(),shown=text=>descendants(panel).some(n=>n.textContent===text&&visible(n));
    assert.ok(panel,'the setup form shows its panel');
    assert.equal(offered(panel,'Check connection')||offered(panel,'Connect'),false,'no way back that cannot work');
    assert.ok(shown('Your account or saved computers changed. Reopen setup from your workspace.'),'what to do instead');
    assert.equal(shown('The connection to this computer could not be confirmed. Choose Check connection to look again.'),false);
    assert.equal(l.cards()[0].children[1].children[0].disabled,true,'nor in the card');
    const calls=l.h.calls.length;
    await l.workspace.act(0,'check');await l.workspace.act(0,'connect');await flush();
    assert.equal(l.h.calls.length,calls,'and nothing is sent');
  });

// H: the frame tells the page which form it shows: once after ready, then only on a change; nothing else crosses.
test('the framed view posts its form to the exact shell origin: setup at mount, working on entering, setup on leaving',async t=>{
  const w=await workingPage(t),{l}=w,kinds=l.frame.posts.map(([message])=>message.type);
  assert.deepEqual(kinds.slice(0,2),['claudfather.workspace.ready','claudfather.workspace.form'],'the form follows the ready message');
  assert.deepEqual(formsPosted(l),['setup','working']);
  for(const [message,origin] of l.frame.posts.filter(([message])=>message.type==='claudfather.workspace.form')){
    assert.deepEqual(Object.keys(message),['type','version','form'],'exactly these keys');
    assert.equal(message.version,1);assert.equal(origin,documentContext.shell_origin,'never a wildcard target');
  }
  l.frame.view.listeners.get('focus')();await flush();l.frame.resize(700);
  assert.deepEqual(formsPosted(l),['setup','working'],'an unchanged form is not posted again');
  await l.workspace.act(0,'logout');await flush();
  assert.deepEqual(formsPosted(l),['setup','working','setup']);
  l.workspace.dispose();await l.workspace.checkAccount();await flush();
  assert.deepEqual(formsPosted(l),['setup','working','setup'],'nothing after the view closes');
});
// The team card carried the messages mark (04a with 04b1): Launch sends the computer's read-back goal once.
const GOAL_PAGE="Launch sends this goal to your team's lead as your first message, once the team is running. Keep this page open until then. A sent goal cannot be unsent.";
const GOAL_COMPUTER="Launch sends this goal to your team's lead as your first message, once the team is running. You can close this page. A sent goal cannot be unsent.";
const SENT_AFTER="Team launched. Once its bots show as running, you can message the lead here.";
function markedHarness(goal,{finish='page',marked=true}={}){
  const h=setupHarness({client:{async setupPreview(input){h.calls.push(['preview',input]);
    return marked?{...h.preview,sends_goal:true,finish,goal:input.goal}:h.preview;}}});
  if(marked)h.context.sends_goal=true;
  const ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  const launchBody=stepsOf(panel).find(n=>n.dataset.step==='launch').children.find(n=>n.className==='setup-step-body');
  const goalNode=descendants(panel).find(n=>n.className==='setup-goal');
  return {h,panel,launchBody,goalNode,async ready(){await h.api.run('context');h.api.setGoal(goal);await h.api.run('preview');}};
}
test('a marked card shows the computer\'s goal and S24 directly above Launch team, and S17 after Launch',async()=>{
  const m=markedHarness('Add a contact form to the homepage');await m.ready();
  const order=m.launchBody.children;
  const launchRow=order.findIndex(n=>descendants(n).some(c=>c.tagName==='BUTTON'&&c.textContent==='Launch team'));
  assert.equal(m.goalNode.textContent,'Add a contact form to the homepage');assert.ok(visible(m.goalNode));
  assert.equal(order[launchRow-2],m.goalNode,'the goal sits just above the note');
  assert.equal(order[launchRow-1].textContent,GOAL_PAGE,'S24, the page form, directly above Launch team');
  await m.h.api.run('launch');await m.h.api.run('receipt');
  assert.equal(m.h.api.snapshot().note,SENT_AFTER);
  assert.equal(m.goalNode.textContent,'','once launched, the goal above Launch is gone');
  m.h.api.dispose();
});
test('the computer form of S24 shows only when the Preview says the computer finishes',async()=>{
  const m=markedHarness('Review the checkout tests',{finish:'computer'});await m.ready();
  assert.ok(descendants(m.launchBody).some(n=>n.textContent===GOAL_COMPUTER&&visible(n)));
  assert.ok(!descendants(m.launchBody).some(n=>n.textContent===GOAL_PAGE));
  m.h.api.dispose();
});
test('an unmarked card shows no goal above Launch and keeps today\'s line after Launch',async()=>{
  const m=markedHarness('Review the checkout tests',{marked:false});await m.ready();
  assert.equal(m.goalNode.textContent,'');
  assert.ok(!descendants(m.panel).some(n=>[GOAL_PAGE,GOAL_COMPUTER].includes(n.textContent)));
  await m.h.api.run('launch');await m.h.api.run('receipt');
  assert.match(m.h.api.snapshot().note,/message the lead with one concrete request; the setup goal alone does not start a task/);
  m.h.api.dispose();
});
test('a Preview answer whose goal marks disagree with the approval is not used',async()=>{
  for(const [context,answer] of [[true,{}],[false,{sends_goal:true,finish:'page',goal:'Ship it'}],
      [true,{sends_goal:true,finish:'later',goal:'Ship it'}],[true,{sends_goal:true,finish:'page',goal:'Another goal'}],
      [false,{finish:'page'}]]){
    const h=setupHarness({client:{async setupPreview(){return {...h.preview,...answer};}}});
    if(context)h.context.sends_goal=true;
    await h.api.run('context');h.api.setGoal('Ship it');
    await h.api.run('preview').catch(()=>{});
    assert.equal(h.api.snapshot().plan,null,JSON.stringify([context,answer]));h.api.dispose();
  }
});
test('person-written text in the setup view is drawn as plain text: the goal above Launch',async()=>{
  const hostile='<img src=x onerror=1><a href="https://example.invalid">link</a><form><input></form>';
  const m=markedHarness(hostile);await m.ready();
  assert.equal(m.goalNode.textContent,hostile,'the goal is its literal text');
  assert.deepEqual(m.goalNode.children,[],'and never parsed into elements');
  const source=await readFile(new URL('../claudlobby/plane/ui/guided-setup.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/innerHTML|outerHTML|insertAdjacentHTML|document\.write/,'the setup view writes no markup');
  m.h.api.dispose();
});

// The sign-in step on its own (claude-signin.js), driven the way a second road drives it: no page around it,
// the four calls from a real client, and no storage.
const {createClaudeSignin,NO_STORAGE}=await import(new URL('../claudlobby/plane/ui/claude-signin.js',import.meta.url));
const linkApproval={generation:id(9),expires_at:1800003600,provider_signin:true};
const linkHost=(extra={})=>({approval:()=>linkApproval,pending:()=>false,busy:()=>false,busyOperation:()=>null,active:()=>true,
  disposed:()=>false,epoch:()=>0,changed:()=>{},run:async()=>true,...extra});
test('link road writes nothing to storage',async()=>{
  const touched=[],spy={getItem:key=>{touched.push(['get',key]);return null;},setItem:key=>{touched.push(['set',key]);}};
  const restore=stubGlobal('localStorage',{value:spy});
  try {
    const answers=['waiting','waiting','connected'],seen=[];
    const calls=async(action,input)=>{seen.push(action);return {generation:input.generation,attempt_id:input.attempt_id,state:answers.shift()??'connected',reason:null,
      ...(action==='begin'?{url:authorizeURL}:{})};};
    const signin=createClaudeSignin({calls,store:NO_STORAGE,host:linkHost(),now:()=>1800000000,copyText:async()=>{}});
    signin.load();assert.equal(signin.attempt(),null,'nothing is read back');
    assert.equal(await signin.operate('signin-begin',0),true);
    assert.equal(signin.state(),'waiting');assert.ok(signin.attempt());
    assert.equal(await signin.operate('signin-status',0),true);
    assert.equal(await signin.operate('signin-status',0),true);assert.equal(signin.state(),'connected');
    assert.deepEqual(seen,['begin','status','status']);
    assert.deepEqual(touched,[],'no read and no write reached browser storage');
    const again=createClaudeSignin({calls,store:NO_STORAGE,host:linkHost(),now:()=>1800000000,copyText:async()=>{}});
    again.load();assert.equal(again.attempt(),null,'a second page remembers no attempt');
    // A store that refuses writes cannot stop a road that keeps nothing; the same refusal stops a road that keeps an attempt.
    const refusing={read:()=>null,write:()=>{throw new Error('full');}};
    const keeps=createClaudeSignin({calls,store:refusing,host:linkHost(),now:()=>1800000000,copyText:async()=>{}});
    await assert.rejects(keeps.operate('signin-begin',0),error=>error.code==='signin_not_started');
    const source=await readFile(new URL('../claudlobby/plane/ui/claude-signin.js',import.meta.url),'utf8');
    assert.doesNotMatch(source,/localStorage|sessionStorage|innerHTML|outerHTML|insertAdjacentHTML|document\.write/,'the step names no storage of its own and writes no markup');
  } finally { restore(); }
});
test('no renewal during a link sign-in',async()=>{
  const reference={reference_id:id(4),endpoint_origin:'https://a.example.test',revision:1};
  const doc={issuer:'https://accounts.example.test',operational_origin:'https://plane.example.test',subject:id(1),workspace_id:id(2),context_generation:id(3)};
  const binding={binding_revision:1,binding_generation:id(5)},HOST='host_'+'a'.repeat(32),TOKEN='B'.repeat(42)+'A',ROTATED='C'.repeat(42)+'A';
  const ready=token=>({version:1,state:'ready',expires_in:900,read_profile:{version:1,profile:'direct-owner-read-v1',host_uid:HOST},
    session_context:{issuer:doc.issuer,subject:doc.subject,workspace_id:doc.workspace_id,operational_origin:doc.operational_origin,...binding},...(token?{token}:{})});
  const seen=[];let time=0,state='waiting';
  const fetch=async(url,init)=>{
    const path=new URL(url).pathname.replace('/api/owner/connected/','');seen.push(path);
    if(path==='challenge')return reply(202,{version:1,state:'awaiting_session_assertion',nonce:'A'.repeat(43),issuer:doc.issuer,host_uid:HOST,operational_origin:doc.operational_origin,...binding,expires_at:1800000120});
    if(path==='issue')return reply(200,ready(TOKEN));
    if(path==='renew')return reply(200,ready(ROTATED));
    if(path==='status')return reply(200,ready());
    if(path.startsWith('setup/signin-'))return reply(200,{generation:id(9),attempt_id:JSON.parse(init.body).attempt_id,state,reason:null,...(state==='waiting'?{url:authorizeURL}:{})});
    return reply(404,{});
  };
  const client=createConnectedReadClient({reference,documentContext:doc,documentOrigin:doc.operational_origin,fetch,monotonicNow:()=>time,
    currentContext:()=>context([reference]),setTimeout:(fn,ms)=>({fn,ms}),clearTimeout:()=>{},
    obtainSessionAssertion:async()=>({version:1,assertion:'e30.e30.AAAA',expires_at:1800000120})});
  const renewals=()=>seen.filter(path=>path==='renew').length;
  assert.equal(await client.connect(),true);
  const signin=createClaudeSignin({calls:(action,input)=>client.setupSignin(action,input),store:NO_STORAGE,host:linkHost(),now:()=>1800000000,copyText:async()=>{}});
  assert.equal(await signin.operate('signin-begin',0),true);assert.equal(signin.state(),'waiting');
  assert.equal(await client.renew(),false,'an explicit renewal waits for the sign-in');assert.equal(renewals(),0);
  time=780000;client.activity({visible:true,userInput:true});await flush();
  assert.equal(renewals(),0,'nor does the session renew by itself while the sign-in is live');
  assert.equal(await signin.operate('signin-status',0),true);assert.equal(await client.renew(),false);assert.equal(renewals(),0,'a status that is not the end releases nothing');
  state='connected';assert.equal(await signin.operate('signin-status',0),true);assert.equal(signin.state(),'connected');
  assert.equal(await client.renew(),true);assert.equal(renewals(),1,'the end of the sign-in releases the renewal');
  signin.dispose();client.dispose();
});
