import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const uri = text => 'data:text/javascript;base64,'+Buffer.from(text).toString('base64');
const {createWorkspaceEnrollment} = await import(uri(await readFile(new URL('../claudlobby/plane/ui/workspace-enrollment.js',import.meta.url),'utf8')));
const nonce='A'.repeat(43), host='host_'+'a'.repeat(32),issuer='https://accounts.example.test',op='https://plane.example.test';
const ref={reference_id:'00000000-0000-4000-8000-000000000004',endpoint_origin:'https://mini.example.test',revision:1};
const context={workspace:{id:'00000000-0000-4000-8000-000000000002',name:'Test workspace'}};
const response=(status,data)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const flush=async()=>{for(let i=0;i<3;i++)await new Promise(r=>setImmediate(r));};
function harness(){
 let current=context,clock=1800000000;const calls=[],timers=[],changed=[];
 let challenge={version:1,state:'awaiting_assertion',issuer,host_uid:host,nonce,operational_origin:op,expires_at:clock+300};
 let assertion={version:1,assertion:'e30.e30.AAAA',expires_at:clock+120};
 let stage={version:1,state:'awaiting_local_approval',expires_at:clock+120};
 let hostFn=(url,init)=>response(202,url.endsWith('/stage')?stage:challenge),signFn=()=>assertion;
 const api=createWorkspaceEnrollment({reference:ref,issuer,operationalOrigin:op,currentContext:()=>current,
  now:()=>clock,setTimeout(fn,ms){const t={fn,ms,active:true};timers.push(t);return t;},clearTimeout(t){if(t)t.active=false;},onChange:s=>changed.push(s),
  fetch:async(url,init)=>{calls.push({url,init});return hostFn(url,init);},obtainAssertion:async(input,opts)=>{calls.push({url:'website',input,opts});return signFn(input,opts);}});
 const nodes=[];const document={createElement(tag){const node={tag,children:[],textContent:'',hidden:false,setAttribute(){},append(...a){this.children.push(...a);},addEventListener(n,fn){this[n]=fn;}};nodes.push(node);return node;}};const element=document.createElement('span');api.mountPanel({document,element});
 return {api,calls,timers,changed,nodes,element,get challenge(){return challenge;},set challenge(x){challenge=x;},set assertion(x){assertion=x;},set stage(x){stage=x;},set current(x){current=x;},set clock(x){clock=x;},set hostFn(x){hostFn=x;},set signFn(x){signFn=x;},text:()=>nodes.map(n=>n.textContent).join('\n')};
}

test('exact challenge/sign/stage only; private code displayed separately after staging, no read/session/action or automatic replay',async()=>{
 const h=harness();assert.equal(await h.api.start(),true);assert.deepEqual(h.calls.map(c=>c.url),[ref.endpoint_origin+'/api/owner/workspace/challenge','website',ref.endpoint_origin+'/api/owner/workspace/stage']);
 for(const c of h.calls.filter(c=>c.init)){assert.equal(c.init.method,'POST');assert.equal(c.init.credentials,'omit');assert.equal(c.init.redirect,'error');assert.equal(c.init.cache,'no-store');assert.deepEqual(c.init.headers,{'Content-Type':'application/json','X-Claudlobby-Owner':'1'});assert.equal(new URL(c.url).search,'');}
 assert.deepEqual(JSON.parse(h.calls[0].init.body),{});assert.deepEqual(h.calls[1].input,{workspace_id:context.workspace.id,host_uid:host,nonce,operational_origin:op});
 assert.deepEqual(JSON.parse(h.calls[2].init.body),{nonce,assertion:'e30.e30.AAAA'});
 assert.match(h.text(),/Approval code: A/);assert.match(h.text(),/claudlobby --root DATA_ROOT host owner confirm-workspace/);assert.ok(!h.text().includes('confirm-workspace '+nonce));
 assert.ok(!JSON.stringify(h.api.snapshot()).includes(nonce));assert.ok(!JSON.stringify(h.changed).includes(nonce));assert.equal(await h.api.start(),false);assert.equal(h.calls.length,3);
 h.api.cancel();assert.ok(!h.text().includes(nonce));assert.equal(h.api.snapshot().state,'idle');h.api.dispose();
});

test('wrong challenge purpose/issuer/origin/shape/nonce/clock refuses before signer or stage',async()=>{
 for(const change of [{state:'awaiting_session_assertion'},{issuer:'https://wrong.example.test'},{operational_origin:'https://wrong.example.test'},{host_uid:host+'\n'},{nonce:nonce+'='},{expires_at:1800000000},{extra:true}]){
  const h=harness();h.challenge={...h.challenge,...change};assert.equal(await h.api.start(),false);assert.equal(h.calls.length,1);assert.equal(h.api.snapshot().state,'refused');assert.ok(!h.text().includes(nonce));h.api.dispose();
 }
 const h=harness();h.clock=NaN;assert.equal(await h.api.start(),false);assert.equal(h.calls.length,1);h.api.dispose();
});

