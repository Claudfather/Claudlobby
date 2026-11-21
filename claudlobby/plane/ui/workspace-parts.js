// The parts view: one row for each part of a started computer, from the parts read (setup-state).
// It reads only, over a connection the person already made, and has no working control in this version.
// Every sentence a person reads here is a draft awaiting the owner's approval (STRINGS_FOR_APPROVAL, slice A).
const keys = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
const has = (value, fields) => value && typeof value === 'object' && !Array.isArray(value) && fields.every(key => Object.hasOwn(value, key));
export const PARTS = Object.freeze(['computer','provider','bots','helper']);
// The sentences the view shows, each with its draft's id on the sheet.
export const PART_TEXT = Object.freeze({
  linked:'Linked',                  // A14, B15
  provider:'Claude', notLinked:'Not linked', // A15
  bots:'Bots', none:'None yet', create:'Create a bot', // A16
  createReason:'Creating bots here is coming. There is nothing to do yet.', // A16
  asleep:'Asleep until Claude is linked', // A17
  later:'Coming soon',              // B17
  teams:n => `${n} ${n === 1 ? 'team' : 'teams'} running`}); // C7
// The embedding page may name parts (part_names in the connected document): keys from PARTS, values 1 to 40
// printable ASCII characters with no space at either end. Anything else drops the whole field, never one name.
const NAME = /^[\x21-\x7e](?:[\x20-\x7e]{0,38}[\x21-\x7e])?$/;
export function partNames(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) return null;
  const entries = Object.entries(value);
  if (entries.some(([key, name]) => !PARTS.includes(key) || typeof name !== 'string' || !NAME.test(name))) return null;
  return Object.freeze(Object.fromEntries(entries));
}
// The parts read's answer (owner_setup.py SetupEngine.state). Later versions add keys inside a part, so only the
// keys read here are required; a state this view does not know makes the whole answer unusable, never half drawn.
function answer(value) {
  if (!has(value, ['version','computer','provider','team','teams','bots','helper']) || value.version !== 1
    || !['started','not_started'].includes(value.computer) || !Number.isSafeInteger(value.teams) || value.teams < 0
    || !has(value.provider, ['state']) || !['not_linked','linked'].includes(value.provider.state)
    || !has(value.team, ['state']) || typeof value.team.state !== 'string'
    || !Array.isArray(value.bots) || !has(value.helper, ['state']) || !['asleep','later'].includes(value.helper.state)) return null;
  return Object.freeze({computer:value.computer, provider:value.provider.state, teams:value.teams, helper:value.helper.state});
}

// One computer's parts. client: its connected read client; name(): its saved name; names: partNames() or null.
// The parts read is sent once for each new ready connection and on refresh() (the window regained focus), never
// on a timer and never to retry. A refusal, a lost answer or a computer whose core has no parts read leaves
// started() false, so the view shows exactly what it showed before this read existed.
export function createWorkspaceParts({client, name, names = null, key, onChange = () => {}}) {
  let current = null, readFor = null, reading = null, disposed = false, mount = null;
  const ready = () => client.snapshot().state === 'ready';
  function connectionChanged(state) {
    if (current && current.epoch !== state.epoch || state.state !== 'ready') current = null;
    if (state.state === 'ready' && readFor !== state.epoch) void read();
  }
  async function read() {
    if (disposed || !ready() || reading || typeof client.setupState !== 'function') return false;
    const epoch = client.snapshot().epoch; readFor = epoch;
    reading = (async () => {
      let value = null;
      try { value = answer(await client.setupState()); } catch { value = null; }
      if (disposed || !ready() || client.snapshot().epoch !== epoch) return false;
      current = value ? {...value, epoch} : null; return !!value;
    })();
    try { return await reading; } finally { reading = null; if (!disposed) onChange(); }
  }
  const live = () => !!current && ready() && client.snapshot().epoch === current.epoch;
  const started = () => live() && current.computer === 'started';
  function mountView(document) {
    const make = (tag, className, text) => { const node = document.createElement(tag); node.className = className; if (text !== undefined) node.textContent = text; return node; };
    const root = make('section','workspace-parts'); root.hidden = true; root.tabIndex = -1;
    const list = make('ul','parts-list'); list.setAttribute('role','list'); root.append(list);
    const rows = {};
    for (const part of PARTS) {
      const row = make('li','part'), mark = make('span','part-mark'), label = make('strong','part-name'), status = make('span','part-status');
      row.dataset.part = part; mark.setAttribute('aria-hidden','true'); row.append(mark,label,status); list.append(row);
      rows[part] = {row,label,status};
    }
    const control = make('button','pill ghost part-control',PART_TEXT.create), reason = make('p','part-reason',PART_TEXT.createReason);
    control.type = 'button'; control.disabled = true; control.setAttribute('aria-disabled','true');
    reason.id = `parts-reason-${key}`; control.setAttribute('aria-describedby',reason.id);
    rows.bots.row.append(control,reason);
    mount = {root,rows,control,reason};
    return root;
  }
  // visible: whether the entry shows the view at all (it keeps today's panel while that panel follows its own launch).
  function render(visible = true) {
    if (!mount) return;
    const {root,rows,control,reason} = mount, shown = visible && started();
    root.hidden = !shown;
    if (!shown) return;
    const set = (part, label, state, text) => {
      const {row,label:node,status} = rows[part];
      if (node.textContent !== label) node.textContent = label;
      if (status.textContent !== text) status.textContent = text; // no repeated announcement
      row.dataset.state = state;
    };
    const computer = names?.computer ?? name();
    root.setAttribute('aria-label', computer);
    set('computer', computer, 'linked', PART_TEXT.linked);
    set('provider', names?.provider ?? PART_TEXT.provider, current.provider, current.provider === 'linked' ? PART_TEXT.linked : PART_TEXT.notLinked);
    set('bots', names?.bots ?? PART_TEXT.bots, current.teams ? 'teams' : 'none', current.teams ? PART_TEXT.teams(current.teams) : PART_TEXT.none);
    control.hidden = reason.hidden = current.teams > 0;
    root.dataset.teams = String(current.teams);
    // The helper has no name in core: drawn only when the embedding page names it.
    rows.helper.row.hidden = !names?.helper;
    if (names?.helper) set('helper', names.helper, current.helper, current.helper === 'later' ? PART_TEXT.later : PART_TEXT.asleep);
  }
  return Object.freeze({connectionChanged, refresh:read, started, render, mountView,
    teams:() => started() ? current.teams : null, get root() { return mount?.root ?? null; },
    dispose() { disposed = true; current = null; mount = null; }});
}
