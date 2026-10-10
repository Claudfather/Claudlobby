import test from 'node:test';
import assert from 'node:assert/strict';
import { ActionState, validContext, validNudgeContext, validFeedbackContext } from '../claudlobby/plane/ui/action-state.js';

const context = {version:1, room:'web', simulation:true,
  scope:{workspace:'example', host:'workshop', fleet:'web', viewer:'owner'},
  recipients:[{id:'lead',label:'Lead',lead:true}], actions:['message','feedback','nudge']};
const target = {recipient:'lead',task_id:'task-1'};
const row = {scope:context.scope,kind:'feedback',target};
const storage = () => {const data=new Map();return {getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)};};
const receipt = (request,status='delivered') => ({...request,version:1,status});

test('an uncertain request survives reload as metadata, with no body and no automatic resend',()=>{
  const store=storage(), state=new ActionState(store);
  state.draft(row,'Private feedback');
  const request=state.begin(context,'feedback',target,'Private feedback','request-1');
  const restored=new ActionState(store);
  assert.equal(restored.pending.length,1);
  assert.equal(restored.pending[0].request_id,'request-1');
  assert.ok(!store.getItem(state.key).includes('Private feedback'));
  assert.equal(restored.draft(row),'');
  assert.throws(()=>restored.begin(context,'feedback',target,'Another request','request-2'),/original receipt/);
  restored.accept(restored.pending[0],receipt(request));
  assert.equal(restored.pending.length,0);
});
test('recorded keeps the original ID pending; only matching terminal receipt resolves it',()=>{
  const state=new ActionState(storage());
  const request=state.begin(context,'feedback',target,'Look here','request-1');
  assert.equal(state.accept(request,receipt(request,'recorded')),'recorded');
  assert.equal(state.pending.length,1);
  for(const changed of [
    {scope:{...request.scope,host:'notebook'}}, {scope:{...request.scope,viewer:'other'}},
    {scope:{...request.scope,workspace:'other'}}, {target:{...target,task_id:'task-2'}},
    {target:{...target,recipient:'engineer'}}, {kind:'nudge'}, {request_id:'request-2'},
    {status:'unknown'}, {version:2},
  ]) assert.throws(()=>state.accept(request,{...receipt(request),...changed}),/does not match/);
  assert.equal(state.pending.length,1);
  state.accept(request,receipt(request,'rejected'));
  assert.equal(state.pending.length,0);
});
test('same-name teams on different hosts, viewers, workspaces and recipients keep separate drafts',()=>{
  const state=new ActionState(storage());
  state.draft(row,'Original');
  for(const changed of [
    {scope:{...row.scope,host:'notebook'}}, {scope:{...row.scope,fleet:'research'}},
    {scope:{...row.scope,viewer:'other'}}, {scope:{...row.scope,workspace:'other'}},
    {target:{...target,recipient:'engineer'}}, {target:{...target,task_id:'task-2'}}, {kind:'nudge'},
  ]) assert.equal(state.draft({...row,...changed}),'');
  const request=state.begin(context,'feedback',target,'Original','request-1');
  state.draft(row,'New text typed while awaiting receipt');
  state.accept(request,receipt(request));
  assert.equal(state.draft(row),'New text typed while awaiting receipt');
});
test('confirmed matching delivery clears its draft, rejection preserves it',()=>{
  const state=new ActionState(storage());
  state.draft(row,'Keep');
  const request=state.begin(context,'feedback',target,'Keep','request-1');
  state.accept(request,receipt(request,'rejected'));
  assert.equal(state.draft(row),'Keep');
  const retry=state.begin(context,'feedback',target,'Keep','request-2');
  state.accept(retry,receipt(retry));
  assert.equal(state.draft(row),'');
});
test('receipt lookup clears only the unchanged in-memory sent draft',()=>{
  const state=new ActionState(storage());
  state.draft(row,'Recover this');
  const request=state.begin(context,'feedback',target,'Recover this','lookup-1');
  state.accept(state.pending[0],receipt(request));
  assert.equal(state.draft(row),'');
  assert.equal(state.sentBodies.size,0);
});
test('visiting many targets never silently drops an unsent draft',()=>{
  const state=new ActionState(storage());
  for(let i=0;i<25;i++) state.draft({...row,target:{...target,task_id:`task-${i}`}},`Draft ${i}`);
  assert.equal(state.draft({...row,target:{...target,task_id:'task-24'}}),'Draft 24');
  assert.equal(state.draft({...row,target:{...target,task_id:'task-0'}}),'Draft 0');
});
test('unavailable storage, malformed records and exhausted capacity refuse before send',()=>{
  const broken=new ActionState({getItem:()=>null,setItem:()=>{throw Error('quota');}});
  assert.throws(()=>broken.begin(context,'feedback',target,'Hello','id'),/Nothing was sent/);
  assert.equal(broken.pending.length,0);
  const malformed=new ActionState({getItem:()=>'{',setItem:()=>{}});
  assert.throws(()=>malformed.begin(context,'feedback',target,'Hello','id'),/Nothing was sent/);
  const state=new ActionState(storage());
  for(let i=0;i<20;i++) state.begin(context,'feedback',{...target,task_id:`task-${i}`},'Hello',`id-${i}`);
  assert.throws(()=>state.begin(context,'feedback',{...target,task_id:'task-21'},'Hello','id-21'),/Nothing was sent/);
});
test('unsupported capability versions, recipients and verbs fail closed',()=>{
  const state=new ActionState(storage());
  assert.equal(validContext({...context,version:2}),false);
  assert.equal(validContext({...context,recipients:[context.recipients[0],context.recipients[0]]}),false);
  assert.equal(validContext({...context,recipients:[{id:'engineer',label:'Engineer'}]}),false);
  assert.equal(validContext({...context,recipients:[...context.recipients,{id:'other',label:'Other lead',lead:true}]}),false);
  assert.throws(()=>state.begin({...context,recipients:[...context.recipients,{id:'engineer',label:'Engineer'}]},'feedback',{...target,recipient:'engineer'},'Hello','id'),/not available/);
  assert.throws(()=>state.begin({...context,actions:['message']},'feedback',target,'Hello','id'),/not available/);
  assert.throws(()=>state.begin(context,'feedback',{...target,recipient:'admin'},'Hello','id'),/not available/);
  assert.throws(()=>state.begin(context,'nudge',{...target,task_id:null},'Hello','id'),/not available/);
});

