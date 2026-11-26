// The parts view: one row for each part of a started computer, from the parts read (setup-state).
// It reads over a connection the person already made. Its one working control is the Claude part's: Link Claude
// asks the setup page (never this view) to run the permission card, and the sign-in that follows is claude-signin.js.
// Every sentence a person reads here is the owner's approved wording (2026-11-23). The Claude part's own sentences
// (CLAUDE_TEXT and claude-signin.js's link endings) are final as of 2026-11-26: the owner delegated their words.
import { createClaudeSignin, SIGNIN_PROGRESS, linkEnding, LINK_ENDING_BUTTONS, LINK_CLOSING } from "./claude-signin.js";
const keys = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
const has = (value, fields) => value && typeof value === 'object' && !Array.isArray(value) && fields.every(key => Object.hasOwn(value, key));
export const PARTS = Object.freeze(['computer','provider','bots','helper']);
// The sentences the view shows, each with its id on the sheet; the owner approved them on 2026-11-23.
export const PART_TEXT = Object.freeze({
  linked:'Linked',                  // A14, B15
  provider:'Claude', notLinked:'Not linked', // A15
  bots:'Bots', none:'None yet', create:'Create a bot', // A16
  createReason:'You can’t create bots here yet.', // A16, as the owner reworded it
  asleep:'Asleep until Claude is linked', // A17
  later:'Coming soon',              // B17
  oneTeam:'1 team set up'});        // the owner's sentence for one team (2026-11-23)
// The Claude part's own sentences (plan section 6; final as of 2026-11-26): B10 (both), N12, N4, B11 and B15.
export const CLAUDE_TEXT = Object.freeze({
  link:'Link Claude',                     // B10
  elsewhere:'Link Claude from the page where you added this computer.', // B10
  update:'Update this computer to link Claude.', goUpdate:'Go to the update', // N12
  waiting:'Linking Claude continues above.', // N4
  stop:'Stop waiting',                    // B11
  saved:'A Claude sign-in is saved on this computer.'}); // B15
// Stop waiting (B11) is offered only once a wait has lasted this long. The setup page answers every request, so it is
// rarely needed, and until then nothing takes Link Claude's place: a double-click cannot press it.
export const STOP_AFTER_MS = 10000;
// A completed refusal is the computer's answer (core's setup door refuses with 403); a lost or unknown answer proves nothing.
const refusal = error => [400,401,403,409].includes(error?.status);
// The count is the saved configuration's, also true mid-Launch, after a failed Launch and after a stop, so it says
// "set up", never "running". Only one team has a sentence: with more the bots part says nothing (no plural is approved).
// The embedding page may name the provider, bots and helper parts (part_names in the connected document), never a
// computer: each computer keeps its own saved name. A name is 1 to 40 ASCII letters and digits in words joined by
// one space or one hyphen, so it cannot read like a status ("Claude - Linked", "Claude: Linked"). Anything else
// drops the whole field, never one name. Nor may a name hold a status the view says ("Linked", "Not linked",
// "Claude Linked", "None yet", "Coming soon", "1 team set up"), which would be drawn beside the real one and contradict it.
const NAMED = Object.freeze(['provider','bots','helper']);
const NAME = /^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*$/;
const words = text => text.toLowerCase().split(/[ -]/);
const STATUSES = Object.freeze(['linked','notLinked','none','asleep','later','oneTeam'].map(key => words(PART_TEXT[key])));
const saysStatus = name => { const w = words(name);
  return STATUSES.some(status => w.some((_, i) => status.every((word, j) => w[i+j] === word))); };
export function partNames(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) return null;
  const entries = Object.entries(value);
  if (entries.some(([key, name]) => !NAMED.includes(key) || typeof name !== 'string' || name.length > 40 || !NAME.test(name) || saysStatus(name))) return null;
  return Object.freeze(Object.fromEntries(entries));
}
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const signinPart = value => has(value, ['generation','phase','attempt_id','state','reason','code_sent','retryable','unconfirmed'])
  && uuid(value.generation) && ['approved','waiting','ended'].includes(value.phase) && (value.attempt_id === null || uuid(value.attempt_id))
  && (value.state === null || typeof value.state === 'string') && (value.reason === null || typeof value.reason === 'string')
  && [value.code_sent, value.retryable, value.unconfirmed].every(flag => typeof flag === 'boolean');
