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
  later:'Coming soon'});            // B17
// With a team the bots part says nothing: the count is the saved configuration's, which is also true mid-Launch, after
// a failed Launch and after a stop, so no sheet sentence fits it ("running" would not be true). A sentence awaits the owner.
// The embedding page may name the provider, bots and helper parts (part_names in the connected document), never a
// computer: each computer keeps its own saved name. A name is 1 to 40 ASCII letters and digits in words joined by
// one space or one hyphen, so it cannot read like a status ("Claude - Linked", "Claude: Linked"). Anything else
// drops the whole field, never one name. Nor may a name hold a status the view says ("Linked", "Not linked",
// "Claude Linked", "None yet", "Coming soon"), which would be drawn beside the real one and contradict it.
const NAMED = Object.freeze(['provider','bots','helper']);
const NAME = /^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*$/;
const words = text => text.toLowerCase().split(/[ -]/);
const STATUSES = Object.freeze(['linked','notLinked','none','asleep','later'].map(key => words(PART_TEXT[key])));
const saysStatus = name => { const w = words(name);
  return STATUSES.some(status => w.some((_, i) => status.every((word, j) => w[i+j] === word))); };
export function partNames(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) return null;
  const entries = Object.entries(value);
  if (entries.some(([key, name]) => !NAMED.includes(key) || typeof name !== 'string' || name.length > 40 || !NAME.test(name) || saysStatus(name))) return null;
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
  return Object.freeze({computer:value.computer, provider:value.provider.state, teams:value.teams, helper:value.helper.state,
    team:value.team.state});
}

// One computer's parts. client: its connected read client; name(): its saved name; names: partNames() or null;
// hold(): true while the entry keeps today's panel for a launch this page sent or follows, when nothing is read.
// The parts read is sent once for each new ready connection and on refresh() (the window regained focus), never
// on a timer and never to retry. A refusal, a lost answer or a computer whose core has no parts read leaves
// started() false, so the view shows exactly what it showed before this read existed.
export function createWorkspaceParts({client, name, names = null, key, hold = () => false, onChange = () => {}}) {
  let current = null, readFor = null, reading = null, again = false, disposed = false, mount = null, last = client.snapshot();
  const ready = () => client.snapshot().state === 'ready';
  function connectionChanged(state) {
    const before = last; last = state;
    // Renewing the same session (ready, renewing, ready again) moves the epoch once and keeps the answer: the view
    // stays as it was through the renewal and no read is sent for it. Any other change drops the answer.
    const renewal = before.state === 'ready' && state.state === 'renewing' || before.state === 'renewing' && state.state === 'ready';
    if (current && renewal && current.epoch === before.epoch) { current = {...current, epoch:state.epoch}; readFor = state.epoch; }
    else if (current && (current.epoch !== state.epoch || state.state !== 'ready')) current = null;
    if (state.state === 'ready' && readFor !== state.epoch) void read();
  }
  async function read() {
    if (disposed || !ready() || hold() || typeof client.setupState !== 'function') return false;
    const epoch = client.snapshot().epoch;
    // A read already sent for this connection answers this one too; one sent for an earlier connection is
    // followed by one more for this connection when it ends, so a change that lands mid-read is never lost.
    if (reading) { if (readFor !== epoch) again = true; return reading; }
    readFor = epoch;
    reading = (async () => {
      let value = null;
      try { value = answer(await client.setupState()); } catch { value = null; }
      if (disposed || !ready() || client.snapshot().epoch !== epoch) return false;
      current = value ? {...value, epoch} : null; return !!value;
    })();
    try { return await reading; }
    finally {
      reading = null;
      if (!disposed) { if (again) { again = false; void read(); } onChange(); }
    }
  }
  // An answer kept through a renewal stays shown while the session renews.
  const live = () => !!current && ['ready','renewing'].includes(client.snapshot().state) && client.snapshot().epoch === current.epoch;
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
    const computer = name();
    root.setAttribute('aria-label', computer);
    set('computer', computer, 'linked', PART_TEXT.linked);
    set('provider', names?.provider ?? PART_TEXT.provider, current.provider, current.provider === 'linked' ? PART_TEXT.linked : PART_TEXT.notLinked);
    set('bots', names?.bots ?? PART_TEXT.bots, current.teams ? 'teams' : 'none', current.teams ? '' : PART_TEXT.none);
    control.hidden = reason.hidden = current.teams > 0;
    root.dataset.teams = String(current.teams);
    // The helper has no name in core: drawn only when the embedding page names it.
    rows.helper.row.hidden = !names?.helper;
    if (names?.helper) set('helper', names.helper, current.helper, current.helper === 'later' ? PART_TEXT.later : PART_TEXT.asleep);
  }
  return Object.freeze({connectionChanged, refresh:read, started, render, mountView,
    teams:() => started() ? current.teams : null,
    // The computer says a Launch it dispatched has not finished cleanly (in progress, or needing attention).
    launchUnfinished:() => started() && current.team === 'launch_unfinished', get root() { return mount?.root ?? null; },
    dispose() { disposed = true; current = null; mount = null; }});
}