test('explicit discard releases only that target, preserves its draft and retains metadata in this tab',()=>{
  const store=storage(), state=new ActionState(store);
  state.draft(row,'Keep my draft');
  const request=state.begin(context,'feedback',target,'Keep my draft','discard-1');
  const other=state.begin(context,'nudge',target,'Other action','discard-2');
  state.discard({...request,target:{...target,task_id:'other'}});
  assert.equal(state.pending.length,2);
  state.discard(request);
  assert.deepEqual(state.pending.map(p=>p.request_id),[other.request_id]);
  assert.equal(state.draft(row),'Keep my draft');
  assert.equal(state.discarded.get(request.request_id).target.task_id,'task-1');
  assert.ok(!JSON.stringify(state.discarded.get(request.request_id)).includes('Keep my draft'));
  assert.equal(state.sentBodies.has(request.request_id),false);
  assert.equal(new ActionState(store).pending.length,1);
  state.begin(context,'feedback',target,'New request','discard-3');
});
test('discard cannot lose a pending ID when storage refuses the write',()=>{
  const store=storage(), state=new ActionState(store);
  const request=state.begin(context,'feedback',target,'Hello','retain-1');
  store.setItem=()=>{throw Error('raw browser quota message');};
  assert.throws(()=>state.discard(request),/still pending/);
  assert.equal(state.pending[0].request_id,'retain-1');
  assert.equal(state.discarded.size,0);
});
test('corrupt storage is preserved until explicit recovery, including oversized saved data',()=>{
  for(const raw of ['{',JSON.stringify({wrong:'shape'}),'x'.repeat(50001)]) {
    const store=storage();store.setItem('plane.pending-actions.v1',raw);
    const state=new ActionState(store);
    assert.equal(state.corruptStorage,true);
    assert.equal(store.getItem(state.key),raw);
    state.clearCorruptStorage();
    assert.equal(store.getItem(state.key),'[]');
    assert.equal(state.storageError,false);
    state.begin(context,'feedback',target,'Hello','recover-1');
  }
  const unreadable=new ActionState({getItem:()=>{throw Error('denied');},setItem:()=>{throw Error('denied');}});
  assert.equal(unreadable.corruptStorage,false);
  assert.equal(unreadable.storageError,true);
});
test('explicit discard frees capacity after twenty unresolved requests',()=>{
  const state=new ActionState(storage());
  for(let i=0;i<20;i++) state.begin(context,'feedback',{...target,task_id:`task-${i}`},'Hello',`cap-${i}`);
  state.discard(state.pending[0]);
  state.begin(context,'feedback',{...target,task_id:'task-20'},'Hello','cap-20');
  assert.equal(state.pending.length,20);
});

