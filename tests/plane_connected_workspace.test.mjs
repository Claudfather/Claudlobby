import assert from 'node:assert/strict';
import test from 'node:test';
import {ActionState} from '../claudlobby/plane/ui/action-state.js';
import {readFile} from 'node:fs/promises';
const read = name => readFile(new URL('../claudlobby/plane/ui/'+name,import.meta.url),'utf8');
const uri = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const ownerURL = uri(await read('owner-api-client.js'));
const actionsURL = uri((await read('connected-actions.js')).replace('"./action-state.js"',JSON.stringify(new URL('../claudlobby/plane/ui/action-state.js',import.meta.url).href)));
const carrierURL = uri((await read('connected-read-client.js')).replace('"./owner-api-client.js"',JSON.stringify(ownerURL)).replace('"./connected-actions.js"',JSON.stringify(actionsURL)));
const adapterURL = uri((await read('two-source-read.js')).replace('"./owner-api-client.js"',JSON.stringify(ownerURL)));
const {createWorkspaceReadTransport}=await import(adapterURL);
const enrollmentURL = uri(await read('workspace-enrollment.js'));
const workspaceText = await read('connected-workspace.js');
const workspaceURL = uri(workspaceText.replace('"./guided-setup.js"',JSON.stringify(new URL('../claudlobby/plane/ui/guided-setup.js',import.meta.url).href)).replace('"./workspace-enrollment.js"',JSON.stringify(enrollmentURL)).replace('"./connected-read-client.js"',JSON.stringify(carrierURL)).replace('"./two-source-read.js"',JSON.stringify(adapterURL)));
const {createConnectedWorkspace} = await import(workspaceURL);
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
function dom() {
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
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){for(const c of this.children)c.isConnected=false;this.children=nodes;this.textContent='';}
    setAttribute(k,v){this.attributes[k]=v;}
    addEventListener(n,fn){this.handlers[n]=fn;}
    focus(){document.activeElement=this;}
    close(){this.closed=true;}
    click(){if(!this.disabled)this.handlers.click?.();}
  }
  for(const id of ['owner-session','main','fleet','channel','fleet-totals','presence-strip','task-detail-content','focus-pane','task-detail','focus-overlay','focus-title','debug-toggle','search'])elements.set(id,new Element());
  const nav=['channel','grid','fleet','trust'].map(view=>{const e=new Element('button');e.dataset.view=view;return e;});
  class Observer {constructor(fn){this.fn=fn;this.targets=[];observers.push(this);}observe(target){this.targets.push(target);}disconnect(){this.closed=true;}}
  return {document,Element,nav,observers,listeners,get:id=>elements.get(id),MutationObserver:Observer};
}
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
  const ui=dom();const dependencies={documentContext:doc,documentOrigin:doc.operational_origin,fetch,
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
  assert.match(rows[0].children[2].textContent,/outcome is unknown/);
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
  const h=harness();h.mount({onPause(){throw new Error('callback');},onResume(){throw new Error('callback');}});await h.api.checkAccount();await flush();await h.api.act(0,'connect');
  for(const name of ['main','fleet-totals','presence-strip','task-detail-content','focus-pane'])h.ui.get(name).append(h.ui.document.createElement('b'));
  const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/read/tasks',()=>held.promise);
  const old=h.api.jget('/api/tasks');await flush();h.metadataStatus=401;await h.api.checkAccount();
  held.resolve(reply(200,ok({tasks:[{task_id:wi,title:'late private'}]})));const result=await old;
  assert.equal(result.state,'denied');assert.equal(result.data,undefined);assert.equal(h.api.snapshot().phase,'denied');
  for(const name of ['main','fleet-totals','presence-strip','task-detail-content','focus-pane'])assert.equal(h.ui.get(name).children.length,0);
  assert.equal(h.ui.get('task-detail').closed,true);assert.equal(h.ui.get('focus-overlay').hidden,true);
  h.metadataStatus=200;assert.equal(await h.api.checkAccount(),false);assert.equal(await h.api.act(0,'connect'),false);h.api.dispose();
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
  for(const n of h.ui.nav)assert.equal(n.disabled,!['channel','grid'].includes(n.dataset.view));assert.equal(h.ui.get('search').disabled,true);
  const root=h.ui.get('owner-session'),row=root.children[1],check=root.children[0].children[1];check.focus();await h.api.checkAccount();assert.equal(h.ui.document.activeElement,check);assert.equal(root.children[0].children[1],check);
  const connect=row.children[1].children[0];assert.equal(connect.disabled,false);connect.click();await flush();assert.equal(h.api.snapshot().computers[0].state,'ready');assert.equal(row.children[1].children[0],connect);
  for(const id of ['fleet','channel']){
    const target=h.ui.get(id);assert.ok(h.ui.observers[0].targets.includes(target));
    const inspect=new h.ui.Element('button');inspect.dataset.botInspect='actor';target.append(inspect);h.ui.observers[0].fn();assert.equal(inspect.disabled,true);assert.match(inspect.title,/Activity and task records/);
  }
  h.api.dispose();assert.equal(h.ui.observers[0].closed,true);assert.equal(h.ui.listeners.has('visibilitychange'),false);assert.equal(h.calls.filter(c=>c.url.endsWith('/logout')).length,0);
});

