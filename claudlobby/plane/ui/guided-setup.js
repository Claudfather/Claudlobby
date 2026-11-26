// One first-team approval; the existing Git operation owns preparation and launch.
import { createClaudeSignin, SIGNIN_PROGRESS, POLL_NOTE } from "./claude-signin.js";
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
// A prepared Preview, whether it came as Preview's own response or inside the context read after a lost one.
const badPlan = (value, selected) => !value || value.version !== selected.version || !request(binding(value)) || value.generation !== selected.generation
  || value.source_revision !== selected.source_revision || 'plan_id' in value || 'release_id' in value || 'release_seal' in value
  || value.fleets?.length !== 1 || value.fleets[0] !== selected.fleet || value.dispatch !== 'prepared' || value.phase !== 'quiesce'
  || !Array.isArray(value.changes) || !value.effects || typeof value.effects !== 'object' || Array.isArray(value.effects)
  || !Number.isFinite(value.expires_at);
const phases = ['unknown','prepare','quiesce','git','environment','publish','migrate','commit','restore','done'];
// What the computer says became of this approval's Preview (owner_setup.py SetupEngine.context); absent beside a reserved
// launch or a kept request it cannot show as a plan.
const PREVIEW_STATES = ['none','preparing','ready','expired','failed'];
// A preparing Preview is read again every 5 s for at most 10 minutes (its plan's own window), then the page stops and says so.
const PREVIEW_POLL_MS = 5000, PREVIEW_POLL_SECONDS = 600;
const onComputer = value => value.continuation ?? 'this installation’s stable CLI with host update continue';
const PREVIEW_NOTES = {
  checking:'Checking what happened to the preview…',
  preparing:'Your computer is still preparing your team. This page checks again by itself.',
  stopped:'Automatic checks stopped after 10 minutes. Your computer was still preparing your team then. Choose Check preview to look again.',
  ready:'Your team is ready to launch.',
  expired:value => `The preview ran out before you chose Launch team, so this page can no longer launch it. To launch it from your computer, run there: ${onComputer(value)}`,
  failed:value => 'Preparing your team stopped on your computer before it finished. ' + (value.continuation
    ? `To retry it there, and launch your team if that works, run: ${value.continuation}` : 'This setup cannot go on from this page.'),
  refused:'Your computer refused to start the preview, so nothing was prepared.',
  notStarted:'The preview did not start on your computer, so nothing was prepared.',
  readRefused:'Your computer refused this setup, so this page cannot go on with it.',
  readExpired:'Setup approval on your computer has ended, so this page cannot go on with this setup.',
  readRejected:'Your computer’s answer about this setup did not match what this page expects. Choose Check preview to look again.',
  readLost:'Could not get an answer from your computer about the preview. Choose Check preview to look again.',
  lost:'The connection to your computer changed, so this page stopped checking on the preview. Once connected again, choose Check setup approval.',
  kept:'Choose Check launch status to see where setup stands on your computer.'};
const refusal = error => [400,401,403,409].includes(error?.status);
// An answer this page will not use; an approval past its end comes apart from a malformed one.
const rejected = (code = 'setup_answer_rejected') => Object.assign(new Error('Setup answer not used.'), {code});
// Presentation only: the step that shows each operation's progress, and its text.
const PROGRESS = {context:['approve','Checking setup approval…'],...SIGNIN_PROGRESS,
  preview:['goal','Preparing your team preview…'],'preview-check':['goal','Checking on the preview…'],
  launch:['launch','Launching your team…'],receipt:['launch','Checking launch status…']};
// Skips unchanged writes, so a status region is not re-announced on every two-second check.
const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
// The control a visitor uses next: the filled action, else Sign in to Claude, the goal or Launch team, whichever
// is shown and enabled. Null when none is, so focus is left where it is.
const nextAction = m => [m.lead,m.signinControls.begin,m.area,m.controls.launch].find(control => {
  if (!control || control.disabled) return false;
  for (let node = control; node && node !== m.root; node = node.parentNode) if (node.hidden) return false;
  return true;
}) ?? null;
const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
// Above Launch team, outside the drawer, beside a launchable Preview. A Preview's first team is always one of the two
// package templates in examples/teams/ (OwnerSetup's Preview runs fleet setup with the approval's template, written by
// commands/setup.py _template_config), each a lead, an engineer and a reviewer under the task-scoped-effects guardrail,
// which asks the person first for these four.
const LAUNCH_STARTS = 'Launch starts your lead, engineer and reviewer on this computer. The lead plans the work, the engineer does it and the reviewer checks it. Merging, deploying, extra spending and destructive changes each need your permission first.';
const OTHERS_RESTART = 'Bots already running here restart briefly.';
// The drawer's name, the one the website's permission cards use (Claudfather/claudfather.ai#174).
const TECHNICAL_DETAILS = 'Technical details';
// Launch restarts every bot its plan declares (config_staging.py restart_bots: every bot, whatever the plan changes),
// each named "<team>/<bot>"; the Preview's own team is its one fleet. Any other name is a bot already running here.
// A plan that does not list them as names cannot rule that out, so it says so too.
const othersRestart = plan => {
  const bots = plan.effects?.restart_bots, own = plan.fleets[0] + '/';
  return !Array.isArray(bots) || bots.some(name => typeof name !== 'string' || !name.startsWith(own));
};
// Shown above Launch team when the team card carried the messages mark (string S24): which form
// follows the Preview answer's finish, page when this page must stay open for the goal to be sent.
const GOAL_SENT = {
  page:"Launch sends this goal to your team's lead as your first message, once the team is running. Keep this page open until then. A sent goal cannot be unsent.",
  computer:"Launch sends this goal to your team's lead as your first message, once the team is running. You can close this page. A sent goal cannot be unsent."};
// A Preview answer's goal fields: with the mark, the finish and the exact goal; without it, no finish.
const badGoal = (value, selected) => (value.sends_goal === true) !== (selected.sends_goal === true)
  || value.sends_goal !== undefined && (!['page','computer'].includes(value.finish) || typeof value.goal !== 'string' || value.goal.length > 2000)
  || value.sends_goal === undefined && 'finish' in value;