test('a terminal receipt arriving after explicit discard resolves its unknown local history',()=>{
  const state=new ActionState(storage());
  const request=state.begin(context,'feedback',target,'Hello','late-discard');
  state.discard(request);
  assert.equal(state.discarded.size,1);
  state.accept(request,receipt(request,'recorded'));
  assert.equal(state.discarded.size,1);
  state.accept(request,receipt(request));
  assert.equal(state.discarded.size,0);
});

test('mixed and over-capacity saved arrays retain valid IDs for copy without rewriting storage',()=>{
  const original=new ActionState(storage()).begin(context,'feedback',target,'Private body','readable-id');
  for(const rows of [
    [original,{...original,request_id:'invalid-row',submitted_at:{toString:'invalid'}}],
    Array.from({length:21},(_,i)=>({...original,request_id:`overflow-${i}`})),
  ]) {
    const store=storage(), raw=JSON.stringify(rows);store.setItem('plane.pending-actions.v1',raw);
    const state=new ActionState(store);
    assert.equal(state.corruptStorage,true);
    assert.equal(state.pending.length,0);
    assert.equal(state.recoverable.length,rows.length===2?1:21);
    assert.ok(!JSON.stringify(state.recoverable).includes('Private body'));
    assert.equal(store.getItem(state.key),raw);
    state.clearCorruptStorage();
    assert.equal(state.recoverable.length,0);
  }
});


const nudgeContext = {version:2,room:'web',simulation:false,scope:{...context.scope,viewer:'nudge-generation'},
  recipients:[{id:'actor_'+ 'a'.repeat(32),label:'Team lead',lead:true}],actions:['nudge'],release_id:'r-'+ 'b'.repeat(64)};
