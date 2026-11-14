// One first-team approval; the existing Git operation owns preparation and launch.
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const keys = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
const revision = value => typeof value === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value);
const fingerprint = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const bindingFields = ['generation','request_id','config_fingerprint','source_revision'];
const request = value => keys(value,bindingFields) && uuid(value.generation) && uuid(value.request_id)
  && fingerprint(value.config_fingerprint) && revision(value.source_revision);
const historical = value => keys(value,['generation','plan_id','request_id']) && uuid(value.generation)
  && uuid(value.request_id) && typeof value.plan_id === 'string' && /^p-[0-9a-f]{64}$/.test(value.plan_id);
const same = (left,right) => bindingFields.every(key => left?.[key] === right?.[key]);
const binding = value => Object.freeze(Object.fromEntries(bindingFields.map(key => [key,value[key]])));
const continuation = value => value === undefined || typeof value === 'string' && value.length > 0
  && value.length <= 16384 && !/[\x00-\x1f\x7f]/.test(value);
const localContinue = value => value.continuation ? `Continue locally: ${value.continuation}.`
  : 'Use this installation’s stable CLI with host update continue.';
const phases = ['unknown','prepare','quiesce','git','environment','publish','migrate','commit','restore','done'];

export function createGuidedSetup({client, storage, storageKey, now = () => Date.now()/1000, onChange = () => {},
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout, visible = () => true}) {
  let context = null, plan = null, pending = null, conflicts = [], legacy = [], incompatible = false;
  let goal = '', planGoal = '', note = '', launchable = false, busy = false, disposed = false, epoch = 0, mount;
  let state = client.snapshot();
  let signinAttempt=null, signinState=null, signinUrl=null, signinNotice='', signinBlocked=false;
  let managedHandoff = false, handoffTimer = null, handoffReads = 0, connectionChecked = false, handoffUntil = 0;
  function stopHandoff() { managedHandoff = false; clearTimeout(handoffTimer); handoffTimer = null; }
  function scheduleHandoff() {
    if (!managedHandoff || handoffTimer !== null || disposed || !pending) return;
    if (!handoffUntil) handoffUntil = now() + 600;
    if (now() >= handoffUntil || handoffReads >= 80) {
      stopHandoff(); note = 'Automatic connection checks paused. Keep the original update and choose Check connection, then Check original update receipt. Do not launch again.'; changed(); return;
    }
    if (!visible() || !active()) return; // Resume the same reads when this view/connection returns.
    handoffTimer = setTimeout(async () => {
      handoffTimer = null;
      if (disposed || !managedHandoff || !pending || !visible() || !active()) return;
      if (now() >= handoffUntil) { scheduleHandoff(); return; }
      if (busy) { scheduleHandoff(); return; }
      const original = pending; busy = true; handoffReads++; changed();
      try {
        // Only the original receipt and existing session status are checked.
        // client.check may promote the SAME token from setup-only to readable;
        // it never invokes Connect, issue or renewal here.
        const value = await client.setupReceipt(original);
        if (disposed || !managedHandoff) return;
        if (!same(original,pending)) { stopHandoff(); return; }
        try {
          receipt(value);
          if (value.managed_owner !== true) throw new Error();
        } catch {
          stopHandoff(); note = 'Original update receipt did not match. Inspect access and retained identifiers; do not launch again.'; return;
        }
        if (!value.launch_reserved) { stopHandoff(); return; }
        if (!connectionChecked && typeof client.check === 'function') {
          await client.check(); connectionChecked = true;
        }
        if (value.state === 'done' || value.state === 'unknown' || Object.keys(value.health_errors).length) stopHandoff();
      } catch (error) {
        if (!disposed && [400,401,403,409].includes(error?.status)) {
          stopHandoff(); note = 'Connection access or the original update was refused. Check access explicitly; Launch is not repeated.';
        } else if (!disposed && managedHandoff) note = 'Starting your managed connection. The original update is retained; Launch is not repeated.';
      } finally { busy = false; if (!disposed) { changed(); scheduleHandoff(); } }
    }, handoffReads < 2 ? 1500 : handoffReads < 5 ? 3000 : 10000);
  }
  function retain(value, collection) {
    if (collection.some(item => request(item) && request(value) ? same(item,value)
        : historical(item) && historical(value) && ['generation','plan_id','request_id'].every(key => item[key] === value[key]))) return;
    if (collection.length >= 16) throw new Error('Inspect retained conflicting identifiers locally.');
    collection.push(Object.freeze({...value}));
  }
  try {
    if (storage === undefined) storage = globalThis.localStorage;
    try {
    const text=storage?.getItem(storageKey+':claude');
    if (text) { const value=JSON.parse(text); if (!keys(value,['generation','attempt_id']) || !uuid(value.generation) || !uuid(value.attempt_id)) throw new Error(); signinAttempt=Object.freeze(value); }
    } catch { signinBlocked=true; }
    const text = storage?.getItem(storageKey);
    if (text) {
      if (text.length >= 8192) throw new Error();
      const value = JSON.parse(text);
      if (request(value)) pending = binding(value);
      else if (historical(value)) retain(value,legacy);
      else if ((keys(value,['pending','conflicts']) || keys(value,['version','pending','conflicts','legacy']) && value.version === 2)
          && (value.pending === null || request(value.pending) || historical(value.pending))
          && Array.isArray(value.conflicts) && value.conflicts.length <= 16 && value.conflicts.every(item => request(item) || historical(item))
          && (value.legacy === undefined || Array.isArray(value.legacy) && value.legacy.length <= 16 && value.legacy.every(historical))) {
        if (request(value.pending)) pending = binding(value.pending);
        else if (value.pending) retain(value.pending,legacy);
        for (const item of value.conflicts) retain(item,request(item) ? conflicts : legacy);
        for (const item of value.legacy || []) retain(item,legacy);
      } else throw new Error();
    }
  } catch { incompatible = true; }
  const active = () => !disposed && ['ready','setup_only'].includes(client.snapshot().state);
  const legacyBlocked = () => incompatible || legacy.length > 0;
  function save(value) {
    try {
      if (incompatible || !storage || typeof storage.setItem !== 'function') throw new Error();
      storage.setItem(storageKey,JSON.stringify({version:2,pending:value,conflicts,legacy}));
    } catch { throw Object.assign(new Error('This browser could not retain the original update identifiers. No launch was sent. Restore site storage; keep the original identifiers for local recovery.'), {code:'setup_storage_unavailable'}); }
  }
  function changed() { render(); onChange(); }
  function connectionChanged(next) {
    if (next.epoch !== state.epoch || next.state !== state.state) {
      epoch++; context = null; plan = null; launchable = false; signinUrl=null;
      if (mount) mount.code.value='';
    }
    state = next;
    if (managedHandoff && ['denied','signed_out','expired','disposed'].includes(next.state)) {
      stopHandoff(); note = 'Connection access needs attention. Keep the original update; reconnect explicitly without another Launch.';
    }
    changed(); scheduleHandoff();
  }
  async function run(operation) {
    if (disposed || busy || !active()) return false;
    if (operation === 'context') { context = null; launchable = false; }
    const ticket = epoch; busy = true; changed();
    try {
      if (operation === 'context') {
        const value = await client.setupContext();
        if (disposed || ticket !== epoch || !active()) return false;
        if (!value || !(value.version === 2 && value.template === 'revenue-web-app' && typeof value.repository === 'string' && !('project_directory' in value)
            || value.version === 3 && value.template === 'local-project' && typeof value.project_directory === 'string'
              && value.project_directory.startsWith('/') && !('repository' in value)) || !uuid(value.generation)
            || typeof value.fleet !== 'string' || !revision(value.source_revision)
            || value.managed_owner !== undefined && value.managed_owner !== true
            || 'release_id' in value || 'release_seal' in value || 'plan_id' in value
            || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || value.launch_reserved && value.request === null || !Number.isFinite(value.expires_at) || value.expires_at <= now()
            || value.request !== null && (!request(value.request) || value.request.generation !== value.generation
              || value.request.source_revision !== value.source_revision)) throw new Error();
        context = Object.freeze({...value}); plan = null;
        if (value.provider_signin === true && value.signin_attempt) signinReceipt(value.signin_attempt);
        if (value.request) {
          if (pending && !same(pending,value.request)) retain(pending,conflicts);
          pending = binding(value.request); save(pending);
        }
        // A null request or new grant is never proof that an original request vanished.
        note = pending ? `Inspect the original update. ${localContinue(value)} Reconnection never submits launch again.` : 'Local approval found. Author a goal and preview its exact configuration.';
        if (value.managed_owner && value.launch_reserved) { managedHandoff = true; handoffUntil = now()+600; handoffReads=0; }
      } else if (operation.startsWith('signin-')) {
        if (!context?.provider_signin || pending || signinBlocked) return false;
        const action=operation.slice(7);
        if (action==='begin') {
          if (signinAttempt) return false;
          const value={generation:context.generation,attempt_id:globalThis.crypto.randomUUID()};
          storage.setItem(storageKey+':claude',JSON.stringify(value));
          signinAttempt=Object.freeze(value); // Retain before ONE effect; no uncertain resend.
        }
        if (!signinAttempt || signinAttempt.generation!==context.generation) return false;
        let input={...signinAttempt};
        if (action==='code') {
          const code=mount?.code.value || '';
          if (mount) mount.code.value=''; // Never retain in model/storage/debug output.
          if (!code || code.length>2305 || /[\r\n\x00]/.test(code)) return false;
          input.code=code;
        }
        const result=await client.setupSignin(action,input); input=null;
        if (disposed || ticket!==epoch || !active()) return false;
        signinReceipt(result);
      } else if (operation === 'preview') {
        if (!context || context.launch_reserved || pending || legacyBlocked() || !goal.trim() || context.expires_at <= now()) return false;
        const selected = context, authored = goal;
        const value = await client.setupPreview({generation:selected.generation,goal:authored});
        if (disposed || ticket !== epoch || selected !== context || !active()) return false;
        if (!value || value.version !== selected.version || !request(binding(value)) || value.generation !== selected.generation
            || value.source_revision !== selected.source_revision || 'plan_id' in value || 'release_id' in value || 'release_seal' in value
            || value.fleets?.length !== 1 || value.fleets[0] !== selected.fleet || value.dispatch !== 'prepared' || value.phase !== 'quiesce'
            || !Array.isArray(value.changes) || !value.effects || typeof value.effects !== 'object' || Array.isArray(value.effects)
            || !Number.isFinite(value.expires_at) || value.expires_at <= now()) throw new Error();
        pending = binding(value); plan = Object.freeze({...value}); planGoal = authored;
        save(pending); launchable = true;
        note = 'Configuration prepared. Review the frozen goal and changes before one explicit launch.';
      } else if (operation === 'launch') {
        if (!context || !plan || !pending || !launchable || legacyBlocked() || !same(plan,pending)
            || planGoal !== goal || plan.expires_at <= now() || context.expires_at <= now()) return false;
        save(pending); launchable = false; // Retain the preview's original identity before the one launch POST.
        managedHandoff = context.managed_owner === true;
        const result = await client.setupLaunch(pending);
        if (disposed || ticket !== epoch || !active()) return false;
        receipt(result);
      } else if (operation === 'receipt') {
        if (!pending) return false;
        const original = pending, result = await client.setupReceipt(original);
        if (disposed || ticket !== epoch || original !== pending || !active()) return false;
        receipt(result);
      } else return false;
      return true;
    } catch (error) {
      if (!disposed && ticket === epoch) {
        if (operation === 'preview') { context = null; launchable = false; }
        if (operation === 'receipt') launchable = false;
        if (operation.startsWith('signin-')) { signinNotice='Sign-in needs attention. Check the original attempt; nothing is repeated.'; signinUrl=null; return false; }
        note = error?.code === 'setup_storage_unavailable' ? error.message : pending
          ? 'Update outcome unknown. Keep the original identifiers; check its receipt while approval is active, or inspect locally after expiry/revocation. Do not launch again.'
          : 'Setup unavailable or refused. Check local approval explicitly before another preview; no team readiness is claimed.';
      }
      return false;
    } finally { busy = false; if (!disposed) { changed(); scheduleHandoff(); } }
  }
  function signinReceipt(value) {
    if (!context || !keys(value,['attempt_id','generation','state','reason',...('url' in value?['url']:[])])
        || !uuid(value.attempt_id) || value.generation!==context.generation
        || signinAttempt && (value.attempt_id!==signinAttempt.attempt_id || value.generation!==signinAttempt.generation)
        || !['starting','waiting','connected','needs_attention','cancelled','unknown'].includes(value.state)
        || value.reason!==null && (typeof value.reason!=='string' || !/^[a-z_]{1,64}$/.test(value.reason))) throw new Error();
    let url=null;
    if (value.url!==undefined) {
      const selected=new URL(value.url);
      if (value.state!=='waiting' || selected.origin!=='https://claude.com' || selected.pathname!=='/cai/oauth/authorize'
          || selected.hash || selected.searchParams.get('redirect_uri')!=='https://platform.claude.com/oauth/code/callback'
          || selected.searchParams.get('scope')!=='user:inference' || value.url.length>8192) throw new Error();
      url=value.url;
    }
    signinAttempt=Object.freeze({generation:value.generation,attempt_id:value.attempt_id});
    storage.setItem(storageKey+':claude',JSON.stringify(signinAttempt));
    signinState=value.state; signinUrl=url;
    signinNotice=value.state==='connected' ? 'Claude connected for this host. This does not start your team.'
      : value.state==='waiting' ? 'Sign in to Claude in the link below. If Claude shows a code, paste it here.'
      : value.state==='starting' ? 'Waiting for Claude… Check sign-in progress.'
      : 'Sign-in needs attention. Keep the original attempt; cancellation does not revoke a token.';
  }
  function receipt(value) {
    if (!value || ![2,3].includes(value.version) || context && value.version !== context.version || !pending || !same(value,pending) || !phases.includes(value.state)
        || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || typeof value.config_committed !== 'boolean'
        || !value.health_errors || typeof value.health_errors !== 'object' || Array.isArray(value.health_errors)
        || value.managed_owner !== undefined && value.managed_owner !== true
        || 'plan_id' in value || 'release_id' in value || 'release_seal' in value) throw new Error();
    if (value.launch_reserved || value.state === 'unknown') launchable = false;
    note = value.state === 'done' && value.config_committed && Object.keys(value.health_errors).length === 0
      ? 'Configuration committed. Check native readiness, then open your team and message the lead with one concrete request. Your setup goal does not start a task.'
      : value.state === 'unknown' || Object.keys(value.health_errors).length
      ? 'Setup needs attention. Keep the original identifiers and inspect this update; do not launch again.'
      : value.state === 'quiesce' && !value.launch_reserved
      ? `Configuration prepared and not launched. ${localContinue(value)} This browser never resubmits after reconnection.`
      : value.state === 'quiesce' && value.launch_reserved
      ? value.managed_owner ? 'Launch retained. Your managed connection is starting; the original update will reconnect here. Do not launch again.'
        : 'Launch retained. Let the local setup guide finish its handoff, then use its original-update status and reconnect commands. Do not launch again.'
      : 'Still checking the original update. Receipt inspection never repeats or resumes setup.';
  }
  function render() {
    if (!mount) return;
    const {area,status,declaration,authority,controls,details,summary,signinBox,signinStatus,signinLink,code,signinControls} = mount;
    signinBox.hidden=!context?.provider_signin || !!pending;
    signinStatus.textContent=signinNotice;
    signinLink.hidden=!signinUrl; if (signinUrl) signinLink.setAttribute('href',signinUrl); else signinLink.removeAttribute?.('href');
    code.disabled=!active() || busy || signinState!=='waiting';
    for (const [operation,button] of Object.entries(signinControls)) button.disabled=!active() || busy || signinBlocked
      || operation==='begin' && !!signinAttempt || operation!=='begin' && !signinAttempt
      || operation==='code' && signinState!=='waiting';
    area.disabled = !active() || busy || !!pending || legacyBlocked();
    const guard = legacyBlocked() ? 'Incompatible historical setup identifiers are retained. Inspect them locally; this browser cannot launch a replacement.'
      : !active() ? 'Connect this computer before setup.' : busy ? 'Checking setup…'
      : pending && !launchable ? 'Use Check original update receipt; launch is not repeated.'
      : !context ? 'Choose Check setup approval before previewing a team.'
      : context.expires_at <= now() ? 'Local setup approval expired. Choose Check setup approval.'
      : !goal.trim() ? 'Enter a goal to enable Preview team.'
      : !plan ? 'Choose Preview team before launching this goal.'
      : plan.expires_at <= now() ? 'This preview expired. Inspect the original update locally.'
      : 'Review the configuration, then choose Launch this exact configuration.';
    status.textContent = [note,guard].filter(Boolean).join(' ');
    declaration.textContent = context ? context.template === 'local-project'
      ? `Team ${context.fleet} for local project ${context.project_directory}. After the team is ready and messages are locally approved, tell the lead what to do. The lead records the task and coordinates implementation and independent review.`
      : `Team ${context.fleet} for ${context.repository}. Lead, engineer and reviewer; explicit human merge policy.` : '';
    authority.textContent = context ? `Local approval covers ${context.template === 'local-project' ? `read/write access to local project ${context.project_directory}` : context.repository} and source ${context.source_revision}. Launch starts native services and may use your configured provider accounts. Local folder, tool and account approvals may still be needed on this computer. Merge, deployment, additional spending and destructive changes need separate permission.` : '';
    summary.textContent = plan ? `Goal: ${planGoal}. Configuration changes ${plan.changes.length} generated files; ${plan.effects?.restart_bots?.length || 0} bots require starting or restarting. Review before launch.` : '';
    details.textContent = plan || pending || conflicts.length || legacy.length ? JSON.stringify({pending,conflicting_identifiers:conflicts,
      historical_identifiers:legacy,...(plan ? {changes:plan.changes,effects:plan.effects,expires_at:plan.expires_at} : {})},null,2) : '';
    for (const [operation,button] of Object.entries(controls)) button.disabled = !active() || busy
      || operation === 'preview' && (!context || !!pending || legacyBlocked() || !goal.trim() || context.expires_at <= now())
      || operation === 'launch' && (!context || !plan || !pending || !launchable || legacyBlocked() || goal !== planGoal || plan.expires_at <= now() || context.expires_at <= now())
      || operation === 'receipt' && !pending;
  }
  function mountPanel({document,element}) {
    const heading = document.createElement('strong'); heading.textContent = 'Create the first team'; element.append(heading);
    const declaration = document.createElement('p'); element.append(declaration);
    const authority = document.createElement('p'); element.append(authority);
    const area = document.createElement('textarea'); area.maxLength = 2000; area.value = goal;
    area.setAttribute('aria-label','Approved project goal'); area.addEventListener('input',() => { goal = area.value; changed(); }); element.append(area);
    const controls = {};
    for (const [operation,title] of [['context','Check setup approval'],['preview','Preview team'],['launch','Launch this exact configuration'],['receipt','Check original update receipt']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = title; button.className = 'pill ghost';
      button.addEventListener('click',() => { if (!button.disabled) void run(operation); }); element.append(button); controls[operation] = button;
    }
    const signinBox=document.createElement('section'); element.append(signinBox);
    const signinStatus=document.createElement('p'); signinStatus.setAttribute('role','status'); signinBox.append(signinStatus);
    const signinLink=document.createElement('a'); signinLink.textContent='Open Claude sign-in'; signinLink.setAttribute('target','_blank'); signinLink.setAttribute('rel','noreferrer noopener'); signinBox.append(signinLink);
    const code=document.createElement('input'); code.type='password'; code.maxLength=2305; code.setAttribute('aria-label','Claude sign-in code'); code.setAttribute('autocomplete','off'); signinBox.append(code);
    const signinControls={};
    for (const [operation,label] of [['begin','Sign in to Claude'],['status','Check sign-in progress'],['code','Submit Claude code'],['cancel','Cancel sign-in']]) {
      const button=document.createElement('button'); button.type='button'; button.textContent=label; button.addEventListener('click',()=>{if(!button.disabled) void run('signin-'+operation);}); signinBox.append(button); signinControls[operation]=button;
    }
    const summary = document.createElement('p'); element.append(summary);
    const disclosure = document.createElement('details'), title = document.createElement('summary'); title.textContent = 'Configuration changes and recovery identifiers'; disclosure.append(title);
    const details = document.createElement('pre'); disclosure.append(details); element.append(disclosure);
    const status = document.createElement('p'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite'); element.append(status);
    mount = {area,status,declaration,authority,details,controls,summary,signinBox,signinStatus,signinLink,code,signinControls}; render();
  }
  return Object.freeze({mountPanel,run,connectionChanged,snapshot:() => ({busy,context,plan,pending,conflicts:[...conflicts],legacy:[...legacy],note,goal}),
    setGoal(value) { if (typeof value === 'string' && value.length <= 2000 && !pending && !legacyBlocked()) { goal = value; if (mount) mount.area.value = value; changed(); } },
    resumeHandoff:() => scheduleHandoff(),
    dispose() { disposed = true; stopHandoff(); epoch++; if (mount) mount.code.value=''; signinUrl=null; mount = null; }});
}