// The parts read's answer (owner_setup.py SetupEngine.state). Later versions add keys inside a part, so only the
// keys read here are required; a state this view does not know makes the whole answer unusable, never half drawn.
function answer(value) {
  if (!has(value, ['version','computer','provider','team','teams','bots','helper']) || value.version !== 1
    || !['started','not_started'].includes(value.computer) || !Number.isSafeInteger(value.teams) || value.teams < 0
    || !has(value.provider, ['state']) || !['not_linked','linked'].includes(value.provider.state)
    || has(value.provider, ['link']) && !['available','not_here'].includes(value.provider.link)
    || has(value.provider, ['signin']) && !signinPart(value.provider.signin)
    || !has(value.team, ['state']) || typeof value.team.state !== 'string'
    || !Array.isArray(value.bots) || !has(value.helper, ['state']) || !['asleep','later'].includes(value.helper.state)) return null;
  // link: the computer's marker ('available' or 'not_here'), null from a core that has none. signin: the link
  // approval's sign-in, null when there is none (provider.signin, owner_setup.py), copied to the keys read here.
  const signin = value.provider.signin;
  return Object.freeze({computer:value.computer, provider:value.provider.state, teams:value.teams, helper:value.helper.state,
    team:value.team.state, link:value.provider.link ?? null, signin:signin ? Object.freeze({generation:signin.generation,
      phase:signin.phase, attempt_id:signin.attempt_id, state:signin.state, reason:signin.reason, code_sent:signin.code_sent,
      retryable:signin.retryable, unconfirmed:signin.unconfirmed}) : null});
}