test('api-client selects only an existing marker; absent marker preserves legacy same-origin reads and zero owner probes',async()=>{
  const original={document:globalThis.document,location:globalThis.location,fetch:globalThis.fetch,EventSource:globalThis.EventSource};
  const apiText=(await read('api-client.js')).replace('"./connected-workspace.js"',JSON.stringify(workspaceURL));
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
 const h=harness();h.mount();await h.api.checkAccount();await flush();
 const row=h.ui.get('owner-session').children[1],actions=row.children[1],primary=actions.children[0],help=row.children.at(-1);
 assert.equal(actions.children.length,1);assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
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
 const enroll=h.api.act(0,'enroll');await flush();h.metadataStatus=403;await h.api.checkAccount();held.resolve(reply(202,{version:1,state:'awaiting_local_approval',expires_at:1800000120}));
 assert.equal(await enroll,false);assert.equal(h.api.snapshot().phase,'denied');const panel=proposalOf(h.ui.get('owner-session').children[1]);assert.ok(!panel.children.some(c=>c.textContent.includes('Approval code:')));
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
  const nav=html.match(/<nav id="view-nav">([\s\S]*?)<\/nav>/)[1];
  assert.deepEqual([...nav.matchAll(/data-view="([^"]+)"/g)].map(m=>m[1]),['channel','grid','fleet','trust']);
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

test('setup-only computer stays visible without blocking the other admitted dashboard',async()=>{
  const h=harness(),a=refs[0].endpoint_origin;h.mount();
  h.overrides.set(a+'/api/owner/connected/issue',()=>reply(200,{version:1,state:'setup_only',token:'A'.repeat(43),expires_in:900,
    setup_profile:{version:1,profile:'first-team-setup-v1',host_uid:'host_'+'a'.repeat(32)},session_context:{issuer:documentContext.shell_origin,
      subject:documentContext.user_id,workspace_id:documentContext.workspace_id,operational_origin:documentContext.operational_origin,binding_revision:1,binding_generation:id(6)}}));
  await h.api.checkAccount(); await h.api.act(0,'connect'); await h.api.act(1,'connect');
  assert.equal(h.api.snapshot().computers[0].state,'setup_only');
  const fleets=await h.api.jget('/api/fleets');
  assert.equal(fleets.data.fleets.length,1); assert.ok(fleets.data.fleets[0].alias.includes(sourceB));
  assert.equal(h.calls.filter(c=>c.url.startsWith(a+'/api/owner/connected/read/')).length,0);
  const card=h.ui.get('owner-session').children[1],primary=card.children[1].children[0];
  assert.equal(primary.textContent,'Disconnect');assert.equal(primary.disabled,false);
  h.overrides.set(a+'/api/owner/connected/logout',init=>{assert.equal(init.headers.Authorization,'Bearer '+'A'.repeat(43));return reply(200,{version:1,state:'signed_out'});});
  assert.equal(await h.api.act(0,'logout'),true);
  assert.equal(primary.textContent,'Connect');assert.equal(primary.disabled,false);
  assert.equal(h.api.snapshot().computers[0].state,'signed_out');
  assert.equal(h.api.snapshot().computers[1].state,'ready');
  assert.equal(h.calls.filter(c=>c.url.startsWith(a+'/api/owner/connected/read/')).length,0);
  h.api.dispose();
});

const {createGuidedSetup}=await import(new URL('../claudlobby/plane/ui/guided-setup.js',import.meta.url));
function setupHarness(options={}) {
  const calls=[],listeners=new Set(),saved=new Map(); let state={state:'setup_only',epoch:1};
  const pending={generation:id(9),plan_id:'p-'+'a'.repeat(64),request_id:id(10)};
  const context={version:1,template:'revenue-web-app',fleet:'shop',repository:'example-org/shop',release_id:'r-'+'b'.repeat(64),release_seal:'c'.repeat(64),generation:id(9),expires_at:1800003600,request:null};
  const preview={...context,plan_id:pending.plan_id,fleets:['shop'],changes:[],effects:{restart_bots:['shop/lead'],reload_supervision:true},activation:'not_requested',expires_at:1800000600};
  const client={snapshot:()=>state,subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},
    async setupContext(){calls.push(['context']);return context;},
    async setupPreview(input){calls.push(['preview',input]);return preview;},
    async setupLaunch(input){assert.equal(saved.has('test'),true);calls.push(['launch',input]);throw new Error('lost');},
    async setupReceipt(input){calls.push(['receipt',input]);return {...input,version:1,state:'active',recorded_activation:{}};},...options.client};
  const storage={getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,value)};
  const api=createGuidedSetup({client,storage,storageKey:'test',newUUID:()=>id(10),now:()=>1800000000});
  return {api,client,calls,saved,pending,context,preview,storage,
    change(next){state={...state,...next};api.connectionChanged(state);for(const fn of listeners)fn(state);}};
}
test('goal preview and explicit exact-plan launch retain UUID before lost response; reload only inspects',async()=>{
  const h=setupHarness();assert.equal(h.calls.length,0);
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');
  h.api.setGoal('Changed goal');assert.equal(await h.api.run('launch'),false);assert.equal(h.calls.length,2);
  h.api.setGoal('Improve checkout tests');assert.equal(await h.api.run('launch'),false);
  assert.deepEqual(h.api.snapshot().pending,h.pending);assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);
  await h.api.run('launch');assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);
  h.api.dispose();
  const resumed=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.deepEqual(resumed.snapshot().pending,h.pending);assert.equal(h.calls.length,3);
  await resumed.run('receipt');assert.equal(h.calls.at(-1)[0],'receipt');assert.match(resumed.snapshot().note,/Team started/);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);resumed.dispose();
});
test('changed connection fences held preview, preserves goal and never launches',async()=>{
  const held=deferred(),h=setupHarness({client:{setupPreview:()=>held.promise}});
  await h.api.run('context');h.api.setGoal('Improve checkout tests');const reading=h.api.run('preview');
  h.change({epoch:2,state:'unavailable'});held.resolve(h.preview);assert.equal(await reading,false);
  assert.equal(h.api.snapshot().plan,null);assert.equal(h.api.snapshot().goal,'Improve checkout tests');
  assert.equal(await h.api.run('launch'),false);h.api.dispose();
});
test('setup panel retains disclosure/focus and refuses launch without durable identifiers',async()=>{
  const h=setupHarness(),ui=dom(),panel=new ui.Element();h.api.mountPanel({document:ui.document,element:panel});
  const detail=panel.children.find(e=>e.tagName==='DETAILS'); detail.open=true;detail.focus();
  await h.api.run('context');h.api.setGoal('Improve checkout tests');await h.api.run('preview');
  assert.equal(panel.children.find(e=>e.tagName==='DETAILS'),detail);assert.equal(detail.open,true);assert.equal(ui.document.activeElement,detail);
  assert.ok(panel.children.some(e=>e.textContent.includes('Team shop for example-org/shop')));
  h.storage.setItem=()=>{throw new Error('blocked');};assert.equal(await h.api.run('launch'),false);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,0);h.api.dispose();
});

