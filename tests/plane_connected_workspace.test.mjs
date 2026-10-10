import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
const read = name => readFile(new URL('../claudlobby/plane/ui/'+name,import.meta.url),'utf8');
const uri = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const ownerURL = uri(await read('owner-api-client.js'));
const carrierURL = uri((await read('connected-read-client.js')).replace('"./owner-api-client.js"',JSON.stringify(ownerURL)));
const adapterURL = uri((await read('two-source-read.js')).replace('"./owner-api-client.js"',JSON.stringify(ownerURL)));
const enrollmentURL = uri(await read('workspace-enrollment.js'));
const workspaceText = await read('connected-workspace.js');
const workspaceURL = uri(workspaceText.replace('"./workspace-enrollment.js"',JSON.stringify(enrollmentURL)).replace('"./connected-read-client.js"',JSON.stringify(carrierURL)).replace('"./two-source-read.js"',JSON.stringify(adapterURL)));
const {createConnectedWorkspace} = await import(workspaceURL);
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const wi = 'wi_'+'1'.repeat(32), fleetUID='fleet_'+'2'.repeat(32);
const refs = ['a','b'].map((n,i)=>({reference_id:id(i+4),endpoint_origin:`https://${n}.example.test`,revision:1}));
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
    querySelectorAll:()=>[...nav,...(elements.get('fleet').children||[])].filter(e=>e.dataset.view||e.dataset.botInspect),
    addEventListener(n,fn){listeners.set(n,fn);},removeEventListener(n){listeners.delete(n);}};
  class Element {
    constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.handlers={};this.hidden=false;this.disabled=false;this.isConnected=true;this.value='';this.textContent='';}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){for(const c of this.children)c.isConnected=false;this.children=nodes;}
    setAttribute(k,v){this.attributes[k]=v;}
    addEventListener(n,fn){this.handlers[n]=fn;}
    focus(){document.activeElement=this;}
    close(){this.closed=true;}
    click(){if(!this.disabled)this.handlers.click?.();}
  }
  for(const id of ['owner-session','main','fleet','fleet-totals','presence-strip','task-detail-content','focus-pane','task-detail','focus-overlay','search'])elements.set(id,new Element());
  const nav=['channel','grid','fleet','trust'].map(view=>{const e=new Element('button');e.dataset.view=view;return e;});
  class Observer {constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){this.closed=true;}}
  return {document,Element,nav,observers,listeners,get:id=>elements.get(id),MutationObserver:Observer};
}
function harness(options={}) {
  const count=options.count??2,doc={...documentContext,references:refs.slice(0,count)},calls=[],timers=[],streams=[],overrides=new Map();
  let metadata=context(doc.references),metadataStatus=200;
  const tokens=new Map();
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
  return {api,doc,ui,calls,timers,streams,overrides,dependencies,
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
  assert.equal(all.data.threads[0].task_link.task_id,'computer-1::'+wi);
  const detail=await h.api.jget(`/api/tasks/${encodeURIComponent('computer-1::'+wi)}?fleet=computer-1%20%2F%20team`);
  assert.equal(detail.data.task.body,'full body');assert.equal(detail.data.task.fleet_uid,'computer-1::'+fleetUID);
  const signer=h.calls.find(c=>c.url.endsWith('/session-assertion'));
  assert.deepEqual(Object.keys(JSON.parse(signer.init.body)).sort(),['binding_generation','binding_revision','expected_context_generation','host_uid','nonce','operational_origin','reference_id','workspace_id']);
  assert.equal(h.api.sendAction,undefined);assert.equal(h.api.prepareAction,undefined);assert.ok(!JSON.stringify(h.api.snapshot()).includes('AAAAA'));
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