const nudgeTarget = {recipient:nudgeContext.recipients[0].id,task_id:'wi_'+ 'c'.repeat(32),assignment_id:null,release_id:nudgeContext.release_id};
const nudgeId = '11111111-1111-4111-8111-111111111111';
const prepared = request => {const {body,...metadata}=request;return {...metadata,semantic_sha256:'d'.repeat(64)};};
const nudgeReceipt = (request,status='delivered') => {const {body,...metadata}=request;return {...metadata,status};};
test('v2 preparation is nonmutating; saved queued-null metadata reloads without reason alongside unchanged v1 rows',()=>{
  const store=storage(),state=new ActionState(store),legacy=state.begin(context,'message',{recipient:'lead',task_id:null},'Message draft','legacy-id');
  const before=store.getItem(state.key), request=state.prepare(nudgeContext,nudgeTarget,'Exact nudge reason',nudgeId);
  assert.equal(store.getItem(state.key),before);assert.equal(state.pending.length,1);assert.equal(state.sentBodies.has(nudgeId),false);
  const sending=state.beginPrepared(nudgeContext,request,prepared(request));
  assert.equal(sending.body,'Exact nudge reason');assert.ok(!store.getItem(state.key).includes('Exact nudge reason'));
  const restored=new ActionState(store);assert.equal(restored.storageError,false);assert.deepEqual(restored.pending[0],state.metadata(legacy));
  assert.equal(restored.pending[1].target.assignment_id,null);assert.equal(restored.pending[1].semantic_sha256,'d'.repeat(64));
  restored.accept(restored.pending[1],nudgeReceipt(sending,'recorded'));assert.equal(restored.pending.length,2);
  restored.accept(restored.pending[1],nudgeReceipt(sending));assert.equal(restored.pending.length,1);
});
test('nudge receipts bind digest, timestamp and every frozen precondition; logical task guard cannot be evaded',()=>{
  const state=new ActionState(storage()),request=state.prepare(nudgeContext,nudgeTarget,'Reason',nudgeId);
  const sending=state.beginPrepared(nudgeContext,request,prepared(request));
  for(const target of [{...nudgeTarget,assignment_id:'asg_'+ 'e'.repeat(32)},{...nudgeTarget,release_id:'r-'+ 'e'.repeat(64)}])
    assert.throws(()=>state.prepare({...nudgeContext,release_id:target.release_id},target,'New reason','22222222-2222-4222-8222-222222222222'),/original receipt/);
  for(const changed of [{semantic_sha256:'e'.repeat(64)},{submitted_at:'2026-01-02T00:00:00Z'},
    {target:{...nudgeTarget,assignment_id:'asg_'+ 'e'.repeat(32)}},{target:{...nudgeTarget,release_id:'r-'+ 'e'.repeat(64)}},
    {target:{...nudgeTarget,recipient:'actor_'+ 'e'.repeat(32)}},{version:1},{kind:'message'},{status:'unknown'},{body:'unrequested body'}])
    assert.throws(()=>state.accept(sending,{...nudgeReceipt(sending),...changed}),/does not match/);
  assert.equal(state.pending.length,1);
});
test('nudge preparation requires canonical explicit preconditions and exact returned metadata',()=>{
  const state=new ActionState(storage());assert.equal(validNudgeContext(nudgeContext),true);
  for(const bad of [{...nudgeContext,actions:['message','nudge']},{...nudgeContext,simulation:true},{...nudgeContext,release_id:'bad'}])
    assert.equal(validNudgeContext(bad),false);
  for(const target of [{recipient:nudgeTarget.recipient,task_id:nudgeTarget.task_id,release_id:nudgeTarget.release_id},
    {...nudgeTarget,assignment_id:undefined},{...nudgeTarget,assignment_id:''},{...nudgeTarget,assignment_id:'task-alias'},
    {...nudgeTarget,recipient:'lead'},{...nudgeTarget,task_id:'task-alias'},{...nudgeTarget,release_id:'r-bad'}])
    assert.throws(()=>state.prepare(nudgeContext,target,'Reason',nudgeId),/unavailable/);
  assert.throws(()=>state.prepare(nudgeContext,nudgeTarget,'Bad \uD800 reason',nudgeId),/reason/);
  const request=state.prepare(nudgeContext,nudgeTarget,'Reason',nudgeId);
  for(const changed of [{body:'Reason'},{status:'recorded'},{target:{...nudgeTarget,assignment_id:'asg_'+ 'e'.repeat(32)}},
    {request_id:'22222222-2222-4222-8222-222222222222'},{semantic_sha256:'bad'}])
    assert.throws(()=>state.beginPrepared(nudgeContext,request,{...prepared(request),...changed}),/did not match/);
  assert.equal(state.pending.length,0);
});
test('combined pending capacity and serialized write ceiling fail before either action can send',()=>{
  const store=storage(),state=new ActionState(store);
  for(let i=0;i<19;i++) state.begin(context,'message',{recipient:'lead',task_id:`legacy-${i}`},'Body',`id-${i}`);
  const request=state.prepare(nudgeContext,nudgeTarget,'Reason',nudgeId);state.beginPrepared(nudgeContext,request,prepared(request));
  assert.equal(state.pending.length,20);
  assert.throws(()=>state.prepare(nudgeContext,{...nudgeTarget,task_id:'wi_'+ 'e'.repeat(32)},'Reason','22222222-2222-4222-8222-222222222222'),/Nothing was sent/);
  const oversized=new ActionState(storage()), escaped={...context,scope:Object.fromEntries(Object.keys(context.scope).map(key=>[key,'\u0000'.repeat(240)]))};
  let ceilingBlocked=false;
  for(let i=0;i<20;i++) {
    try {oversized.begin(escaped,'message',{recipient:'lead',task_id:`task-${i}`},'Body',`oversized-${i}`);}
    catch(error) {assert.match(error.message,/Nothing was sent/);ceilingBlocked=true;break;}
  }
  assert.equal(ceilingBlocked,true);assert.ok(oversized.pending.length<20);assert.ok(oversized.storage.getItem(oversized.key).length<=50000);
  const broken=storage();broken.setItem=()=>{throw Error('quota');};const blocked=new ActionState(broken),pre=blocked.prepare(nudgeContext,nudgeTarget,'Reason',nudgeId);
  assert.throws(()=>blocked.beginPrepared(nudgeContext,pre,prepared(pre)),/Nothing was sent/);assert.equal(blocked.pending.length,0);
});


