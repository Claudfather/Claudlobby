// One local first-team approval. Plans and activation journals stay canonical.
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const planID = value => typeof value === 'string' && /^p-[0-9a-f]{64}$/.test(value);
const request = value => value && Object.keys(value).sort().join() === 'generation,plan_id,request_id'
  && uuid(value.generation) && uuid(value.request_id) && planID(value.plan_id);

export function createGuidedSetup({client, storage, storageKey,
  newUUID = () => globalThis.crypto.randomUUID(), now = () => Date.now()/1000, onChange = () => {}}) {
  let context = null, plan = null, pending = null, goal = '', planGoal = '', note = '', busy = false, disposed = false, epoch = 0, mount;
  let state = client.snapshot();
  try { if (storage === undefined) storage = globalThis.localStorage; const text = storage?.getItem(storageKey); if (text && text.length < 512) { const value = JSON.parse(text); if (request(value)) pending = Object.freeze({...value}); } } catch {}
  const active = () => !disposed && ['ready','setup_only'].includes(client.snapshot().state);
  function save(value) { try { if (!storage || typeof storage.setItem !== 'function') throw new Error(); storage.setItem(storageKey,JSON.stringify(value)); } catch { throw new Error('Save the original activation identifiers before launching.'); } }
  function changed() { render(); onChange(); }
  function connectionChanged(next) {
    if (next.epoch !== state.epoch || next.state !== state.state) { epoch++; context = null; plan = null; }
    state = next; changed();
  }
  async function run(operation) {
    if (disposed || busy || !active()) return false;
    const ticket = epoch; busy = true; changed();
    try {
      if (operation === 'context') {
        const value = await client.setupContext();
        if (disposed || ticket !== epoch || !active()) return false;
        if (!value || value.version !== 1 || value.template !== 'revenue-web-app' || !uuid(value.generation)
            || typeof value.fleet !== 'string' || typeof value.repository !== 'string' || !/^r-[0-9a-f]{64}$/.test(value.release_id)
            || !/^[0-9a-f]{64}$/.test(value.release_seal) || !Number.isFinite(value.expires_at) || value.expires_at <= now()
            || value.request !== null && !request(value.request)) throw new Error();
        context = Object.freeze({...value}); plan = null;
        if (value.request) {
          if (pending && JSON.stringify(pending) !== JSON.stringify(value.request)) {
            // Compare fields rather than relying on incoming property ordering.
            if (!['generation','plan_id','request_id'].every(key => pending[key] === value.request[key])) throw new Error();
          }
          pending = Object.freeze({...value.request}); save(pending);
        }
        note = pending ? 'Inspect the original activation. Launch is never repeated.' : 'Local approval found. Author a goal and preview its exact plan.';
      } else if (operation === 'preview') {
        if (!context || pending || !goal.trim() || context.expires_at <= now()) return false;
        const selected = context, authored = goal;
        const value = await client.setupPreview({generation:selected.generation,goal:authored});
        if (disposed || ticket !== epoch || selected !== context || !active()) return false;
        if (!value || value.version !== 1 || !planID(value.plan_id) || value.generation !== selected.generation
            || value.release_id !== selected.release_id || value.release_seal !== selected.release_seal
            || value.fleets?.length !== 1 || value.fleets[0] !== selected.fleet || value.activation !== 'not_requested'
            || !Number.isFinite(value.expires_at) || value.expires_at <= now()) throw new Error();
        plan = Object.freeze(value); planGoal = authored;
        note = 'Preview only. Review all changes before explicitly launching this exact plan.';
      } else if (operation === 'launch') {
        if (!context || !plan || pending || planGoal !== goal || plan.expires_at <= now() || context.expires_at <= now()) return false;
        const value = {generation:context.generation,plan_id:plan.plan_id,request_id:newUUID()};
        if (!request(value)) throw new Error();
        save(value); pending = Object.freeze(value); // Durable client identity precedes the one launch POST.
        const result = await client.setupLaunch(value);
        if (disposed || ticket !== epoch || !active()) return false;
        receipt(result);
      } else if (operation === 'receipt') {
        if (!pending) return false;
        const original = pending, result = await client.setupReceipt(original);
        if (disposed || ticket !== epoch || original !== pending || !active()) return false;
        receipt(result);
      } else return false;
      return true;
    } catch {
      if (!disposed && ticket === epoch) note = pending ? 'Activation outcome unknown. Keep the original identifiers and check its receipt; do not launch again.' : 'Setup unavailable or refused. Check local approval explicitly; no team readiness is claimed.';
      return false;
    } finally { busy = false; if (!disposed) changed(); }
  }
  function receipt(value) {
    if (!value || value.version !== 1 || !pending || !['generation','plan_id','request_id'].every(k => value[k] === pending[k])
        || !['unknown','prepared','activating','rolling_back','rolled_back','active'].includes(value.state)) throw new Error();
    note = value.state === 'active'
      ? 'Team started. Check connection for its dashboard. Provider and repository access have not been tested here.'
      : value.state === 'activating' || value.state === 'prepared'
      ? 'Still checking. Inspect the original setup receipt; this never repeats or resumes setup.'
      : 'Setup needs attention. Keep the original identifiers and inspect this setup; do not launch again.';
  }
  function render() {
    if (!mount) return;
    const {area, status, declaration, controls, details, summary} = mount;
    area.disabled = !active() || busy || !!pending;
    status.textContent = note || (active() ? 'First-team setup requires an explicit local approval on this computer.' : 'Connect this computer before setup.');
    declaration.textContent = context ? `Team ${context.fleet} for ${context.repository}. Lead, engineer and reviewer; explicit human merge policy.` : '';
    summary.textContent = plan ? `Goal: ${planGoal}. Plan changes ${plan.changes?.length || 0} generated files; ${plan.effects?.restart_bots?.length || 0} bots require starting or restarting. Review the plan before launch.` : '';
    details.textContent = plan ? JSON.stringify({plan_id:plan.plan_id,release_id:plan.release_id,release_seal:plan.release_seal,changes:plan.changes,effects:plan.effects,expires_at:plan.expires_at},null,2) : pending ? JSON.stringify(pending,null,2) : '';
    for (const [operation,button] of Object.entries(controls)) button.disabled = !active() || busy
      || operation === 'preview' && (!context || !!pending || !goal.trim() || context.expires_at <= now())
      || operation === 'launch' && (!context || !plan || !!pending || goal !== planGoal || plan.expires_at <= now() || context.expires_at <= now())
      || operation === 'receipt' && !pending;
  }
  function mountPanel({document,element}) {
    const heading = document.createElement('strong'); heading.textContent = 'Create the first team'; element.append(heading);
    const declaration = document.createElement('p'); element.append(declaration);
    const area = document.createElement('textarea'); area.maxLength = 2000; area.value = goal;
    area.setAttribute('aria-label','Approved repository goal'); area.addEventListener('input',() => { goal = area.value; changed(); }); element.append(area);
    const controls = {};
    for (const [operation,title] of [['context','Check setup approval'],['preview','Preview team'],['launch','Launch this exact plan'],['receipt','Check original activation receipt']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = title; button.className = 'pill ghost';
      button.addEventListener('click',() => { if (!button.disabled) void run(operation); }); element.append(button); controls[operation] = button;
    }
    const summary = document.createElement('p'); element.append(summary);
    const disclosure = document.createElement('details'), title = document.createElement('summary'); title.textContent = 'Plan changes and recovery identifiers'; disclosure.append(title);
    const details = document.createElement('pre'); disclosure.append(details); element.append(disclosure);
    const status = document.createElement('p'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite'); element.append(status);
    mount = {area,status,declaration,details,controls,summary}; render();
  }
  return Object.freeze({mountPanel,run,connectionChanged,snapshot:() => ({busy,context,plan,pending,note,goal}),
    setGoal(value) { if (typeof value === 'string' && value.length <= 2000 && !pending) { goal = value; if (mount) mount.area.value = value; changed(); } },
    dispose() { disposed = true; epoch++; mount = null; }});
}
