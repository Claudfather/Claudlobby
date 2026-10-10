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
const enrollmentURL = uri(await read('workspace-enrollment.js'));
const workspaceText = await read('connected-workspace.js');
const workspaceURL = uri(workspaceText.replace('"./workspace-enrollment.js"',JSON.stringify(enrollmentURL)).replace('"./connected-read-client.js"',JSON.stringify(carrierURL)).replace('"./two-source-read.js"',JSON.stringify(adapterURL)));
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
function dom() {
  const elements=new Map(),observers=[],listeners=new Map();
  const document={visibilityState:'visible',activeElement:null,
    getElementById:id=>elements.get(id),createElement:name=>new Element(name),
    querySelector:q=>q==='main'?elements.get('main'):null,
    querySelectorAll:q=>{
      assert.ok(['#view-nav button, [data-bot-inspect]', '#view-nav button, #debug-toggle, [data-bot-inspect]'].includes(q), 'selector must be modelled explicitly');
      return [...nav,...(q.includes('#debug-toggle')?[elements.get('debug-toggle')]:[]),...(elements.get('fleet').children||[]).filter(e=>e.dataset.botInspect)];
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
  for(const id of ['owner-session','main','fleet','fleet-totals','presence-strip','task-detail-content','focus-pane','task-detail','focus-overlay','focus-title','debug-toggle','search'])elements.set(id,new Element());
  const nav=['channel','grid','fleet','trust'].map(view=>{const e=new Element('button');e.dataset.view=view;return e;});
  class Observer {constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){this.closed=true;}}
  return {document,Element,nav,observers,listeners,get:id=>elements.get(id),MutationObserver:Observer};
}
function harness(options={}) {
  const count=options.count??2,doc={...documentContext,references:options.references??refs.slice(0,count)},calls=[],timers=[],streams=[],overrides=new Map();
  let metadata=context(doc.references),metadataStatus=200;
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
    now:()=>1800000000,setTimeout(fn,ms){const t={fn,ms,active:true};timers.push(t);return t;},clearTimeout(t){if(t)t.active=false;},MutationObserver:ui.MutationObserver,...options.dependencies};
  const api=createConnectedWorkspace(dependencies);
  return {api,doc,ui,calls,timers,streams,overrides,dependencies,grants,receipts,
    get metadata(){return metadata;},set metadata(v){metadata=v;},set metadataStatus(v){metadataStatus=v;},
    mount(callbacks={}){return api.mountSessionControls({document:ui.document,element:ui.get('owner-session'),...callbacks});},
    timer(ms){const t=timers.find(t=>t.active&&t.ms===ms);assert.ok(t);t.active=false;t.fn();}};
}

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
  const h=harness();let body;h.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2),()=>new Response(new ReadableStream({start(c){body=c;}}),{headers:{'Content-Type':'application/json'}}));
  const checking=h.api.checkAccount();await flush();h.timer(8000);assert.equal(await checking,false);assert.equal(h.api.snapshot().phase,'unavailable');assert.equal(h.calls.length,1);h.api.dispose();
  const late=harness(),held=deferred();late.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2),()=>held.promise);
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
  for(const n of h.ui.nav)assert.equal(n.disabled,n.dataset.view!=='channel');assert.equal(h.ui.get('search').disabled,true);
  const root=h.ui.get('owner-session'),row=root.children[2],check=root.children[1];check.focus();await h.api.checkAccount();assert.equal(h.ui.document.activeElement,check);assert.equal(root.children[1],check);
  const connect=row.children[1];assert.equal(connect.disabled,false);connect.click();await flush();assert.equal(h.api.snapshot().computers[0].state,'ready');assert.equal(row.children[1],connect);
  const inspect=new h.ui.Element('button');inspect.dataset.botInspect='actor';h.ui.get('fleet').append(inspect);h.ui.observers[0].fn();assert.equal(inspect.disabled,true);assert.match(inspect.title,/Activity and task records/);
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
  const root=h.ui.get('owner-session'),check=root.children[1];check.focus();
  await h.api.checkAccount();assert.equal(root.children[1],check);assert.equal(check.isConnected,true);
  assert.equal(h.ui.document.activeElement,check);h.api.dispose();
});