test('v2 reason uses logical task identity across assignment/release while pending wire binding stays frozen',()=>{
  const state=new ActionState(storage()),row={version:2,kind:'nudge',scope:nudgeContext.scope,target:nudgeTarget};
  state.draft(row,'Kept task reason');
  const moved={...row,target:{...nudgeTarget,assignment_id:'asg_'+'f'.repeat(32),release_id:'r-'+'f'.repeat(64)}};
  assert.equal(state.draft(moved),'Kept task reason');
  for(const changed of [{...moved,scope:{...moved.scope,viewer:'new-grant'}},
    {...moved,target:{...moved.target,recipient:'actor_'+'f'.repeat(32)}},
    {...moved,target:{...moved.target,task_id:'wi_'+'f'.repeat(32)}}])assert.equal(state.draft(changed),'');
  const original=state.prepare(nudgeContext,nudgeTarget,'Kept task reason',nudgeId);
  state.beginPrepared(nudgeContext,original,prepared(original));
  assert.deepEqual(state.pending[0].target,nudgeTarget);assert.equal(state.pending[0].semantic_sha256,'d'.repeat(64));
  assert.ok(!state.storage.getItem(state.key).includes('Kept task reason'));
});


const feedbackContext={...nudgeContext,scope:{...nudgeContext.scope,viewer:'feedback-generation'},actions:['feedback']};
test('v2 feedback prepared rows reload beside nudge and legacy rows without storing any comment',()=>{
 const store=storage(),state=new ActionState(store);assert.equal(validFeedbackContext(feedbackContext),true);assert.equal(validNudgeContext(feedbackContext),false);
 state.begin(context,'message',{recipient:'lead',task_id:null},'Legacy','legacy');
 const nudge=state.prepare(nudgeContext,nudgeTarget,'Reason',nudgeId);state.beginPrepared(nudgeContext,nudge,prepared(nudge));
 const request=state.prepare(feedbackContext,nudgeTarget,'Exact comment \n Ω','22222222-2222-4222-8222-222222222222');const sending=state.beginPrepared(feedbackContext,request,prepared(request));
 assert.equal(sending.body,'Exact comment \n Ω');assert.equal(sending.kind,'feedback');assert.ok(!store.getItem(state.key).includes('Exact comment'));
 const restored=new ActionState(store);assert.equal(restored.storageError,false);assert.deepEqual(restored.pending.map(r=>r.kind),['message','nudge','feedback']);
 restored.accept(restored.pending[2],nudgeReceipt(sending,'recorded'));assert.equal(restored.pending.length,3);restored.accept(restored.pending[2],nudgeReceipt(sending));assert.equal(restored.pending.length,2);
});
test('feedback immutable proof binds kind/assignment/release/digest and logical guard survives assignment change',()=>{
 const state=new ActionState(storage()),request=state.prepare(feedbackContext,nudgeTarget,'Comment',nudgeId);const sending=state.beginPrepared(feedbackContext,request,prepared(request));
 assert.throws(()=>state.prepare(feedbackContext,{...nudgeTarget,assignment_id:'asg_'+'e'.repeat(32)},'Second','22222222-2222-4222-8222-222222222222'),/original receipt/);
 for(const patch of [{kind:'nudge'},{semantic_sha256:'e'.repeat(64)},{target:{...nudgeTarget,assignment_id:'asg_'+'e'.repeat(32)}},{target:{...nudgeTarget,release_id:'r-'+'e'.repeat(64)}},{status:'unknown'}])assert.throws(()=>state.accept(sending,{...nudgeReceipt(sending),...patch}),/does not match/);
 const another=new ActionState(storage());assert.throws(()=>another.beginPrepared(nudgeContext,request,prepared(request)),/did not match/);assert.equal(another.pending.length,0);
 for(const body of ['Bad \uD800','Bad\u0000','x'.repeat(2001)])assert.throws(()=>another.prepare(feedbackContext,nudgeTarget,body,nudgeId),/comment/);
});
