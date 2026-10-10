// Explicit synthetic adapter for the loopback development fixture only.
const rooms = ['Workshop / Web app', 'Notebook / Web app'];
const now = () => new Date().toISOString();
const env = data => ({state:'ok',data,provenance:{source:'synthetic fixture',checked_at:now()}});
const scope = room => ({workspace:'Example studio',host:room.split(' / ')[0],fleet:room,viewer:'example-owner'});
const recipients = [{id:'lead',label:'Team lead',lead:true},{id:'engineer',label:'Engineer'},{id:'reviewer',label:'Reviewer'}];
const bar = document.createElement('aside');
bar.style.cssText='padding:12px 24px;background:#262722;color:#fff4dd;font:13px system-ui;display:flex;gap:18px;flex-wrap:wrap;align-items:center';
bar.innerHTML='<b>DEVELOPMENT EXAMPLE · no real bots</b><label>Next response <select id="fixture-outcome"><option value="delivered">Delivered</option><option value="drop">Lose response after recording</option><option value="recorded">Recorded only</option><option value="rejected">Refused</option></select></label><a href="/?readonly=1" style="color:inherit">Read-only view</a>';
document.body.prepend(bar);
const readonly = new URLSearchParams(location.search).has('readonly');
const calls = async (path, body) => {
  const response = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(6000)});
  if(!response.ok) throw Error('fixture request refused');
  return response.json();
};
export async function interactionContext(room) {
  if(readonly || !rooms.includes(room)) return null;
  return {version:1,room,scope:scope(room),simulation:true,recipients,actions:['message','feedback','nudge']};
}
export const sendAction = request => calls('/fixture/action',{request,outcome:document.getElementById('fixture-outcome').value});
export const actionReceipt = request => calls('/fixture/receipt',{request});
export function createEventSource() { return new EventTarget(); }
function tasks(room) {
  const id = room.split(' / ')[0].toLowerCase();
  return [
    {task_id:id+'-signup',title:'A shorter signup flow is ready for review',state:'completed',attention:false,terminal_event:{occurred_at:now()},last_event:{event:'completed',occurred_at:now()}},
    {task_id:id+'-audience',title:'Which audience should we design for first?',state:'blocked',attention:true,attention_question:'Solo consultants or small agencies? This changes the next draft.',attention_reason:['blocked'],last_event:{event:'blocked',occurred_at:now()}},
    {task_id:id+'-checklist',title:'Adding a clearer first-run checklist',state:'active',attention:false,last_event:{event:'progress',occurred_at:now()}},
  ].map(t=>({...t,fleet:room,created_at:now(),resolved:true,issues:[],assignment_count:1,assignment_history:[],current_assignment:{assignee_short:'Engineer',assignee_alias:'engineer',dispatch_message_id:id+'-dispatch'},delivery:{integrity:'synthetic receipt'}}));
}
function thread(task) {
  return {key:task.task_id,work_item_id:task.task_id,title:task.title,latest_seq:1,task_events:[],delivered:true,terminal:task.state==='completed'?'completed':null,messages:[{msg_id:task.task_id+'-report',message_class:'report',sender_short:'Engineer',recipient_short:'Team lead',occurred_at:now(),privacy:'full',body:task.state==='completed'?'I shortened signup to three steps and wrote the remaining questions in the review note. This example result still needs your review.':'The first draft is ready. I need a decision on the audience before choosing the next examples.',body_words:null,delivery_state:'delivered'}]};
}
export async function jget(url) {
  const u=new URL(url,location.origin), selected=u.searchParams.get('fleet');
  const active=selected?rooms.filter(r=>r===selected):rooms;
  const fleets=rooms.map(alias=>({alias,bots:3,presence:{counts:{working:1,idle:2},live_poll:'ok'},capture:'example',open:2,attention:1,overdue:0,orphaned:0,last_activity_at:now(),newest_report_at:now(),reports_24h:3,unacked:null}));
  if(['/api/fleets','/api/overview'].includes(u.pathname)) return env({fleets,default:rooms[0],totals:{fleets:2,bots:6,working:2,attention:2,overdue:0,live_poll:'ok'},host:{daemon_serving:false,rows:'example',spool_files:null,ingest_lag_state:'none',samples:{}}});
  if(u.pathname==='/api/summary') return {state:'synthetic fixture'};
  if(u.pathname.startsWith('/api/tasks/')) {
    const id=decodeURIComponent(u.pathname.slice('/api/tasks/'.length));
    const task=active.flatMap(tasks).find(t=>t.task_id===id);
    return task?env({task:{...task,body:'Synthetic task description; no real fleet data.',assignments:[],history:[]}})
      :{state:'not_found',remediation:'No synthetic task in this example team.'};
  }
  if(u.pathname==='/api/tasks') return env({tasks:active.flatMap(tasks),issue_count:0,truncated:false});
  if(u.pathname==='/api/channel') {
    const saved=await fetch('/fixture/records').then(r=>r.json());
    return env({threads:[...saved.filter(r=>active.includes(r.scope.fleet)&&r.status!=='rejected').map((r,i)=>({key:r.request_id,work_item_id:r.target.task_id,title:r.kind+' to '+r.target.recipient,latest_seq:10+i,task_events:[],delivered:r.status==='delivered',messages:[{msg_id:r.request_id,message_class:'message',sender_short:'You',recipient_short:r.target.recipient,occurred_at:r.submitted_at,privacy:'full',body:r.body,delivery_state:r.status}]})),...active.flatMap(room=>tasks(room).slice(0,2).map(t=>thread(t)))]});
  }
  if(u.pathname==='/api/identities') return env({identities:active.flatMap(room=>recipients.map(r=>({alias:r.id,short:r.label,kind:'actor',fleet:room,last_seen:now()})))});
  if(u.pathname==='/api/presence') return env({bots:[],counts:{}});
  if(u.pathname==='/api/grid') return env({panes:[],sampler_running:false});
  return {state:'unavailable',remediation:'Outside this bounded example.'};
}
