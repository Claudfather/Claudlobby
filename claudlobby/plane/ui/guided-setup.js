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

export function createGuidedSetup({client, storage, storageKey, now = () => Date.now()/1000, onChange = () => {}}) {
  let context = null, plan = null, pending = null, conflicts = [], legacy = [], incompatible = false;
  let goal = '', planGoal = '', note = '', launchable = false, busy = false, disposed = false, epoch = 0, mount;
  let state = client.snapshot();
  function retain(value, collection) {
    if (collection.some(item => request(item) && request(value) ? same(item,value)
        : historical(item) && historical(value) && ['generation','plan_id','request_id'].every(key => item[key] === value[key]))) return;
    if (collection.length >= 16) throw new Error('Inspect retained conflicting identifiers locally.');
    collection.push(Object.freeze({...value}));
  }
  try {
    if (storage === undefined) storage = globalThis.localStorage;
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
    if (next.epoch !== state.epoch || next.state !== state.state) { epoch++; context = null; plan = null; launchable = false; }
    state = next; changed();
  }
  async function run(operation) {
    if (disposed || busy || !active()) return false;
    if (operation === 'context') { context = null; launchable = false; }
    const ticket = epoch; busy = true; changed();
    try {
      if (operation === 'context') {
        const value = await client.setupContext();
        if (disposed || ticket !== epoch || !active()) return false;
        if (!value || value.version !== 2 || value.template !== 'revenue-web-app' || !uuid(value.generation)
            || typeof value.fleet !== 'string' || typeof value.repository !== 'string' || !revision(value.source_revision)
            || 'release_id' in value || 'release_seal' in value || 'plan_id' in value
            || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || value.launch_reserved && value.request === null || !Number.isFinite(value.expires_at) || value.expires_at <= now()
            || value.request !== null && (!request(value.request) || value.request.generation !== value.generation
              || value.request.source_revision !== value.source_revision)) throw new Error();
        context = Object.freeze({...value}); plan = null;
        if (value.request) {
          if (pending && !same(pending,value.request)) retain(pending,conflicts);
          pending = binding(value.request); save(pending);
        }
        // A null request or new grant is never proof that an original request vanished.
        note = pending ? `Inspect the original update. ${localContinue(value)} Reconnection never submits launch again.` : 'Local approval found. Author a goal and preview its exact configuration.';
      } else if (operation === 'preview') {
        if (!context || context.launch_reserved || pending || legacyBlocked() || !goal.trim() || context.expires_at <= now()) return false;
        const selected = context, authored = goal;
        const value = await client.setupPreview({generation:selected.generation,goal:authored});
        if (disposed || ticket !== epoch || selected !== context || !active()) return false;
        if (!value || value.version !== 2 || !request(binding(value)) || value.generation !== selected.generation
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
        note = error?.code === 'setup_storage_unavailable' ? error.message : pending
          ? 'Update outcome unknown. Keep the original identifiers; check its receipt while approval is active, or inspect locally after expiry/revocation. Do not launch again.'
          : 'Setup unavailable or refused. Check local approval explicitly before another preview; no team readiness is claimed.';
      }
      return false;
    } finally { busy = false; if (!disposed) changed(); }
  }
  function receipt(value) {
    if (!value || value.version !== 2 || !pending || !same(value,pending) || !phases.includes(value.state)
        || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || typeof value.config_committed !== 'boolean'
        || !value.health_errors || typeof value.health_errors !== 'object' || Array.isArray(value.health_errors)
        || 'plan_id' in value || 'release_id' in value || 'release_seal' in value) throw new Error();
    if (value.launch_reserved || value.state === 'unknown') launchable = false;
    note = value.state === 'done' && value.config_committed && Object.keys(value.health_errors).length === 0
      ? 'Configuration committed. Check connection to view your team; inspect native health separately.'
      : value.state === 'unknown' || Object.keys(value.health_errors).length
      ? 'Setup needs attention. Keep the original identifiers and inspect this update; do not launch again.'
      : value.state === 'quiesce' && !value.launch_reserved
      ? `Configuration prepared and not launched. ${localContinue(value)} This browser never resubmits after reconnection.`
      : 'Still checking the original update. Receipt inspection never repeats or resumes setup.';
  }
  function render() {
    if (!mount) return;
    const {area,status,declaration,authority,controls,details,summary} = mount;
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
    declaration.textContent = context ? `Team ${context.fleet} for ${context.repository}. Lead, engineer and reviewer; explicit human merge policy.` : '';
    authority.textContent = context ? `Local approval covers ${context.repository} and source ${context.source_revision}. Launch starts native services and may use your configured provider accounts. Local folder, tool and account approvals may still be needed on this computer. Merge, deployment, additional spending and destructive changes need separate permission.` : '';
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
    area.setAttribute('aria-label','Approved repository goal'); area.addEventListener('input',() => { goal = area.value; changed(); }); element.append(area);
    const controls = {};
    for (const [operation,title] of [['context','Check setup approval'],['preview','Preview team'],['launch','Launch this exact configuration'],['receipt','Check original update receipt']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = title; button.className = 'pill ghost';
      button.addEventListener('click',() => { if (!button.disabled) void run(operation); }); element.append(button); controls[operation] = button;
    }
    const summary = document.createElement('p'); element.append(summary);
    const disclosure = document.createElement('details'), title = document.createElement('summary'); title.textContent = 'Configuration changes and recovery identifiers'; disclosure.append(title);
    const details = document.createElement('pre'); disclosure.append(details); element.append(disclosure);
    const status = document.createElement('p'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite'); element.append(status);
    mount = {area,status,declaration,authority,details,controls,summary}; render();
  }
  return Object.freeze({mountPanel,run,connectionChanged,snapshot:() => ({busy,context,plan,pending,conflicts:[...conflicts],legacy:[...legacy],note,goal}),
    setGoal(value) { if (typeof value === 'string' && value.length <= 2000 && !pending && !legacyBlocked()) { goal = value; if (mount) mount.area.value = value; changed(); } },
    dispose() { disposed = true; epoch++; mount = null; }});
}
