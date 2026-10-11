// One local first-team approval. Plans and activation journals stay canonical.
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const planID = value => typeof value === 'string' && /^p-[0-9a-f]{64}$/.test(value);
const request = value => value && Object.keys(value).sort().join() === 'generation,plan_id,request_id'
  && uuid(value.generation) && uuid(value.request_id) && planID(value.plan_id);

export function createGuidedSetup({client, storage, storageKey,
  newUUID = () => globalThis.crypto.randomUUID(), now = () => Date.now()/1000, onChange = () => {}, onOpenTeam}) {
  let context = null, plan = null, pending = null, conflicts = [], goal = '', planGoal = '', note = '', busy = false, disposed = false, epoch = 0, mount;
  let state = client.snapshot();
  let teamActive = false;
  let teamFleet = null; // Navigation only; opening still requires a fresh admitted roster.
  try { if (storage === undefined) storage = globalThis.localStorage; const text = storage?.getItem(storageKey); if (text && text.length < 8192) { const value = JSON.parse(text); if (request(value)) pending = Object.freeze({...value});
    else if (value && Object.keys(value).sort().join() === 'conflicts,pending' && (value.pending === null || request(value.pending))
        && Array.isArray(value.conflicts) && value.conflicts.length <= 16 && value.conflicts.every(request)) {
      pending = value.pending && Object.freeze({...value.pending}); conflicts = value.conflicts.map(v => Object.freeze({...v}));
    } } } catch {}
  const active = () => !disposed && ['ready','setup_only'].includes(client.snapshot().state);
  function save(value) { try { if (!storage || typeof storage.setItem !== 'function') throw new Error(); storage.setItem(storageKey,JSON.stringify({pending:value,conflicts})); } catch { throw new Error('Save the original activation identifiers before launching.'); } }
  function retainConflict(value) {
    if (conflicts.some(v => ['generation','plan_id','request_id'].every(key => v[key] === value[key]))) return;
    if (conflicts.length >= 16) throw new Error('Inspect retained conflicting identifiers locally.');
    conflicts.push(Object.freeze({...value}));
  }
  function changed() { render(); onChange(); }
  function connectionChanged(next) {
    if (next.epoch !== state.epoch || next.state !== state.state) { epoch++; context = null; plan = null; teamActive = false; }
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
            || value.request !== null && (!request(value.request) || value.request.generation !== value.generation)) throw new Error();
        if (value.github != null && (typeof value.github.name !== 'string'
            || !['managed','independent'].includes(value.github.mode) || value.github.repository !== value.repository
            || !value.github.permissions || Array.isArray(value.github.permissions)
            || typeof value.github.permissions !== 'object'
            || Object.values(value.github.permissions).some(level => !['read','write'].includes(level)))) throw new Error();
        context = Object.freeze({...value}); plan = null; teamActive = false; teamFleet = null;
        if (pending && (pending.generation !== value.generation || value.request
            && !['generation','plan_id','request_id'].every(key => pending[key] === value.request[key]))) {
          retainConflict(pending);
          const canonical = value.request && Object.freeze({...value.request});
          save(canonical); pending = canonical;
        } else if (value.request) {
          save(value.request); pending = Object.freeze({...value.request});
        }
        // Same-generation null is not evidence of no queued/reserved work.
        note = pending ? 'Check your team’s launch status below.' : 'Your computer is ready for setup. Give your team a goal, then preview the changes.';
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
        note = 'Your team is ready to review. Launch starts the approved bots on your computer.';
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
      } else if (operation === 'repository') {
        if (!pending || !teamActive || context?.can_prepare_repository !== true || context.expires_at <= now()) return false;
        note = 'Preparing working copies for your team. This can take a few minutes; keep this page open.'; changed();
        const original = pending, result = await client.setupRepository(original);
        if (disposed || ticket !== epoch || original !== pending || !active()) return false;
        const repository = context.repository.toLowerCase();
        if (!result || result.version !== 1 || !['generation','plan_id','request_id'].every(k => result[k] === original[k])
            || typeof result.repository !== 'string' || result.repository.toLowerCase() !== repository
            || !Array.isArray(result.checkouts) || !result.checkouts.length
            || result.checkouts.some(row => !['prepared','existing'].includes(row.state)
              || typeof row.repository !== 'string' || row.repository.toLowerCase() !== repository)) throw new Error();
        note = 'Working copies are available for your team. Open team to see its activity and start a conversation. Application dependencies and setup scripts have not been run.';
      } else if (operation === 'open') {
        if (!teamFleet || typeof onOpenTeam !== 'function') return false;
        let opened;
        try { opened = await onOpenTeam(teamFleet) === true; }
        catch { opened = false; }
        // Checking the connection can admit dashboard reads and reset setup context.
        // The workspace callback fences that transition before selecting the team.
        if (disposed || !active()) return false;
        note = opened ? 'Team dashboard opened. Talk to your team below when messaging is approved on this computer.'
          : 'The team dashboard is not available yet. Check the connection and try Open team again; this does not repeat the launch.';
        return opened;
      } else return false;
      return true;
    } catch {
      if (!disposed && ticket === epoch) note = operation === 'repository'
        ? 'Repository preparation could not be confirmed and may still be running on your computer. Wait a few minutes before retrying preparation; existing working copies are preserved. If it keeps failing, check the selected connection locally. Do not launch the team again.'
        : pending ? 'Activation outcome unknown. Keep the original identifiers; check its receipt while approval is active, or inspect locally after expiry/revocation. Do not launch again.' : 'Setup unavailable or refused. Check local approval explicitly; no team readiness is claimed.';
      return false;
    } finally { busy = false; if (!disposed) changed(); }
  }
  function receipt(value) {
    if (!value || value.version !== 1 || !pending || !['generation','plan_id','request_id'].every(k => value[k] === pending[k])
        || !['unknown','prepared','activating','rolling_back','rolled_back','active'].includes(value.state)) throw new Error();
    teamActive = value.state === 'active';
    teamFleet = teamActive ? context?.fleet || teamFleet : null;
    note = teamActive && context?.can_prepare_repository === true
      ? 'Team started. Prepare its repository below, then Open team.'
      : value.state === 'active'
      ? 'Team started. Open team to see its dashboard. Provider and repository access have not been tested here.'
      : value.state === 'activating' || value.state === 'prepared'
      ? 'Still checking. Inspect the original setup receipt; this never repeats or resumes setup.'
      : 'Setup needs attention. Keep the original identifiers and inspect this setup; do not launch again.';
  }
  function render() {
    if (!mount) return;
    const {area, status, declaration, connection, controls, details, summary} = mount;
    area.disabled = !active() || busy || !!pending;
    status.textContent = note || (active() ? 'First-team setup requires an explicit local approval on this computer.' : 'Connect this computer before setup.');
    declaration.textContent = context ? `Team ${context.fleet} for ${context.repository}. Lead, engineer and reviewer; explicit human merge policy.` : '';
    connection.textContent = !context ? '' : context.github
      ? `GitHub: ${context.github.name}. Access to ${context.repository}: ${Object.entries(context.github.permissions).map(([name,level]) => `${name.replaceAll('_',' ')} ${level}`).join(', ')}. Provider access still needs to be verified.`
      : 'No named GitHub connection selected. Complete repository access on this computer before asking the team to work.';
    summary.textContent = plan ? `Goal: ${planGoal}. Plan changes ${plan.changes?.length || 0} generated files; ${plan.effects?.restart_bots?.length || 0} bots require starting or restarting. Review the plan before launch.` : '';
    details.textContent = plan ? JSON.stringify({conflicting_identifiers:conflicts,plan_id:plan.plan_id,release_id:plan.release_id,release_seal:plan.release_seal,changes:plan.changes,effects:plan.effects,expires_at:plan.expires_at},null,2) : pending || conflicts.length ? JSON.stringify({pending,conflicting_identifiers:conflicts},null,2) : '';
    for (const [operation,button] of Object.entries(controls)) button.disabled = !active() || busy
      || operation === 'preview' && (!context || !!pending || !goal.trim() || context.expires_at <= now())
      || operation === 'launch' && (!context || !plan || !!pending || goal !== planGoal || plan.expires_at <= now() || context.expires_at <= now())
      || operation === 'receipt' && !pending
      || operation === 'open' && (!teamFleet || typeof onOpenTeam !== 'function')
      || operation === 'repository' && (!teamActive || !pending || context?.can_prepare_repository !== true || context.expires_at <= now());
  }
  function mountPanel({document,element}) {
    const heading = document.createElement('strong'); heading.textContent = 'Create your team'; element.append(heading);
    const declaration = document.createElement('p'); element.append(declaration);
    const connection = document.createElement('p'); element.append(connection);
    const area = document.createElement('textarea'); area.maxLength = 2000; area.value = goal;
    area.placeholder = 'What should your team accomplish? For example, simplify signup and add tests for the checkout flow.';
    area.setAttribute('aria-label','Approved repository goal'); area.addEventListener('input',() => { goal = area.value; changed(); }); element.append(area);
    const controls = {};
    for (const [operation,title] of [['context','Check computer setup'],['preview','Preview team'],['launch','Launch team'],['receipt','Check launch status'],['repository','Prepare repository'],['open','Open team']]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = title; button.className = 'pill ghost';
      button.addEventListener('click',() => { if (!button.disabled) void run(operation); }); element.append(button); controls[operation] = button;
    }
    const summary = document.createElement('p'); element.append(summary);
    const disclosure = document.createElement('details'), title = document.createElement('summary'); title.textContent = 'Plan changes and recovery identifiers'; disclosure.append(title);
    const details = document.createElement('pre'); disclosure.append(details); element.append(disclosure);
    const status = document.createElement('p'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite'); element.append(status);
    mount = {area,status,declaration,connection,details,controls,summary}; render();
  }
  return Object.freeze({mountPanel,run,connectionChanged,snapshot:() => ({busy,context,plan,pending,conflicts:[...conflicts],note,goal}),
    setGoal(value) { if (typeof value === 'string' && value.length <= 2000 && !pending) { goal = value; if (mount) mount.area.value = value; changed(); } },
    dispose() { disposed = true; epoch++; mount = null; }});
}