test('canonical reserved request wins two-tab storage conflict, preserving both IDs through reload',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  const losing={...h.pending,request_id:id(11)};
  h.saved.set('test',JSON.stringify(losing));
  const restored=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  h.context.request={...h.pending};
  assert.equal(await restored.run('context'),true);
  assert.deepEqual(restored.snapshot().pending,h.pending);assert.deepEqual(restored.snapshot().conflicts,[losing]);
  await restored.run('receipt');assert.deepEqual(h.calls.at(-1),['receipt',h.pending]);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);
  const reloaded=createGuidedSetup({client:h.client,storage:h.storage,storageKey:'test',now:()=>1800000000});
  assert.deepEqual(reloaded.snapshot().pending,h.pending);assert.deepEqual(reloaded.snapshot().conflicts,[losing]);
  restored.dispose();reloaded.dispose();h.api.dispose();
});

test('only a new grant generation clears blocking pending; same-generation null never permits retry',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  assert.equal(await h.api.run('context'),true);assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.equal(await h.api.run('preview'),false);assert.equal(await h.api.run('launch'),false);
  h.context.generation=id(12);
  assert.equal(await h.api.run('context'),true);assert.equal(h.api.snapshot().pending,null);
  assert.deepEqual(h.api.snapshot().conflicts,[h.pending]);
  h.client.setupPreview=async input=>{h.calls.push(['preview',input]);return {...h.preview,generation:id(12)};};
  assert.equal(await h.api.run('preview'),true);assert.equal(h.api.snapshot().goal,'Improve tests');
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
});

test('expired or revoked browser receipt retains original identifiers with local inspection guidance',async()=>{
  const h=setupHarness();await h.api.run('context');h.api.setGoal('Improve tests');await h.api.run('preview');await h.api.run('launch');
  h.client.setupReceipt=async()=>{throw new Error('approval expired');};
  assert.equal(await h.api.run('receipt'),false);assert.deepEqual(h.api.snapshot().pending,h.pending);
  assert.match(h.api.snapshot().note,/locally after expiry\/revocation/);
  assert.equal(h.calls.filter(c=>c[0]==='launch').length,1);h.api.dispose();
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