// One computer's parts. client: its connected read client; name(): its saved name; names: partNames() or null;
// hold(): true while the entry keeps today's panel for a launch this page sent or follows, when nothing is read.
// The parts read is sent once for each new ready connection and on refresh() (the window regained focus), never
// on a timer and never to retry. A read the hold kept back is sent once by release() when the hold ends. A read asked
// for while one is in flight is never answered by it (that read was sent before whatever asks now): one fresh read
// follows it, shared by every request made meanwhile. A refusal, a lost answer or a computer whose core has no parts read leaves
// started() false, so the view shows exactly what it showed before this read existed; once an answer is shown for this
// connection, such a refresh keeps it, so one failed read never takes the view away.
// The Claude part (section "The Claude part" of the plan): onSetupPage is true on the setup page's own frame (the
// browser carrier), the only place Link Claude can be asked. request(run) asks that page, with run 'link' or 'update',
// and says whether it was sent; the view only ever calls it from a press. runEnded(message) takes the page's answer.
export function createWorkspaceParts({client, name, names = null, key, hold = () => false, onChange = () => {},
  onSetupPage = false, request = () => false, now = () => Date.now()/1000, visible = () => true,
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout,
  copyText = text => globalThis.navigator.clipboard.writeText(text)}) {
  let current = null, readFor = null, reading = null, disposed = false, mount = null, last = client.snapshot();
  let queued = null, wake = null; // the one fresh read promised to requests made while a read is in flight, and its resolver
  let held = false; // a read for a ready connection was kept back by hold()
  // The Claude part. waiting: this view pressed Link Claude and the page has not answered (Stop waiting only ends the
  // line, a later answer still lands); stopShown: the wait has lasted STOP_AFTER_MS, so Stop waiting is offered (its
  // timer, stopTimer); settling: approvals this view waited for whose read has not answered yet. sg: the lifted sign-in
  // step for the current link approval (claude-signin.js),
  // with no storage: the parts read is its memory. busy/busyOp: one sign-in call at a time. notice: 'refused' (a status
  // check refused on a view that did not run the link) or 'closing' (a begin on the setup page that never reached the
  // computer), each cleared when the read's sign-in changes. noOffer: the page answered "Go to the update" with no
  // offer to show; cleared by a read with a link marker and by a new connection.
  let noOffer = false, waiting = false, stopShown = false, stopTimer = null, settling = 0, sg = null, busy = false, busyOp = null,
    epochs = 0, notice = null, noticeFor = null;
  const ready = () => client.snapshot().state === 'ready';
  function wait(on) {
    clearTimeout(stopTimer); stopTimer = null; stopShown = false; waiting = on;
    if (on) stopTimer = setTimeout(() => { stopTimer = null; if (!disposed && waiting) { stopShown = true; onChange(); } }, STOP_AFTER_MS);
  }
  function connectionChanged(state) {
    const before = last; last = state;
    // Renewing or checking the same session (ready, renewing or checking, ready again) moves the epoch once and keeps
    // the answer: the view stays as it was through it and no read is sent for it. It must end ready on the computer the
    // answer came from; any other change, or an end anywhere but ready, drops the answer.
    const passing = value => value === 'renewing' || value === 'checking';
    const renewal = before.state === 'ready' && passing(state.state)
      || passing(before.state) && state.state === 'ready' && (state.read_profile?.host_uid ?? null) === current?.host;
    if (current && renewal && current.epoch === before.epoch) { current = {...current, epoch:state.epoch}; readFor = state.epoch; }
    else {
      if (state.epoch !== before.epoch) noOffer = false; // a new connection: the page is asked again
      if (current && (current.epoch !== state.epoch || state.state !== 'ready')) { current = null; dropSignin(); }
    }
    if (state.state === 'ready' && readFor !== state.epoch) void read();
  }
  async function read() {
    if (disposed || !ready() || typeof client.setupState !== 'function') return false;
    if (hold()) { held = true; return false; }
    held = false;
    // A read in flight was sent before whatever asks now (an approval, a begin, a new connection, a press), so its
    // answer never stands in for this one: once it settles one fresh read is sent, and every request meanwhile shares it.
    if (reading) return queued ??= new Promise(resolve => { wake = resolve; });
    const epoch = client.snapshot().epoch;
    readFor = epoch;
    reading = (async () => {
      let value = null;
      try { value = answer(await client.setupState()); } catch { value = null; }
      if (disposed || !ready() || client.snapshot().epoch !== epoch) return false;
      // An unusable or lost refresh keeps an answer already shown for this connection.
      if (value) current = {...value, epoch, host:client.snapshot().read_profile?.host_uid ?? null};
      else if (current?.epoch !== epoch) { current = null; dropSignin(); }
      if (value) { syncSignin(); if (value.link !== null) noOffer = false; }
      return !!value;
    })();
    try { return await reading; }
    finally {
      reading = null;
      if (queued) { const resolve = wake; queued = wake = null; resolve(disposed ? false : read()); }
      if (!disposed) onChange();
    }
  }
  const signinKey = () => current?.signin ? `${current.signin.generation}/${current.signin.phase}/${current.signin.attempt_id}` : null;
  function dropSignin() {
    if (!sg) return;
    const old = sg; sg = null; old.dead = true; old.module.stopPoll(); old.module.dispose();
    mount?.claude.step.replaceChildren();
  }
  // Keeps the sign-in step in line with the read: one for a link approval that has not ended, resumed from the read's
  // attempt (its url and time left come from setup-signin-status), none once it ended, Claude is linked or it is gone.
  function syncSignin() {
    const signin = current?.signin ?? null;
    if (noticeFor !== signinKey()) { notice = null; noticeFor = signinKey(); }
    // A read sent before a Try again can say the sign-in ended while that begin is in flight: the step waits for its answer.
    if (mount && signin?.phase === 'ended' && current.provider !== 'linked' && sg?.beginning && sg.generation === signin.generation) return;
    if (!mount || !signin || current.provider === 'linked' || signin.phase === 'ended') { dropSignin(); return; }
    const have = sg?.module.attempt()?.attempt_id ?? null;
    if (sg && sg.generation === signin.generation && (signin.attempt_id === have || busy)) return;
    dropSignin();
    if (notice === 'refused') return; // this page may not carry it; nothing is asked of it again until the sign-in changes
    startSignin(signin.generation, signin.attempt_id, 'resume');
    if (signin.attempt_id !== null) void runOp('signin-status', true);
  }
  function startSignin(generation, attemptId, mode) {
    dropSignin();
    // beginning: its begin is in flight; lost: its begin failed with no refusal, so may or may not have reached the computer.
    const g = {dead:false, epoch:++epochs, generation, mode, answered:false, beginning:false, lost:false, module:null};
    g.module = createClaudeSignin({calls:(action, input) => client.setupSignin(action, input),
      // No storage: the one kept attempt is the computer's, named by the parts read.
      store:{read:() => attemptId === null ? null : JSON.stringify({generation, attempt_id:attemptId}), write:() => {}},
      host:{approval:() => ({generation, expires_at:Number.MAX_SAFE_INTEGER, provider_signin:true}), pending:() => false,
        busy:() => busy, busyOperation:() => busyOp, active:() => !disposed && !g.dead && ready(), disposed:() => disposed || g.dead,
        epoch:() => g.epoch, changed:() => onChange(), run:(operation, options) => runOp(operation, options?.quiet === true)},
      now, visible, setTimeout, clearTimeout, copyText});
    g.module.load();
    sg = g;
    if (mount) {
      const make = mount.make, row = (...nodes) => { const node = make('div','setup-actions'); node.append(...nodes); return node; };
      const action = (label, className, operation) => { const node = make('button', className, label); node.type = 'button';
        node.addEventListener('click', () => { if (!node.disabled) void runOp(operation); }); return node; };
      mount.claude.step.replaceChildren();
      g.module.mount({make, action, row, repaint:() => onChange(), step:before => {
        const body = make('div','setup-step-body'); mount.claude.step.append(...before, body); return {body}; }});
    }
    return g;
  }
  // One sign-in call, as the page's own setup panel runs one (claude-signin.js operate/failed), with the link road's
  // difference: a call this page refuses before any answer came is not an error, it is the line that sends the person
  // to the page where they added this computer (or, for Try again, Link Claude).
  async function runOp(operation, quiet = false) {
    const g = sg;
    if (!g || g.dead || busy || disposed) return false;
    busy = true; busyOp = quiet || !Object.hasOwn(SIGNIN_PROGRESS, operation) ? null : operation;
    if (operation === 'signin-begin') g.beginning = true;
    onChange();
    let ok = false;
    try { ok = await g.module.operate(operation, g.epoch); if (ok) g.answered = true; }
    catch (error) {
      const restored = operation === 'signin-retry' ? g.module.restoreRetry(error) : '';
      if (!disposed && !g.dead) {
        if (g.answered || restored) g.module.failed(operation, error, restored, g.epoch);
        else if (onSetupPage && (operation === 'signin-begin' && (refusal(error) || error?.code === 'signin_not_started')
          || g.lost && refusal(error))) {
          // This page ran the link and began the sign-in itself, and that begin never reached the computer: the computer
          // refused it, this browser never sent it, or the computer refused a later call for the attempt of a begin whose
          // answer was lost (core refused that check, which also stops that attempt here). The plan's "begin refused" row.
          // Nothing started, so the attempt is dropped.
          notice = 'closing'; noticeFor = signinKey(); dropSignin();
        }
        // A lost or failed answer on the page that ran the link (a begin that may in fact have started, or a check) is
        // an ordinary failure: the step keeps its attempt and offers Check sign-in progress.
        else if (onSetupPage) { if (operation === 'signin-begin') g.lost = true; g.module.failed(operation, error, restored, g.epoch); }
        // Off the setup page, only the computer's refusal of a check this view never ran the link for sends the person
        // back to the setup page. A lost or failed answer proves nothing: the step keeps its attempt and offers Check
        // sign-in progress, as it does once any call has been answered.
        else if (refusal(error)) { notice = 'refused'; noticeFor = signinKey(); dropSignin(); }
        else g.module.failed(operation, error, restored, g.epoch);
      }
    }
    finally {
      busy = false; busyOp = null; g.beginning = false;
      if (!disposed && !g.dead) {
        onChange(); g.module.schedule();
        if (ok && operation === 'signin-begin') void read(); // the read now names this attempt
        // An ending is drawn from the status answer itself (endedView); no timer reads the parts. The next
        // event read (focus, visibility, pageshow, run-ended) refreshes.
      } else if (!disposed) onChange();
    }
    return ok;
  }
  function beginSignin(mode) {
    const signin = current?.signin;
    // Only Try again starts one by this road's own press: the last read says the sign-in ended, or the step's own
    // status check saw it end (the poll reads nothing, so the read may still say waiting).
    const own = sg && sg.generation === signin?.generation && ['needs_attention','cancelled','unknown'].includes(sg.module.state());
    if (!mount || !signin || signin.phase !== 'ended' && !own) return false;
    startSignin(signin.generation, null, mode);
    void runOp('signin-begin');
    return true;
  }
  // The page's answer to this view's request (the entry checked who sent it and its exact keys).
  async function runEnded(message) {
    if (disposed || !message) return false;
    if (message.run !== 'link') {
      // An update answer: stopped means the page showed its offer; refused (other) means it has none to show, so the
      // button that asked for it goes (N12's line stays).
      if (message.outcome !== 'refused') return false;
      noOffer = true; onChange(); return true;
    }
    // An approval this view waited for keeps its waiting line with no button until that read answers, so Link Claude never
    // flashes back between an approval and its begin. Any other answer, or one this view never waited for, settles nothing.
    const expecting = waiting, settle = expecting && message.outcome === 'approved';
    wait(false); if (settle) settling++;
    onChange();
    let value = false;
    try { value = await read(); } // once, fresh (never one sent before this answer); the only read this answer asks for
    finally { if (settle) settling--; }
    if (!disposed) onChange();
    // Approved and still waiting: the sign-in begins by itself, once. After Stop waiting it waits for its own button.
    if (message.outcome === 'approved' && expecting && value && !disposed && !busy && current?.signin?.phase === 'approved'
      && current.signin.attempt_id === null) {
      if (!sg) startSignin(current.signin.generation, null, 'begin');
      void runOp('signin-begin');
    }
    return true;
  }
  function pressLink() {
    if (!onSetupPage || busy || waiting || settling) return; // a second press of a double-click asks nothing more
    notice = null;
    if (request('link') === true) { wait(true); onChange(); }
  }
  // An answer kept through a renewal or a session check stays shown while it lasts.
  const live = () => !!current && ['ready','renewing','checking'].includes(client.snapshot().state) && client.snapshot().epoch === current.epoch;
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
    // The Claude part, inside the provider row: one line, at most one button, and the lifted sign-in step.
    const claudeBox = make('div','part-claude'), line = make('p','part-claude-line'), actions = make('div','setup-actions'),
      button = make('button','pill setup-primary part-claude-button'), stop = make('button','pill ghost part-claude-button',CLAUDE_TEXT.stop),
      step = make('div','part-claude-step');
    line.setAttribute('role','status'); button.type = 'button'; stop.type = 'button'; actions.append(button,stop);
    claudeBox.append(line,actions,step); rows.provider.row.append(claudeBox);
    button.addEventListener('click', () => {
      if (button.disabled) return;
      const operation = button.dataset.operation;
      if (operation === 'link') pressLink();
      else if (operation === 'update') request('update'); // only asks the page to show its own offer; the view never updates anything
      else if (operation === 'try_again') { if (!busy && beginSignin('retry')) onChange(); }
      else if (operation === 'check_again') void read();
    });
    stop.addEventListener('click', () => { wait(false); onChange(); });
    mount = {root,rows,control,reason,make,claude:{box:claudeBox,line,button,stop,step}};
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
    const linked = current.provider === 'linked' || sg?.module.state() === 'connected'; // the step's own answer counts before a read says it
    set('provider', names?.provider ?? PART_TEXT.provider, linked ? 'linked' : current.provider, linked ? PART_TEXT.linked : PART_TEXT.notLinked);
    set('bots', names?.bots ?? PART_TEXT.bots, current.teams ? 'teams' : 'none',
      current.teams === 1 ? PART_TEXT.oneTeam : current.teams ? '' : PART_TEXT.none);
    control.hidden = reason.hidden = current.teams > 0;
    root.dataset.teams = String(current.teams);
    renderClaude();
    // The helper has no name in core: drawn only when the embedding page names it.
    rows.helper.row.hidden = !names?.helper;
    if (names?.helper) set('helper', names.helper, current.helper, current.helper === 'later' ? PART_TEXT.later : PART_TEXT.asleep);
  }
  // What the Claude part shows for this read (the table in the plan's section on the Claude part): a line, at most one
  // button, or the lifted sign-in step.
  // The ending a status check just reported, before any read has said it: the same line and button the read would give.
  function endedView() {
    const m = sg?.module, state = m?.state();
    if (!['connected','needs_attention','cancelled','unknown'].includes(state)) return null;
    if (state === 'connected') return {line:CLAUDE_TEXT.saved};
    // A status cannot say whether the computer still holds an unconfirmed sign-in, so that ending keeps Check again,
    // whose read says.
    const unconfirmed = state === 'unknown' && m.reason() === 'child_quiet_unproven';
    const ending = linkEnding({state, reason:m.reason(), code_sent:m.flags().sent, retryable:m.retryable(), unconfirmed});
    return {line:ending.line, button:ending.button === 'link' && !onSetupPage ? null : ending.button};
  }
  function claudeView() {
    const c = current, signin = c.signin;
    // Waiting for the page's answer: the line, and Stop waiting only once the wait has lasted (never where Link Claude was before then).
    const waitingView = {line:CLAUDE_TEXT.waiting, button:stopShown ? 'stop' : null};
    if (c.provider === 'linked' || sg?.module.state() === 'connected') return {line:CLAUDE_TEXT.saved};       // linked wins over any sign-in
    if (notice === 'closing') return {line:LINK_CLOSING, button:onSetupPage ? 'link' : null};
    if (signin && signin.phase !== 'ended') {
      const done = endedView();
      if (done) return done;
      if ((waiting || settling) && signin.attempt_id === null) return waitingView;
      return notice === 'refused' ? {line:CLAUDE_TEXT.elsewhere} : {step:true};
    }
    // A Try again answered after this read: the step follows the new attempt (also one whose begin lost its answer, which
    // may have started, so it keeps it and offers Check sign-in progress) until a read names the current attempt.
    if (signin && !busy && sg?.generation === signin.generation && (sg.module.attempt()?.attempt_id ?? signin.attempt_id) !== signin.attempt_id)
      return endedView() ?? {step:true};
    if (signin) {
      const ending = linkEnding(signin), button = ending.button;
      return {line:ending.line, button:button === 'link' && !onSetupPage ? null : button};
    }
    if (c.link === 'not_here') return {};                                // a nine-card computer links Claude in its own road
    if (c.link === null) return onSetupPage ? (noOffer ? {line:CLAUDE_TEXT.update} : {line:CLAUDE_TEXT.update, button:'update'}) : {line:CLAUDE_TEXT.elsewhere};
    if (!onSetupPage) return {line:CLAUDE_TEXT.elsewhere};
    return waiting || settling ? waitingView : {button:'link'};
  }
  function renderClaude() {
    const {box,line,button,stop,step} = mount.claude, view = claudeView();
    const text = view.line ?? '';
    if (line.textContent !== text) line.textContent = text;
    line.hidden = !text;
    const main = view.button && view.button !== 'stop' ? view.button : null;
    button.hidden = !main; stop.hidden = view.button !== 'stop';
    if (main) {
      button.dataset.operation = main;
      const label = main === 'link' ? CLAUDE_TEXT.link : main === 'update' ? CLAUDE_TEXT.goUpdate : LINK_ENDING_BUTTONS[main];
      if (button.textContent !== label) button.textContent = label;
      button.disabled = busy || !ready();
    }
    step.hidden = !view.step;
    box.hidden = !text && !main && view.button !== 'stop' && !view.step;
    if (view.step && sg) {
      const f = sg.module.flags();
      const lead = f.typed ? 'code' : f.waitingLink ? 'link' : f.retryOffered ? 'retry'
        : f.manualCheck && !f.ended && f.refusal !== 'attempt' ? 'status' : null;
      sg.module.paint({live:ready(), busy, shown:true, flags:f, filled:(name, quiet) => lead === name ? 'pill setup-primary' : quiet});
    }
  }
  return Object.freeze({connectionChanged, refresh:read, started, render, mountView, runEnded,
    // The entry's hold ended: a read it kept back is sent now, once; with none kept back nothing is sent.
    release:() => held ? read() : Promise.resolve(false),
    teams:() => started() ? current.teams : null,
    // The computer's read says it has no team and no Launch under way (true of a reset computer too).
    noTeam:() => live() && current.teams === 0 && current.team === 'none',
    // Whether an answer for this connection is held, whatever it says; until one is, the entry keeps its form.
    answered:() => live(),
    // The computer says a Launch it dispatched has not finished cleanly (in progress, or needing attention). Not tied to
    // started(): a first team's Launch runs on a computer that reads not_started until its configuration is committed,
    // and the panel must not say "nothing runs yet" there either. It only hides that line and holds the panel.
    launchUnfinished:() => live() && current.team === 'launch_unfinished', get root() { return mount?.root ?? null; },
    dispose() { dropSignin(); clearTimeout(stopTimer); stopTimer = null; disposed = true; current = null; mount = null; }});
}