export function createGuidedSetup({client, storage, storageKey, now = () => Date.now()/1000, onChange = () => {},
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout, visible = () => true,
  copyText = text => globalThis.navigator.clipboard.writeText(text)}) {
  let context = null, plan = null, pending = null, conflicts = [], legacy = [], incompatible = false;
  let goal = '', planGoal = '', note = '', launchable = false, busy = false, disposed = false, epoch = 0, mount;
  let state = client.snapshot();
  // The Claude sign-in step is its own module (claude-signin.js); this page gives it the approval, the one kept
  // attempt's storage (the team road keeps it in this browser) and the page around it.
  const signin = createClaudeSignin({calls:(action, input) => client.setupSignin(action,input),
    store:{read:() => storage?.getItem(storageKey+':claude'), write:text => storage.setItem(storageKey+':claude',text)},
    host:{approval:() => context, pending:() => !!pending, busy:() => busy, busyOperation:() => busyOperation, active:() => active(),
      disposed:() => disposed, epoch:() => epoch, changed:() => changed(), run:(operation, options) => run(operation,options)},
    now, visible, setTimeout, clearTimeout, copyText});
  let busyOperation=null; // presentation: which explicit operation is in flight
  // Presentation from the entry that owns Connect: {operation:'connect'|'check', disabled, busy:null|operation, hint}.
  // connectFocus follows one panel click ('sent', then 'busy') so keyboard focus lands on the next action.
  // standalone: the panel is the whole view (no computer card or workspace beside it).
  // launchedFor: the binding a clean done receipt was for; a rebound request is not launched.
  let onConnect=null, connectView=null, connectFocus=null, standalone=false, launchedFor=null;
  // followed: in this page's lifetime the panel sent a Launch, sent a launch status check or followed a hand-off.
  // Never saved: a request kept in storage from an earlier page is not followed until this page acts on it.
  // The follow ends at a clean done receipt for the current request (launched()); see following().
  let followed=false;
  // The computer said Launch sends the goal (its context's sends_goal); kept across connection changes for the line after Launch.
  let sendsGoal=false;
  const launched = () => !!launchedFor && !!pending && same(launchedFor,pending);
  let managedHandoff = false, handoffTimer = null, handoffReads = 0, connectionChecked = false, handoffUntil = 0;
  // launchHeld: the request the computer last said holds a Launch (launch_reserved, from a launch status or context read).
  // In the whole view the steps already done stay done while it holds: the hand-over's own connection check moves the
  // connection on, which clears the approval context (connectionChanged). handoffChecking: that check is in flight.
  // An answer that contradicts the launch, or a lost connection, lets go of it, so approval is checked again; a pause of
  // the automatic checks does not. launchSeen: the request the computer ever said was reserved; never let go, so the
  // line that nothing runs yet stays hidden for that request.
  let launchHeld = null, handoffChecking = false, launchSeen = null;
  // launchUnderway: the computer's parts read says a Launch it dispatched has not finished (the entry passes it on).
  // Presentation only, for a page opened again before it read anything: the line that nothing runs yet is hidden,
  // and with this browser's own request kept, the approval is shown done. It sends, resumes and follows nothing.
  let launchUnderway = false;
  const launchHolding = () => standalone && !!pending && !!launchHeld && same(launchHeld,pending);
  // An approval is known for this page's setup: one the computer holds a launch for, a launch it says is under way, or
  // a live approval read. Until one is, Check setup approval leads (render) and a failed approval read says so (run).
  const approvalKnown = (time = now()) => launchHolding() || standalone && launchUnderway && !!pending
    || !!context && context.expires_at > time;
  const releaseLaunch = () => { if (!launched()) launchHeld = null; };
  const holdLaunch = reserved => { launchHeld = reserved ? pending : null; if (reserved) seeLaunch(pending); };
  // launchSeen is also kept in this browser (beside the request, as the sign-in attempt is), so a page opened again
  // still knows the computer said this request's Launch was reserved. Presentation only; a failed write changes nothing.
  const seeLaunch = value => {
    if (value === launchSeen || value && launchSeen && same(value,launchSeen)) return;
    launchSeen = value; try { storage?.setItem(storageKey+':launch',JSON.stringify(value)); } catch {}
  };
  // previewState: the computer's last preview_state for this approval (null: not said). previewWatch: a Preview was sent
  // from this approval, or the computer reported one, so reads keep the steps already done instead of clearing them.
  // previewTried: how this page's last Preview ended without a plan ('refused' by the computer, or 'lost').
  // previewProblem: the last read about it was refused ('refused'), showed the approval ended ('expired'), was not
  // usable ('rejected') or got no answer ('lost'); only the last two are worth looking again. previewChecking: the
  // read that follows a failed Preview is queued. previewUntil/previewStopped: the bound on automatic checks.
  let previewState = null, previewWatch = false, previewTried = null, previewProblem = null, previewChecking = false;
  let previewTimer = null, previewUntil = 0, previewStopped = false;
  // Preview team can be sent only while the computer holds no preview for this approval (it refuses a second one).
  const previewOpen = () => (previewState === null || previewState === 'none') && !previewProblem && !previewChecking;
  const previewPolling = () => previewState === 'preparing' && !previewStopped && !previewProblem;
  function stopPreviewPoll() { clearTimeout(previewTimer); previewTimer = null; }
  function schedulePreviewPoll() {
    if (previewTimer !== null || disposed || !context || pending || !previewPolling()) return;
    if (!previewUntil) previewUntil = now() + PREVIEW_POLL_SECONDS;
    if (now() >= previewUntil) { previewStopped = true; note = PREVIEW_NOTES.stopped; changed(); return; }
    if (!visible() || !active()) return; // Resumes with resumeHandoff when this view/connection returns.
    const ticket = epoch;
    previewTimer = setTimeout(() => {
      previewTimer = null;
      if (disposed || ticket !== epoch || !previewPolling()) return;
      if (busy) { schedulePreviewPoll(); return; } // never overlaps another call
      void run('preview-check',{quiet:true}); // its completion schedules the next check
    },PREVIEW_POLL_MS);
  }
  function stopHandoff(paused = false) { managedHandoff = false; clearTimeout(handoffTimer); handoffTimer = null; if (!paused) releaseLaunch(); }
  function scheduleHandoff() {
    if (!managedHandoff || handoffTimer !== null || disposed || !pending) return;
    if (!handoffUntil) handoffUntil = now() + 600;
    if (now() >= handoffUntil || handoffReads >= 80) {
      stopHandoff(true); note = standalone ? 'Automatic checks paused. Choose Check launch status to look again. Do not launch again.'
        : 'Automatic checks paused. Choose Check connection on the computer card, then Check launch status. Do not launch again.'; changed(); return;
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
          stopHandoff(); note = 'The launch status did not match this setup. Do not launch again; inspect the update on the computer.'; return;
        }
        if (!value.launch_reserved) { stopHandoff(); return; }
        if (!connectionChecked && typeof client.check === 'function') {
          handoffChecking = true;
          try { await client.check(); } finally { handoffChecking = false; }
          connectionChecked = true;
        }
        if (value.state === 'done' || value.state === 'unknown' || Object.keys(value.health_errors).length) stopHandoff();
      } catch (error) {
        if (!disposed && [400,401,403,409].includes(error?.status)) {
          stopHandoff(); note = 'The computer refused the connection or launch status check. Choose Check connection; the launch is not repeated.';
        } else if (!disposed && managedHandoff) note = 'Starting your team’s connection. The launch is not repeated.';
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
    signin.load();
    try { const seen = JSON.parse(storage?.getItem(storageKey+':launch') ?? 'null'); if (request(seen)) launchSeen = binding(seen); }
    catch { /* unreadable: nothing is known about a launch, as before */ }
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
  // unconfirmed: the request is only what this browser kept from an earlier visit. It still blocks a second launch, and
  // its Check launch status leads once an approval is known (render), but no step is drawn done for it until the
  // computer says something about it in this visit (an accepted read of the approval, a launch status, a Preview, or
  // its parts read saying a Launch is unfinished). A computer that was reset otherwise shows a finished step it never
  // had (40-02).
  let unconfirmed = !!pending;
  const drawn = () => !!pending && !unconfirmed;
  const active = () => !disposed && ['ready','setup_only'].includes(client.snapshot().state);
  const legacyBlocked = () => incompatible || legacy.length > 0;
  function save(value, kept = conflicts) {
    try {
      if (incompatible || !storage || typeof storage.setItem !== 'function') throw new Error();
      storage.setItem(storageKey,JSON.stringify({version:2,pending:value,conflicts:kept,legacy}));
    } catch { throw Object.assign(new Error('This browser could not save the setup identifiers, so nothing was launched. Allow site storage for this page, then try again.'), {code:'setup_storage_unavailable'}); }
  }
  function changed() { render(); onChange(); }
  // The kept request belongs to another approval, which the computer no longer holds (see the approval read in run):
  // it joins the conflicting ones, still shown under Technical details, and this page stops following it. Saved first, and only
  // then changed here, so a browser that cannot save keeps it as before. Conflicting identifiers gate nothing, so a full
  // list drops its oldest one here, instead of a computer set up again many times stopping at the cap.
  function setAside() {
    const next = conflicts.some(item => request(item) && same(item,pending)) ? conflicts
      : [...conflicts.slice(Math.max(0,conflicts.length-15)),Object.freeze({...pending})];
    save(null,next); conflicts = next; pending = null;
    stopHandoff(); followed = false; handoffUntil = 0; handoffReads = 0; connectionChecked = false;
  }
  const RENEWAL_STATES=['renewing','ready','setup_only'];
  function connectionChanged(next) {
    // The client's own pre-begin renewal of a still-valid session rotates the epoch but is not
    // a loss: the approved context and its effect survive it. Any other state (expired, denied,
    // signed out, unavailable, disposed) still takes the full path below.
    if (next.signinRenewal===true && RENEWAL_STATES.includes(next.state) && RENEWAL_STATES.includes(state.state)) {
      state = next; changed(); return;
    }
    if (next.epoch !== state.epoch || next.state !== state.state) {
      // A check on the preview that was queued, running or scheduled stops here; say so instead of leaving it on screen.
      if (previewChecking || previewTimer !== null || busyOperation === 'preview-check' || busy && previewPolling()) note = PREVIEW_NOTES.lost;
      stopPreviewPoll(); previewState = previewTried = previewProblem = null; previewWatch = previewChecking = previewStopped = false; previewUntil = 0;
      epoch++; context = null; plan = null; launchable = false;
      signin.lost(); // only a live link can be lost
    }
    state = next;
    if (['denied','signed_out','expired','disposed'].includes(next.state)) releaseLaunch();
    if (managedHandoff && ['denied','signed_out','expired','disposed'].includes(next.state)) {
      stopHandoff(); note = standalone ? 'Connection needs attention. Choose Connect above; the launch is not repeated.'
        : 'Connection needs attention. Reconnect from the computer card; the launch is not repeated.';
    }
    changed(); scheduleHandoff();
  }
  // quiet: the automatic poll shows steady text, not a progress flash.
  // preview-check: the same read as context, asking the computer what became of a Preview. Once a Preview was sent or
  // the computer reported one (previewWatch), a read keeps the approval and the steps already done on screen.
  async function run(operation, {quiet = false} = {}) {
    if (disposed || busy || !active()) return false;
    const reading = operation === 'context' || operation === 'preview-check';
    if (operation === 'preview-check' && (!context || pending)) return false;
    // Beside a fresh launchable plan a failed read still retires Launch, as before.
    const keep = reading && !launchable && (previewWatch || operation === 'preview-check');
    if (reading && !keep) { context = null; launchable = false; }
    if (reading) { stopPreviewPoll(); previewChecking = false; }
    if (operation === 'preview-check' && !quiet) { previewStopped = false; previewUntil = 0; } // a click looks again for 10 minutes
    const ticket = epoch; busy = true; busyOperation = quiet || !Object.hasOwn(PROGRESS,operation) ? null : operation; changed();
    try {
      if (reading) {
        const value = await client.setupContext();
        if (disposed || ticket !== epoch || !active()) return false;
        if (Number.isFinite(value?.expires_at) && value.expires_at <= now()) throw rejected('setup_approval_expired');
        if (!value || !(value.version === 2 && value.template === 'revenue-web-app' && typeof value.repository === 'string' && !('project_directory' in value)
            || value.version === 3 && value.template === 'local-project' && typeof value.project_directory === 'string'
              && value.project_directory.startsWith('/') && !('repository' in value)) || !uuid(value.generation)
            || typeof value.fleet !== 'string' || !revision(value.source_revision)
            || value.managed_owner !== undefined && value.managed_owner !== true
            || value.sends_goal !== undefined && value.sends_goal !== true
            || 'release_id' in value || 'release_seal' in value || 'plan_id' in value
            || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || value.launch_reserved && value.request === null || !Number.isFinite(value.expires_at) || value.expires_at <= now()
            || value.request !== null && (!request(value.request) || value.request.generation !== value.generation
              || value.request.source_revision !== value.source_revision)) throw rejected();
        // The computer's word on this approval's Preview: none and preparing and failed keep no request; ready and expired
        // keep one; nothing is said beside a reserved launch; a plan comes only with ready.
        const said = value.preview_state;
        if (said !== undefined && (!PREVIEW_STATES.includes(said) || value.launch_reserved
            || ['none','preparing','failed'].includes(said) === (value.request !== null))
            || (value.preview !== undefined) !== (said === 'ready')) throw rejected();
        // A Preview the computer finished and kept: offered while its window is open, never beside a reserved launch.
        const kept = value.preview && !value.launch_reserved && value.request && value.preview.expires_at > now() ? value.preview : null;
        if (value.preview !== undefined && (!value.request || value.launch_reserved
            || kept && (badPlan(kept,value) || !same(kept,value.request) || typeof kept.goal !== 'string' || kept.goal.length > 2000
              || badGoal(kept,value)))) throw rejected();
        context = Object.freeze({...value}); plan = null; launchable = false; previewProblem = null; sendsGoal = value.sends_goal === true;
        previewState = said ?? null;
        if (said !== undefined && said !== 'none') previewWatch = true;
        if (said !== 'preparing') { previewUntil = 0; previewStopped = false; }
        // An approval that holds no request, under another generation than a request or sign-in this browser kept: the
        // computer holds neither, as after it was set up again from nothing. It cannot get here while it keeps that
        // request: a new approval is refused while any Preview or Launch is retained (OwnerSetup.allow), and this read is
        // refused while its sign-in attempt belongs to another approval (provider_signin.py _row). Never launched; see setAside.
        // The sign-in is set aside and the computer's attempt checked first, so a read rejected there changes no storage.
        if (value.request === null && value.provider_signin === true) signin.setAside(value.generation);
        if (value.provider_signin === true && value.signin_attempt) signin.receipt(value.signin_attempt);
        if (value.request === null && pending && pending.generation !== value.generation) setAside();
        if (value.request) {
          if (pending && !same(pending,value.request)) retain(pending,conflicts);
          pending = binding(value.request); save(pending);
        }
        // An accepted approval read confirms what this page still keeps: a request under another approval was set aside
        // just above, so one still kept is this approval's (a null request under it never proves the request vanished).
        unconfirmed = false;
        holdLaunch(value.launch_reserved);
        if (kept) {
          const {goal:keptGoal,...prepared} = kept;
          plan = Object.freeze(prepared); goal = planGoal = keptGoal; launchable = true; previewTried = null;
          seeLaunch(null); // the computer keeps this Preview only while nothing was dispatched, so nothing runs yet
        }
        // A ready plan whose window this browser's clock has already passed reads as expired.
        const shown = kept ? 'ready' : said === 'ready' ? 'expired' : said;
        // A null request under the same approval is never proof that an original request vanished.
        note = value.launch_reserved ? `Setup was already submitted from this browser. ${localContinue(value)} Reconnecting never launches again.`
          : shown === 'ready' ? PREVIEW_NOTES.ready
          : shown === 'expired' ? PREVIEW_NOTES.expired(value)
          : shown === 'preparing' ? previewStopped ? PREVIEW_NOTES.stopped : PREVIEW_NOTES.preparing
          : shown === 'failed' ? PREVIEW_NOTES.failed(value)
          : pending ? value.request ? PREVIEW_NOTES.kept : `Setup was already submitted from this browser. ${localContinue(value)} Reconnecting never launches again.`
          : previewTried ? previewTried === 'refused' ? PREVIEW_NOTES.refused : PREVIEW_NOTES.notStarted : '';
        if (value.managed_owner && value.launch_reserved) { managedHandoff = true; followed = true; handoffUntil = now()+600; handoffReads=0; }
      } else if (operation.startsWith('signin-')) {
        if (!await signin.operate(operation,ticket)) return false;
      } else if (operation === 'preview') {
        if (!context || context.launch_reserved || pending || legacyBlocked() || !signin.ready() || !goal.trim() || context.expires_at <= now()
            || !previewOpen()) return false;
        const selected = context, authored = goal; previewWatch = true; previewTried = null;
        const value = await client.setupPreview({generation:selected.generation,goal:authored});
        if (disposed || ticket !== epoch || selected !== context || !active()) return false;
        if (badPlan(value,selected) || value.expires_at <= now() || badGoal(value,selected)
            || value.goal !== undefined && value.goal !== authored) throw new Error();
        // With the mark, the goal shown above Launch is the one the computer read back.
        pending = binding(value); unconfirmed = false; plan = Object.freeze({...value}); planGoal = value.goal ?? authored;
        save(pending); launchable = true;
        note = PREVIEW_NOTES.ready;
      } else if (operation === 'launch') {
        if (!context || !plan || !pending || !launchable || legacyBlocked() || !signin.ready() || !same(plan,pending)
            || planGoal !== goal || plan.expires_at <= now() || context.expires_at <= now()) return false;
        save(pending); launchable = false; // Retain the preview's original identity before the one launch POST.
        managedHandoff = context.managed_owner === true; followed = true;
        const result = await client.setupLaunch(pending);
        if (disposed || ticket !== epoch || !active()) return false;
        receipt(result);
      } else if (operation === 'receipt') {
        if (!pending) return false;
        followed = true;
        const original = pending, result = await client.setupReceipt(original);
        if (disposed || ticket !== epoch || original !== pending || !active()) return false;
        receipt(result);
      } else return false;
      return true;
    } catch (error) {
      const restored = operation==='signin-retry' ? signin.restoreRetry(error) : ''; // even if the connection moved on meanwhile
      if (!disposed && ticket === epoch) {
        // A failed read leaves no approval shown beside its "could not confirm" note.
        // A Preview that failed leaves the approval and the finished steps as they were; one read then asks the computer
        // what became of it. A failed read drops them only where nothing was previewed yet, or beside a launchable plan.
        if (operation === 'preview') launchable = false;
        if (reading && !keep) { context = null; launchable = false; }
        if (reading && refusal(error)) releaseLaunch(); // as for the launch status read below
        if (reading && keep) {
          // The approval and the steps already done stay; the computer's next answer decides what is true.
          previewProblem = refusal(error) ? 'refused' : error?.code === 'setup_approval_expired' ? 'expired'
            : error?.code === 'setup_answer_rejected' ? 'rejected' : 'lost';
          note = error?.code === 'setup_storage_unavailable' ? error.message
            : {refused:PREVIEW_NOTES.readRefused,expired:PREVIEW_NOTES.readExpired,rejected:PREVIEW_NOTES.readRejected}[previewProblem]
              ?? PREVIEW_NOTES.readLost;
          return false;
        }
        if (operation === 'receipt') launchable = false;
        if (operation === 'receipt' && refusal(error)) releaseLaunch(); // a refusal contradicts the hold; a lost answer does not
        if (operation.startsWith('signin-')) return signin.failed(operation,error,restored,ticket);
        if (operation === 'preview' && !pending && error?.code !== 'setup_storage_unavailable') {
          // One read asks the computer what became of it. A refusal and a lost answer are remembered apart.
          previewTried = refusal(error) ? 'refused' : 'lost'; previewChecking = true; note = PREVIEW_NOTES.checking;
          queueMicrotask(() => {
            if (disposed || ticket !== epoch || !previewChecking) return;
            if (!active()) { previewChecking = false; note = PREVIEW_NOTES.lost; changed(); return; }
            void run('preview-check',{quiet:true});
          });
          return false;
        }
        // A failed approval read with no approval known says so, as Check setup approval then leads (render).
        note = error?.code === 'setup_storage_unavailable' ? error.message : pending && (operation !== 'context' || approvalKnown())
          ? 'The launch could not be confirmed. Choose Check launch status, or inspect the update on the computer if approval expired or was revoked. Do not launch again.'
          : 'Could not confirm setup approval. Make sure setup was approved on this computer, then check again.';
      }
      return false;
    } finally { busy = false; busyOperation = null; if (!disposed) { changed(); scheduleHandoff(); signin.schedule(); schedulePreviewPoll(); } }
  }
  // After a panel Connect, a page that kept the computer's word that this request's Launch was reserved (seen: launchSeen
  // before that Connect) reads that launch's status once, as Check launch status does (receipt changes nothing on the
  // computer). It never launches, renews or retries, and it is not this page following a launch (the click still is).
  // A failed or refused read shows nothing: nobody asked, and an approval that ended days ago refuses it.
  async function readSeenLaunch(seen) {
    if (disposed || busy || !active() || !pending || !seen || !same(seen,pending) || launched() || launchable
        || managedHandoff) return;
    const original = pending, ticket = epoch; busy = true; busyOperation = 'receipt'; changed();
    try {
      const value = await client.setupReceipt(original);
      if (disposed || ticket !== epoch || original !== pending || !active()) return;
      receipt(value);
    } catch (error) {
      if (disposed || ticket !== epoch) return;
      if (refusal(error)) releaseLaunch();
    } finally { busy = false; busyOperation = null; if (!disposed) { changed(); scheduleHandoff(); } }
  }
  function receipt(value) {
    if (!value || ![2,3].includes(value.version) || context && value.version !== context.version || !pending || !same(value,pending) || !phases.includes(value.state)
        || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || typeof value.config_committed !== 'boolean'
        || !value.health_errors || typeof value.health_errors !== 'object' || Array.isArray(value.health_errors)
        || value.managed_owner !== undefined && value.managed_owner !== true
        || 'plan_id' in value || 'release_id' in value || 'release_seal' in value) throw new Error();
    unconfirmed = false;
    if (value.launch_reserved || value.state === 'unknown') launchable = false;
    holdLaunch(value.launch_reserved);
    const done = value.state === 'done' && value.config_committed && Object.keys(value.health_errors).length === 0;
    launchedFor = done ? pending : null;
    note = done ? standalone ? 'Team launched. Your team is running on this computer.'
      : sendsGoal || plan?.sends_goal === true ? 'Team launched. Once its bots show as running, you can message the lead here.'
      : 'Team launched. Once its bots show as running, message the lead with one concrete request; the setup goal alone does not start a task.'
      : value.state === 'unknown' || Object.keys(value.health_errors).length
      ? 'The launch needs attention on the computer. Do not launch again; inspect the update there using the identifiers under Technical details.'
      : value.state === 'quiesce' && !value.launch_reserved
      ? `Prepared but not launched. ${localContinue(value)} This page never resends it after reconnecting.`
      : value.state === 'quiesce' && value.launch_reserved
      ? value.managed_owner ? 'Launch received. Your team’s connection is starting and will reconnect here. Do not launch again.'
        : 'Launch received. Let the setup guide on the computer finish its handoff, then use its status and reconnect commands. Do not launch again.'
      : 'Still in progress. Checking status never repeats or resumes setup.';
  }
  let detailsFor = null; // the plan, pending and identifier counts the Technical details JSON was last built from
  const LEGACY = 'This browser holds identifiers from an older setup, so it cannot launch a new team here. Inspect them on the computer; they are listed under Technical details.';
  function render() {
    // One clock reading per repaint, so the notes below and the expired line under Launch team agree.
    const time = now();
    // Check preview needs a live approval: once this page's clock says it ended, a note offering it says so instead.
    if (previewWatch && context && context.expires_at <= time && /Check preview/.test(note)) note = PREVIEW_NOTES.readExpired;
    // Nor does it say the team is ready once the Preview has run out, where Launch says the preview expired, or once
    // this page no longer holds it (a connection change drops it). The banner is only ever set beside a plan.
    if (note === PREVIEW_NOTES.ready && (!plan || plan.expires_at <= time)) note = '';
    if (!mount) return;
    const m = mount, live = active();
    // A launch the computer holds for this request was approved, even once the approval context is gone.
    const holding = launchHolding(), underway = standalone && launchUnderway;
    const approved = approvalKnown(time);
    // It keeps a sign-in this page saw connected for the same approval.
    const signinKnown = context?.provider_signin === true || holding && !context && signin.state() === 'connected'
      && signin.attempt()?.generation === pending.generation;
    // Sign-in is a step when this approval asks for it; until approval is known it is shown as upcoming.
    const signinShown = drawn() ? signinKnown && signin.state() === 'connected' : signinKnown || !context;
    const signinDone = signinKnown && signin.ready();
    const states = {approve:approved ? 'done' : 'current',
      signin:!signinShown ? null : signinDone ? 'done' : approved ? 'current' : 'upcoming',
      goal:drawn() ? 'done' : approved && (!signinKnown || signinDone) ? 'current' : 'upcoming',
      launch:pending ? 'current' : 'upcoming'};
    // With a Connect step, nothing after it is current until this computer is connected.
    const settling = holding && handoffChecking && ['checking','renewing'].includes(state.state);
    const connecting = !!m.steps.connect && !live && !settling;
    if (m.steps.connect) states.connect = live || settling ? 'done' : 'current';
    if (connecting) for (const name of ['approve','launch']) if (states[name] === 'current') states[name] = 'upcoming';
    // The whole view ends on finished steps, even after the handoff's status check moved the connection
    // on and cleared the approval context: nothing is left to approve or check again. Only its Connect step stays
    // current while this computer is not connected, so the view keeps its way back.
    const finished = standalone && launched();
    if (finished) for (const name of Object.keys(states))
      if (states[name] !== null && !(name === 'connect' && connecting)) states[name] = 'done';
    const progress = {approve:'',signin:'',goal:'',launch:''};
    progress.connect = connecting && connectView?.busy ? connectView.busy === 'check' ? 'Checking connection…' : 'Connecting…' : '';
    // A starting sign-in says so in its own line above the buttons instead, wherever that line is shown.
    const lineSaysIt = signin.lineSending() && ['current','done'].includes(states.signin);
    if (busy && busyOperation && !lineSaysIt) { const [step,text] = PROGRESS[busyOperation]; progress[step] = text; }
    if (!progress.signin && signinKnown && !pending && signin.polling()) progress.signin = POLL_NOTE;
    if (!progress.launch && managedHandoff) progress.launch = 'Checking automatically while your team connects…';
    // While the computer prepares the preview the goal step keeps Preview team's own line, so nothing changes between checks.
    if (!progress.goal && !pending && previewPolling()) progress.goal = PROGRESS.preview[1];
    for (const [name,step] of Object.entries(m.steps)) {
      // Before Connect, only Connect (40-02). A Launch the computer said finished cleanly in this page is no stored tick:
      // its done steps stay beside the current Connect step, which is the way back to the team (41-07).
      const state = states[name]; step.root.hidden = state === null || connecting && name !== 'connect' && !finished;
      if (state === null) continue;
      step.root.dataset.state = state;
      if (step.done) step.done.hidden = state !== 'done';
      step.body.hidden = state !== 'current';
      const text = progress[name]; setText(step.progress,text);
      if (text && text !== POLL_NOTE) step.progress.dataset.tone = 'working'; else delete step.progress.dataset.tone;
    }
    // An automatic check briefly holds every action; workspace.css dims them only once it takes a moment.
    if (busy && !busyOperation) m.root.dataset.polling = 'true'; else delete m.root.dataset.polling;
    const {controls} = m;
    setText(m.approveDone,states.approve === 'done' ? 'Setup approved' : '');
    // Once a launch is held or finished, nothing waits on Launch team any more.
    m.lede.hidden = standalone && !!pending && !!launchSeen && same(launchSeen,pending) || launched() || underway || connecting;
    // Once a preview exists for this approval the note says what is true; a new check cannot continue an ended approval.
    setText(m.approveHint,context && !approved && !previewWatch ? 'Setup approval expired. Check it again to continue.' : '');
    // Hidden until approved; never while launchable (re-checking would retire a fresh preview's launch).
    m.recheck.hidden = states.approve !== 'done' || launchable || finished;
    const flags = signin.flags();
    const launchShown = !!plan && launchable;
    // Check preview: once the computer said a preview exists that this page cannot move on, and it is not checking by itself.
    const checkShown = approved && !pending && previewWatch && !previewOpen() && !previewPolling() && !previewChecking
      && !['refused','expired'].includes(previewProblem); // a refusal or an ended approval cannot change by looking again
    // One filled action: the step that can move leads. Preview team and Launch team are filled whenever shown;
    // so is Sign in to Claude, except while it is held as sign-in starts. A retained launch's status check leads once
    // an approval for it is known; until then Check setup approval does, since that read is what can show the computer
    // was set up again (and the request set aside).
    const lead = pending && approved ? launchShown ? null : 'receipt'
      : !approved ? 'context'
      : checkShown ? 'check'
      : states.signin !== 'current' ? null
      : flags.typed ? 'code' : flags.waitingLink ? 'link' : flags.retryOffered ? 'retry'
      : flags.manualCheck && !flags.ended && flags.refusal !== 'attempt' ? 'status' : null;
    const filled = (name, quiet) => (connecting ? 'connect' : lead) === name ? 'pill setup-primary' : quiet;
    signin.paint({live,busy,shown:['current','done'].includes(states.signin),filled,flags});
    controls.context.className = filled('context','pill ghost');
    controls.receipt.className = filled('receipt','pill ghost');
    controls.previewCheck.className = filled('check','pill ghost'); controls.previewCheck.hidden = !checkShown;
    controls.preview.hidden = !previewOpen();
    m.lead = {context:controls.context,receipt:controls.receipt,check:controls.previewCheck,code:m.signinControls.code,link:m.signinLink,
      retry:m.steps.signin.retry,status:m.signinControls.status}[lead] ?? null;
    setText(m.goalDone,pending ? planGoal ? `Goal: ${planGoal}` : 'Team preview prepared' : '');
    setText(m.goalText,context ? `Your team works in ${context.template === 'local-project' ? context.project_directory : context.repository}.` : '');
    setText(m.goalHint,goal.trim() ? '' : 'Write a goal to preview your team.');
    m.area.disabled = !live || busy || !!pending || legacyBlocked() || !previewOpen();
    controls.launch.hidden = !launchShown;
    setText(m.summary,launchShown ? LAUNCH_STARTS : 'Check launch status to see where setup stands. This page never sends a launch twice.');
    setText(m.restart,launchShown && othersRestart(plan) ? OTHERS_RESTART : '');
    // The counts sit under Technical details, beside the app version and the raw plan.
    setText(m.counts,launchShown ? `Launching changes ${count(plan.changes.length,'generated file','generated files')} and starts or restarts ${count(plan.effects?.restart_bots?.length || 0,'bot','bots')}.` : '');
    m.counts.hidden = !m.counts.textContent;
    // The exact goal Launch approves, as plain text, with S24 directly above Launch team: only when Launch sends it.
    const sends = launchShown && plan.sends_goal === true;
    setText(m.launchGoal,sends ? planGoal : '');
    setText(m.launchGoalNote,sends ? GOAL_SENT[plan.finish] ?? GOAL_SENT.page : '');
    setText(m.launchHint,plan && plan.expires_at <= time ? 'This preview expired. Inspect the update on the computer.' : '');
    setText(m.status,[note, legacyBlocked() ? LEGACY : !live && !m.connect ? 'Connect this computer to continue setup.' : ''].filter(Boolean).join(' '));
    setText(m.declaration,context ? context.template === 'local-project'
      ? `Your team ${context.fleet} works in ${context.project_directory}. Once the team has finished starting and you’ve allowed messages to it, tell the lead what to do. The lead writes down the task and coordinates the work and an independent review.`
      : `Your team ${context.fleet} works on ${context.repository}, with a lead, an engineer and a reviewer. The team needs your permission before it merges a pull request.` : '');
    setText(m.authority,context ? `This approval covers ${context.template === 'local-project' ? `reading and changing files in ${context.project_directory}` : `work on ${context.repository}`}, for app version ${context.source_revision} only. Launching starts the team and the app’s background services on this computer and may use the provider accounts you set up. Some folders, tools or accounts may still need your approval on this computer. Merging, deploying, extra spending and destructive changes each need your separate permission.` : '');
    // retain only grows conflicts and legacy, and a set-aside replaces conflicts whole (setAside, which may drop the
    // oldest), so their identity and lengths track every change.
    const detailsKey = [plan, pending, conflicts, conflicts.length, legacy.length];
    if (!detailsFor || detailsKey.some((value,index) => value !== detailsFor[index])) {
      detailsFor = detailsKey;
      setText(m.details,plan || pending || conflicts.length || legacy.length ? JSON.stringify({pending,conflicting_identifiers:conflicts,
        historical_identifiers:legacy,...(plan ? {changes:plan.changes,effects:plan.effects,expires_at:plan.expires_at} : {})},null,2) : '');
    }
    m.disclosure.hidden = !context && !m.details.textContent;
    for (const [operation,button] of Object.entries(controls)) button.disabled = !live || busy
      || operation === 'preview' && (!context || !!pending || legacyBlocked() || !signin.ready() || !goal.trim() || context.expires_at <= time || !previewOpen())
      || operation === 'previewCheck' && !checkShown
      || operation === 'launch' && (!context || !plan || !pending || !launchable || legacyBlocked() || !signin.ready() || goal !== planGoal || plan.expires_at <= time || context.expires_at <= time)
      || operation === 'receipt' && !pending;
    m.recheck.disabled = controls.context.disabled;
    if (m.connect) {
      const {button,hint,text} = m.connect;
      text.hidden = finished; // once the team is launched there is no setup left to finish; the button stays
      setText(button,connectView?.operation === 'check' ? 'Check connection' : 'Connect');
      button.className = filled('connect','pill ghost');
      button.disabled = !connectView || !!connectView.disabled || !!connectView.busy;
      setText(hint,connecting && !connectView?.busy ? connectView?.hint || '' : '');
    }
    if (m.launchDone) setText(m.launchDone,finished ? 'Team launched' : '');
    // After a Connect chosen in this panel, keyboard focus moves on with the steps instead of falling to the page,
    // but only while this document has focus and nothing else took it (focus is on the page or still on Connect).
    const focusFree = () => { const doc = m.document; if (doc?.hasFocus?.() === false) return false;
      const current = doc?.activeElement; return !current || current === doc.body || current === m.connect?.button; };
    if (connectFocus && connectView?.busy) connectFocus = 'busy';
    if (connectFocus && live) {
      // Checking setup approval only reads it, so after a Connect chosen here the panel does that itself while the
      // computer is still in first-team setup. Focus then moves to the next action (Check setup approval if the read failed).
      connectFocus = null;
      queueMicrotask(async () => {
        const seen = launchSeen;
        if (client.snapshot().state === 'setup_only') await run('context');
        if (!disposed && mount === m) await readSeenLaunch(seen);
        if (!disposed && mount === m && focusFree()) nextAction(m)?.focus?.();
      });
    }
    else if (connectFocus === 'busy' && !connectView?.busy) {
      connectFocus = null; if (!m.connect.button.disabled && focusFree()) m.connect.button.focus?.();
    }
  }
  // Steps in order, each with a mark workspace.css draws from its state (✓ once done); only the current step shows its body. Long approval text, the counts and identifiers sit under Technical details.
  // connect: optional, from an entry that owns Connect for this computer; it adds a first "Connect this computer" step.
  // standalone: the panel is the whole view, so its notes never point at a computer card and a launch ends on a done step.
  function mountPanel({document,element,connect,standalone:alone = false}) {
    standalone = alone === true;
    const make = (tag,className,text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
    const action = (label,className,operation) => { const node = make('button',className,label); node.type = 'button';
      node.addEventListener('click',() => { if (!node.disabled) void run(operation); }); return node; };
    const row = (...nodes) => { const node = make('div','setup-actions'); node.append(...nodes); return node; };
    element.className = [element.className,'setup-panel'].filter(Boolean).join(' ');
    const status = make('p','setup-note'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
    const list = make('ol','setup-steps'); list.setAttribute('role','list');
    const lede = make('p','setup-lede','Nothing runs on this computer until you choose Launch team.');
    element.append(lede,status,list);
    const steps = {};
    const step = (name,title,{done = true, before = []} = {}) => {
      const root = make('li','setup-step'), mark = make('span','setup-step-mark'), progress = make('p','setup-progress');
      const doneLine = done ? make('div','setup-step-done') : null, body = make('div','setup-step-body');
      root.dataset.step = name; mark.setAttribute('aria-hidden','true'); progress.setAttribute('role','status');
      root.append(mark,make('h3','setup-step-title',title),...(doneLine ? [doneLine] : []),...before,body,progress); list.append(root);
      return steps[name] = {root,done:doneLine,body,progress};
    };
    onConnect = typeof connect === 'function' ? connect : null;
    let connectStep = null;
    if (onConnect) {
      const connectRow = step('connect','Connect this computer'), button = make('button','pill setup-primary','Connect'), hint = make('p','setup-hint');
      button.type = 'button';
      button.addEventListener('click',() => {
        if (button.disabled || !onConnect) return;
        connectFocus = 'sent'; try { onConnect(); } catch { /* the entry owns Connect and its outcome */ }
        if (connectFocus === 'sent') connectFocus = null; // the entry refused it, so nothing will move focus
      });
      connectRow.done.append(make('span','','Connected'));
      const text = make('p','setup-step-text','Connect to see what is set up on this computer.');
      connectRow.body.append(text,row(button),hint);
      connectStep = {button,hint,text};
    }
    const approve = step('approve','Approve setup'), approveDone = make('span');
    const recheck = action('Check setup approval again','pill ghost','context'), approveHint = make('p','setup-hint');
    const contextButton = action('Check setup approval','pill setup-primary','context');
    approve.done.append(approveDone);
    approve.body.append(make('p','setup-step-text','Check that this computer approved setup for this workspace.'),row(contextButton),approveHint);
    const parts = signin.mount({make,action,row,repaint:render,
      step:before => step('signin','Sign in to Claude',{done:false,before})});
    const goalStep = step('goal','Describe the goal'), goalDone = make('span'), goalText = make('p','setup-step-text');
    goalStep.done.append(goalDone);
    const area = make('textarea','setup-input'); area.maxLength = 2000; area.rows = 4; area.value = goal;
    area.setAttribute('placeholder','For example: Add a contact form to the homepage, with tests that check each message arrives.');
    area.addEventListener('input',() => { goal = area.value; changed(); });
    const goalField = make('label','setup-field'), preview = action('Preview team','pill setup-primary','preview'), goalHint = make('p','setup-hint');
    const previewCheck = action('Check preview','pill ghost','preview-check');
    goalField.append(make('span','setup-field-label','What should your team work on first?'),area);
    goalStep.body.append(goalText,goalField,row(preview,previewCheck),goalHint);
    const launchStep = step('launch','Launch your team',{done:standalone}), summary = make('p','setup-step-text'), launchHint = make('p','setup-hint');
    const restart = make('p','setup-step-text');
    const launch = action('Launch team','pill setup-primary','launch'), receiptButton = action('Check launch status','pill ghost','receipt');
    const launchGoal = make('p','setup-goal'), launchGoalNote = make('p','setup-step-text');
    launchStep.body.append(summary,restart,launchGoal,launchGoalNote,row(launch,receiptButton),launchHint);
    const launchDone = standalone ? make('span') : null; launchStep.done?.append(launchDone);
    const disclosure = make('details','setup-details'), declaration = make('p'), authority = make('p'), counts = make('p'), details = make('pre');
    disclosure.append(make('summary','',TECHNICAL_DETAILS),declaration,authority,counts,row(recheck),details); element.append(disclosure);
    mount = {root:element,document,lede,status,steps,connect:connectStep,launchDone,approveDone,recheck,approveHint,signinStatus:parts.signinStatus,signinLink:parts.signinLink,signinCopyButton:parts.signinCopyButton,
      signinCopyNote:parts.signinCopyNote,signinField:parts.signinField,code:parts.code,codeRow:parts.codeRow,
      signinControls:parts.signinControls,goalDone,goalText,area,goalHint,summary,restart,launchGoal,launchGoalNote,launchHint,disclosure,declaration,authority,counts,details,
      controls:{context:contextButton,preview,previewCheck,launch,receipt:receiptButton}};
    detailsFor = null; render();
  }
  return Object.freeze({mountPanel,run,connectionChanged,snapshot:() => ({busy,context,plan,pending,conflicts:[...conflicts],legacy:[...legacy],note,goal,previewState}),
    setGoal(value) { if (typeof value === 'string' && value.length <= 2000 && !pending && !legacyBlocked()) { goal = value; if (mount) mount.area.value = value; changed(); } },
    resumeHandoff:() => { scheduleHandoff(); signin.schedule(); schedulePreviewPoll(); },
    // Whether this page follows a Launch it sent or followed (see followed) that has not ended in a clean done
    // receipt; the entry keeps the panel while it does. Once launched, the page is treated like one opened again.
    following:() => followed && !launched(),
    // Presentation only: how the entry's Connect looks now. It never connects or sends anything itself.
    showConnect(value) {
      const next = value && typeof value === 'object' ? {operation:value.operation === 'check' ? 'check' : 'connect',
        disabled:!!value.disabled, busy:['connect','check'].includes(value.busy) ? value.busy : null,
        hint:typeof value.hint === 'string' ? value.hint : ''} : null;
      if (JSON.stringify(next) === JSON.stringify(connectView)) return;
      connectView = next; if (!disposed) render();
    },
    showLaunchUnderway(value) { if (value && pending) unconfirmed = false; if (!!value === launchUnderway) return; launchUnderway = !!value; if (!disposed) render(); },
    // The computer's parts read says it has no team and no Launch under way: what this browser kept of a launch it
    // saw before is not about this computer now, so its saw-launch note goes. Nothing this visit confirmed is touched,
    // and the request itself stays kept (it still blocks a second launch and still leads Check launch status).
    noTeam() { if (unconfirmed && launchSeen && !followed && !managedHandoff) { seeLaunch(null); if (!disposed) render(); } },
    dispose() { disposed = true; stopHandoff(); signin.stopPoll(); stopPreviewPoll(); epoch++; signin.dispose(); mount = null; }});
}