test('wrong assertion finite DTO refuses without staging or exposing assertion/code',async()=>{
 for(const result of [{version:2,assertion:'e30.e30.AAAA',expires_at:1800000120},{version:1,assertion:'not a JWT',expires_at:1800000120},{version:1,assertion:'e30.e30.AAAA',expires_at:1800000126},{version:1,assertion:'e30.e30.AAAA',expires_at:1800000120,extra:true},{version:1,assertion:'a.b.'+'c'.repeat(8189),expires_at:1800000120}]){
  const h=harness();h.assertion=result;assert.equal(await h.api.start(),false);assert.equal(h.calls.length,2);assert.ok(!h.text().includes(nonce));h.api.dispose();
 }
});

test('five-second browser clock skew is tolerated without accepting unbounded host or issuer retention',async()=>{
 for(const secondsBehind of [1,5]){
  const h=harness();h.clock=1800000000-secondsBehind;assert.equal(await h.api.start(),true);
  assert.ok(h.timers.filter(t=>t.active).every(t=>t.ms<=125000));h.api.dispose();
 }
 const h=harness();h.challenge={...h.challenge,expires_at:1800000306};assert.equal(await h.api.start(),false);assert.equal(h.calls.length,1);h.api.dispose();
});

test('workspace rename refreshes the label and disposal erases it permanently, including later cancellation',async()=>{
 const h=harness();assert.ok(h.text().includes('Test workspace'));
 h.current={workspace:{...context.workspace,name:'Renamed workspace'}};h.api.refresh();
 assert.ok(h.text().includes('Renamed workspace'));assert.ok(!h.text().includes('Test workspace'));
 await h.api.start();const cancel=h.nodes.find(n=>n.tag==='button');h.api.dispose();const changes=h.changed.length;
 h.api.cancel();cancel.click();h.api.refresh();
 assert.equal(h.api.snapshot().state,'disposed');assert.equal(h.changed.length,changes);
 assert.ok(!h.text().includes('Renamed workspace'));assert.ok(!h.text().includes(nonce));
});

test('each asynchronous boundary fences account loss/cancel/disposal and never sends the following write',async()=>{
 for(const door of ['challenge','website','stage']){
  const h=harness(),held=deferred();if(door==='website')h.signFn=()=>held.promise;else h.hostFn=(url)=>url.endsWith('/'+door)?held.promise:response(202,h.challenge);
  const start=h.api.start();await flush();h.current=null;h.api.dispose();
  held.resolve(door==='website'?{version:1,assertion:'e30.e30.AAAA',expires_at:1800000120}:response(202,door==='stage'?{version:1,state:'awaiting_local_approval',expires_at:1800000120}:h.challenge));
  assert.equal(await start,false);assert.equal(h.api.snapshot().state,'disposed');assert.ok(!h.text().includes(nonce));assert.equal(h.calls.length,door==='challenge'?1:door==='website'?2:3);
 }
});

test('lost/refused/malformed stage is unknown with original code for local inspection; duplicate click never resends',async()=>{
 for(const failed of [()=>{throw new Error();},()=>response(403,{}),()=>response(202,{version:1,state:'ready',token:'never'})]){
  const h=harness();h.hostFn=(url)=>url.endsWith('/stage')?failed():response(202,h.challenge);
  assert.equal(await h.api.start(),false);assert.equal(h.api.snapshot().state,'unknown');assert.match(h.text(),/may have been saved/);assert.match(h.text(),/do not resend automatically/);assert.ok(h.text().includes(nonce));
  assert.equal(await h.api.start(),false);assert.equal(h.calls.length,3);h.api.cancel();assert.ok(!h.text().includes(nonce));h.api.dispose();
 }
});

test('expiry clears staged code and aborts a held assertion; bounded host body timeout cannot publish late',async()=>{
 const done=harness();await done.api.start();const timer=done.timers.find(t=>t.active&&t.ms===120000);timer.fn();assert.equal(done.api.snapshot().state,'expired');assert.ok(!done.text().includes(nonce));done.api.dispose();
 const h=harness(),held=deferred();h.signFn=()=>held.promise;const start=h.api.start();await flush();h.timers.find(t=>t.active&&t.ms===300000).fn();held.resolve({version:1,assertion:'e30.e30.AAAA',expires_at:1800000120});assert.equal(await start,false);assert.equal(h.calls.length,2);h.api.dispose();
 const body=harness();body.hostFn=()=>new Response(new ReadableStream({start(){}}),{status:202,headers:{'Content-Type':'application/json'}});const pending=body.api.start();await flush();body.timers.find(t=>t.active&&t.ms===35000).fn();assert.equal(await pending,false);assert.equal(body.api.snapshot().state,'refused');body.api.dispose();
});


test('busy clicks never duplicate challenge; assertion delay past original challenge expiry cannot stage',async()=>{
 const h=harness(),held=deferred();h.hostFn=()=>held.promise;const start=h.api.start();assert.equal(await h.api.start(),false);assert.equal(h.calls.length,1);held.resolve(response(202,h.challenge));await start;assert.equal(h.calls.length,3);h.api.dispose();
 const late=harness(),signer=deferred();late.challenge={...late.challenge,expires_at:1800000010};late.signFn=()=>signer.promise;const pending=late.api.start();await flush();late.clock=1800000011;signer.resolve({version:1,assertion:'e30.e30.AAAA',expires_at:1800000130});assert.equal(await pending,false);assert.equal(late.calls.length,2);assert.equal(late.api.snapshot().state,'expired');assert.ok(!late.text().includes(nonce));late.api.dispose();
});
