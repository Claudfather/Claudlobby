import test from 'node:test';
import assert from 'node:assert/strict';
import { ActionState, validContext } from '../claudlobby/plane/ui/action-state.js';

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