test('first-use approval uses enrollment purpose only, preserves peer reads and requires later explicit Connect',async()=>{
 const h=harness();h.mount();await h.api.checkAccount();await flush();await h.api.act(1,'connect');
 const before=h.calls.length;assert.equal(await h.api.act(0,'enroll'),true);
 const calls=h.calls.slice(before);assert.deepEqual(calls.map(c=>new URL(c.url).pathname),['/api/plane/context','/api/owner/workspace/challenge','/api/plane/host-assertion','/api/owner/workspace/stage']);
 const signer=calls.find(c=>c.url.endsWith('/host-assertion'));assert.equal(signer.init.credentials,'include');assert.deepEqual(Object.keys(JSON.parse(signer.init.body)).sort(),['host_uid','nonce','operational_origin','workspace_id']);
 assert.notEqual(h.api.snapshot().computers[0].state,'ready');assert.equal(h.api.snapshot().computers[1].state,'ready');
 const root=h.ui.get('owner-session'),first=root.children[2],proposal=first.children.at(-1);
 assert.ok(proposal.children.some(c=>c.textContent.includes('Approval code:')));assert.equal((await h.api.jget('/api/overview')).data.coverage.reachable,1);
 assert.equal(await h.api.act(0,'enroll'),false);assert.equal(h.calls.filter(c=>c.url.endsWith('/workspace/stage')).length,1);
 // Synthetic host fixture now represents local approval; UI itself has no confirm door.
 assert.equal(await h.api.act(0,'connect'),true);assert.ok(!proposal.children.some(c=>c.textContent.includes('Approval code:')));
 const count=h.calls.length;assert.equal(await h.api.act(0,'enroll'),false);assert.equal(h.calls.length,count);h.api.dispose();
});

test('account loss during host staging erases approval code and fences late staged result',async()=>{
 const h=harness();h.mount();await h.api.checkAccount();await flush();const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/workspace/stage',()=>held.promise);
 const enroll=h.api.act(0,'enroll');await flush();h.metadataStatus=403;await h.api.checkAccount();held.resolve(reply(202,{version:1,state:'awaiting_local_approval',expires_at:1800000120}));
 assert.equal(await enroll,false);assert.equal(h.api.snapshot().phase,'denied');const panel=h.ui.get('owner-session').children[2].children.at(-1);assert.ok(!panel.children.some(c=>c.textContent.includes('Approval code:')));
 assert.equal(h.calls.filter(c=>c.url.endsWith('/issue')).length,0);h.api.dispose();
});

test('forgetting approval returns keyboard focus to that computer, including an in-flight request',async()=>{
 for(const busy of [false,true]){
  const h=harness();h.mount();await h.api.checkAccount();await flush();const held=deferred();
  if(busy)h.overrides.set(refs[0].endpoint_origin+'/api/owner/workspace/stage',()=>held.promise);
  const pending=h.api.act(0,'enroll');if(busy)await flush();else await pending;
  const row=h.ui.get('owner-session').children[2],proposal=row.children.at(-1),cancel=proposal.children.at(-1);
  cancel.focus();cancel.click();assert.ok([row,row.children[5]].includes(h.ui.document.activeElement));
  assert.ok(!proposal.children.some(c=>c.textContent.includes('Approval code:')));
  if(busy)held.resolve(reply(202,{version:1,state:'awaiting_local_approval',expires_at:1800000120}));await pending;
  assert.equal(h.calls.filter(c=>c.url.endsWith('/workspace/stage')).length,1);h.api.dispose();
 }
});

test('temporary account loading is aria-disabled and activation guarded without blurring focus',async()=>{
  const h=harness();h.mount();await h.api.checkAccount();await flush();
  const check=h.ui.get('owner-session').children[1];check.focus();
  const held=deferred();h.overrides.set(documentContext.shell_origin+'/api/plane/context?workspace_id='+id(2),()=>held.promise);
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
  const buttons=h.ui.get('owner-session').children[2].children;
  assert.equal(buttons[3].disabled,true);assert.equal(buttons[4].disabled,false);
  buttons[4].click();await flush();assert.equal(h.calls.filter(c=>c.url.endsWith('/logout')).length,1);
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
  assert.equal(await h.api.nudgeContext(sourceA+' / team'),null);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/status')).length-statusBefore,1);
  assert.equal(h.api.snapshot().computers[0].state,'ready');assert.equal((await h.api.jget('/api/overview')).data.coverage.reachable,2);h.api.dispose();
});

test('late send through renewal remains unknown with original UUID; restored receipt is pure and no mutation replays',async()=>{
  const h=harness({count:1});await h.api.checkAccount();await flush();await h.api.act(0,'connect');await h.api.jget('/api/overview');
  const context=await h.api.interactionContext(sourceA+' / team'),state=new ActionState({getItem:()=>null,setItem(){}});
  const request=state.begin(context,'message',{recipient:context.recipients[0].id,task_id:null},'Keep me',id(40));
  const held=deferred();h.overrides.set(refs[0].endpoint_origin+'/api/owner/connected/actions/send',()=>held.promise);
  const send=h.api.sendAction(request);await flush();await h.api.act(0,'renew');
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
