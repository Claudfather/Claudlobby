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
  assert.throws(()=>broken.begin(context,'feedback',target,'Hello','id'),/quota/);
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
