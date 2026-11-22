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
  ready:'Preview ready. Review it below, then choose Launch team.',
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
const PROGRESS = {context:['approve','Checking setup approval…'],'signin-begin':['signin','Starting Claude sign-in…'],
  'signin-retry':['signin','Starting a new Claude sign-in…'],
  'signin-status':['signin','Checking sign-in…'],'signin-code':['signin','Sending your code…'],'signin-cancel':['signin','Cancelling sign-in…'],
  preview:['goal','Preparing your team preview…'],'preview-check':['goal','Checking on the preview…'],
  launch:['launch','Launching your team…'],receipt:['launch','Checking launch status…']};
// The one progress line that is a note, not a wait: every other shown progress line turns a ring.
const POLL_NOTE = 'This page checks for progress automatically.';
// Skips unchanged writes, so a status region is not re-announced on every two-second check.
const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
// The control a visitor uses next: the filled action, else Sign in to Claude, the goal or Launch team, whichever
// is shown and enabled. Null when none is, so focus is left where it is.
const nextAction = m => [m.lead,m.signinControls.begin,m.area,m.controls.launch].find(control => {
  if (!control || control.disabled) return false;
  for (let node = control; node && node !== m.root; node = node.parentNode) if (node.hidden) return false;
  return true;
}) ?? null;
// One plain sentence for each finite host reason (provider_signin.py); unknown reasons fall back by state.
const sameFiles = 'Setup files on this computer changed during sign-in.', unreadable = 'Claude replied in a way this computer did not recognize.';
const SIGNIN_REASONS = {timeout:'The sign-in took too long.', provider_failed:'Claude stopped before sign-in finished.',
  effect_uncertain:'Sign-in was stopped before it finished; a sign-in Claude had already finished stays saved.',
  interrupted:'This computer restarted or stopped during sign-in.',
  unsupported_provider:'The Claude app on this computer changed or is not a supported version.',
  unsupported_output:unreadable, ambiguous_output:unreadable, output_limit:unreadable,
  credential_already_present:'This computer already has a saved Claude sign-in.',
  environment_format:'A settings file on this computer could not be read.',
  environment_changed:'A settings file on this computer changed during sign-in.',
  storage_uncertain:'The sign-in may not have been saved on this computer.',
  artifact_copy_failed:'Claude could not be prepared on this computer.',
  private_path_changed:sameFiles, file_binding_changed:sameFiles, profile_changed:sameFiles, scope_changed:sameFiles,
  boundary_or_process_failure:'Sign-in stopped unexpectedly on this computer.',
  child_quiet_unproven:'This computer could not confirm that the sign-in process stopped.'};
const signinOutcome = (state, reason) => typeof reason === 'string' && Object.hasOwn(SIGNIN_REASONS, reason) ? SIGNIN_REASONS[reason]
  : state === 'cancelled' ? 'Sign-in was cancelled; a sign-in Claude had already finished stays saved.'
  : state === 'unknown' ? 'This computer could not confirm how sign-in ended.' : 'Sign-in did not finish.';
// The host keeps one attempt per setup and refuses another attempt ID, except after an ending it
// reports as retryable: a sign-in that timed out before its code reached Claude (provider_signin.py).
const NO_RETRY = 'Trying again needs setup to be recovered on the computer itself.';
const RETRY = 'Choose Try again for a new sign-in link.';
// Exactly the host's RETRYABLE endings (provider_signin.py); the host also requires that no code was sent.
const retryableEnding = (state, reason) => state === 'needs_attention' && reason === 'timeout'
  || state === 'cancelled' && reason === 'effect_uncertain';
// The host's CODE_MARGIN + MIN_CODE_WINDOW (provider_signin.py): with less approval left it starts no new sign-in.
const SIGNIN_APPROVAL_ROOM = 50;
const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;

export function createGuidedSetup({client, storage, storageKey, now = () => Date.now()/1000, onChange = () => {},
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout, visible = () => true,
  copyText = text => globalThis.navigator.clipboard.writeText(text)}) {
  let context = null, plan = null, pending = null, conflicts = [], legacy = [], incompatible = false;
  let goal = '', planGoal = '', note = '', launchable = false, busy = false, disposed = false, epoch = 0, mount;
  let state = client.snapshot();
  // signinRetryable: the host's latest answer for this attempt said it would admit a new attempt ID.
  // signinDeadline: the host's expires_at beside the current link (whole host-clock seconds), else null.
  // signinRetryFrom: while a Try again is in flight, the ended attempt it replaces, restored if nothing was admitted.
  // While a Try again is unsettled, the other ID is kept (also in storage) until the host names the new one:
  // signinPredecessor, the ended ID it replaces (this browser follows the new one); signinSuccessor, the
  // unanswered new ID this browser set aside after the host named the ended one. At most one is set.
  let signinAttempt=null, signinState=null, signinReason=null, signinUrl=null, signinNotice='', signinBlocked=false, signinRetryable=false;
  let signinDeadline=null, signinRetryFrom=null, signinPredecessor=null, signinSuccessor=null;
  const signinRecord = () => ({...signinAttempt,...(signinPredecessor ? {replaces:signinPredecessor} : {}),
    ...(signinSuccessor ? {replaced_by:signinSuccessor} : {})});
  // signinCopy {url,kind,n} is live only while url is the current link; n counts repeats of one result.
  let signinTimer=null, signinHalted=false, signinCopy=null, copySeq=0;
  let busyOperation=null; // presentation: which explicit operation is in flight
  // Presentation from the entry that owns Connect: {operation:'connect'|'check', disabled, busy:null|operation, hint}.
  // connectFocus follows one panel click ('sent', then 'busy') so keyboard focus lands on the next action.
  // standalone: the panel is the whole view (no computer card or workspace beside it).
  // launchedFor: the binding a clean done receipt was for; a rebound request is not launched.
  let onConnect=null, connectView=null, connectFocus=null, standalone=false, launchedFor=null;
  // followed: in this page's lifetime the panel sent a Launch, sent a launch status check or followed a hand-off.
  // Never saved: a request kept in storage from an earlier page is not followed until this page acts on it.
  let followed=false;
  const launched = () => !!launchedFor && !!pending && same(launchedFor,pending);
  // The host keeps a waiting state until its worker consumes an accepted code, so remember that
  // this attempt's code was taken; and whether the computer refused (not merely missed) a code or the attempt.
  let signinCodeSent=null, signinRefusal=null; // signinRefusal: null | 'code' | 'attempt'
  const codeSent = () => !!signinAttempt && signinCodeSent===signinAttempt.attempt_id;
  // One status check at a time, about every two seconds, well inside the conversation's call
  // bound (CALL_LIMIT in owner_api_stdio.py), so the manual checks offered once it stops still fit.
  // While this page is hidden (Claude's tab in front), a slower read-only check keeps the owning
  // conversation inside its 45-second idle bound, so it is not drained and cancelled meanwhile.
  const SIGNIN_POLL_MS=2000, SIGNIN_HIDDEN_POLL_MS=25000, SIGNIN_TERMINAL=['connected','needs_attention','cancelled'];
  const signinOpen = () => context?.provider_signin===true && !!signinAttempt && !SIGNIN_TERMINAL.includes(signinState);
  // Provider-required Preview/Launch needs this context's own attempt confirmed connected; a
  // missing, unknown, cancelled or needs_attention attempt is not readiness. Not required: no guard.
  const signinReady = () => context?.provider_signin!==true
    || !!signinAttempt && signinAttempt.generation===context.generation && signinState==='connected';
  const signinPolling = () => signinAttempt && ['starting','waiting'].includes(signinState) && !signinHalted;
  // The computer is finishing on its own (starting, or a code was taken): nothing to press, and its line turns a ring.
  const signinWorking = () => (signinState === 'starting' || signinSent()) && !signinHalted;
  // This page's own Sign in to Claude or Try again is on its way and the computer has not answered yet.
  // Its line says so above the buttons from the click on, so nothing moves when the answer arrives.
  const signinSending = () => signinState === null && ['signin-begin','signin-retry'].includes(busyOperation);
  // The same with its attempt already kept (a session renewal before it re-renders): nothing is known to check.
  const signinUnanswered = () => !!signinAttempt && signinSending();
  // The line only: a Try again says so from its click, while the ended attempt it replaces is still the one kept.
  const signinLineSending = () => signinSending() || busyOperation === 'signin-retry' && signinRetryOffered();
  // Try again: only for an ended attempt of this live approval that the host said a new attempt may replace.
  const signinRetryOffered = () => context?.provider_signin===true && context.expires_at>now() && !pending && !signinBlocked
    && !!signinAttempt && signinRetryable && retryableEnding(signinState,signinReason);
  function clearSignin(notice) {
    signinUrl=signinDeadline=null; if (mount) mount.code.value='';
    if (notice) signinNotice=notice;
  }
  // Copy is a fallback beside the new-tab link. Where the clipboard is blocked, show the link to copy by hand.
  async function copySigninLink() {
    const url=signinUrl, seq=++copySeq; if (!url) return;
    let copied=true; try { await copyText(url); } catch { copied=false; }
    if (disposed || seq!==copySeq || url!==signinUrl) return; // a later click, a new link or a cleared one wins
    const kind=copied ? 'copied' : 'manual';
    signinCopy={url,kind,n:signinCopy?.url===url && signinCopy.kind===kind ? signinCopy.n+1 : 1}; changed();
    if (!copied) { // iOS ignores select() on a read-only field; an explicit range selects it there too.
      const field=mount?.signinField; field?.focus?.(); field?.select?.(); field?.setSelectionRange?.(0,url.length);
    }
  }
  function stopSigninPoll() { clearTimeout(signinTimer); signinTimer=null; }
  function scheduleSigninPoll() {
    stopSigninPoll();
    if (disposed || !signinPolling() || !context?.provider_signin || pending || signinBlocked || !active()) return;
    const ticket=epoch, delay=visible() ? SIGNIN_POLL_MS : SIGNIN_HIDDEN_POLL_MS;
    signinTimer=setTimeout(() => {
      signinTimer=null;
      if (disposed || ticket!==epoch || !signinPolling()) return;
      if (context?.expires_at<=now()) { clearSignin(lostNotice); changed(); return; }
      if (busy) { scheduleSigninPoll(); return; } // never overlaps another call
      void run('signin-status',{quiet:true}); // its completion schedules the next check
    },delay);
  }
  const lostNotice='The sign-in link was cleared because this connection or approval changed. Check setup approval, then check sign-in progress.';
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
  const releaseLaunch = () => { if (!launched()) launchHeld = null; };
  const holdLaunch = reserved => { launchHeld = reserved ? pending : null; if (reserved) launchSeen = pending; };
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
    try {
    const text=storage?.getItem(storageKey+':claude');
    if (text) {
      const value=JSON.parse(text), other='replaces' in value ? 'replaces' : 'replaced_by' in value ? 'replaced_by' : null;
      if (!keys(value,['generation','attempt_id',...(other ? [other] : [])]) || !uuid(value.generation) || !uuid(value.attempt_id)
          || other && (!uuid(value[other]) || value[other]===value.attempt_id)) throw new Error();
      signinAttempt=Object.freeze({generation:value.generation,attempt_id:value.attempt_id});
      signinPredecessor=value.replaces ?? null; signinSuccessor=value.replaced_by ?? null;
    }
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
    } catch { throw Object.assign(new Error('This browser could not save the setup identifiers, so nothing was launched. Allow site storage for this page, then try again.'), {code:'setup_storage_unavailable'}); }
  }
  function changed() { render(); onChange(); }
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
      epoch++; context = null; plan = null; launchable = false; stopSigninPoll();
      clearSignin(['starting','waiting'].includes(signinState) ? lostNotice : undefined); // only a live link can be lost
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
            || kept && (badPlan(kept,value) || !same(kept,value.request) || typeof kept.goal !== 'string' || kept.goal.length > 2000))) throw rejected();
        context = Object.freeze({...value}); plan = null; launchable = false; previewProblem = null;
        previewState = said ?? null;
        if (said !== undefined && said !== 'none') previewWatch = true;
        if (said !== 'preparing') { previewUntil = 0; previewStopped = false; }
        if (value.provider_signin === true && value.signin_attempt) signinReceipt(value.signin_attempt);
        if (value.request) {
          if (pending && !same(pending,value.request)) retain(pending,conflicts);
          pending = binding(value.request); save(pending);
        }
        holdLaunch(value.launch_reserved);
        if (kept) {
          const {goal:keptGoal,...prepared} = kept;
          plan = Object.freeze(prepared); goal = planGoal = keptGoal; launchable = true; previewTried = null;
        }
        // A ready plan whose window this browser's clock has already passed reads as expired.
        const shown = kept ? 'ready' : said === 'ready' ? 'expired' : said;
        // A null request or new grant is never proof that an original request vanished.
        note = value.launch_reserved ? `Setup was already submitted from this browser. ${localContinue(value)} Reconnecting never launches again.`
          : shown === 'ready' ? PREVIEW_NOTES.ready
          : shown === 'expired' ? PREVIEW_NOTES.expired(value)
          : shown === 'preparing' ? previewStopped ? PREVIEW_NOTES.stopped : PREVIEW_NOTES.preparing
          : shown === 'failed' ? PREVIEW_NOTES.failed(value)
          : pending ? value.request ? PREVIEW_NOTES.kept : `Setup was already submitted from this browser. ${localContinue(value)} Reconnecting never launches again.`
          : previewTried ? previewTried === 'refused' ? PREVIEW_NOTES.refused : PREVIEW_NOTES.notStarted : '';
        if (value.managed_owner && value.launch_reserved) { managedHandoff = true; followed = true; handoffUntil = now()+600; handoffReads=0; }
      } else if (operation.startsWith('signin-')) {
        if (!context?.provider_signin || pending || signinBlocked) return false;
        const action=operation.slice(7);
        if (action==='begin' || action==='retry') {
          // Try again is a new attempt ID, sent only from its explicit click; nothing retries automatically.
          if (action==='begin' ? signinAttempt : !signinRetryOffered()) return false;
          signinRetryFrom = action==='retry'
            ? {attempt:signinAttempt,state:signinState,reason:signinReason,retryable:signinRetryable,successor:signinSuccessor} : null;
          const value={generation:context.generation,attempt_id:globalThis.crypto.randomUUID()};
          // A Try again keeps the ID it replaces beside it, so any later page load can tell which one is current;
          // any ID set aside by an earlier unsettled Try again is dropped (restored if this one is undone).
          const replaces = action==='retry' ? signinAttempt.attempt_id : null;
          // Written before anything changes in memory: a failed write sends nothing and undoes the Try again.
          try { storage.setItem(storageKey+':claude',JSON.stringify(replaces ? {...value,replaces} : value)); }
          catch { throw Object.assign(new Error('This browser could not save the new sign-in, so nothing was sent. Choose Try again.'),
            {code:'signin_not_started'}); }
          signinPredecessor=replaces; signinSuccessor=null;
          signinAttempt=Object.freeze(value); // Retain before ONE effect; no uncertain resend.
          if (action==='retry') { // nothing of the ended attempt carries over to the new one
            signinState=signinReason=signinUrl=signinDeadline=signinRefusal=signinCodeSent=signinCopy=null;
            signinRetryable=signinHalted=false; signinNotice='';
          }
        }
        if (!signinAttempt || signinAttempt.generation!==context.generation) return false;
        let input={...signinAttempt};
        if (action==='code') {
          if (codeSent()) return false; // the computer already took this attempt's code
          const code=mount?.code.value || '';
          if (mount) mount.code.value=''; // Never retain in model/storage/debug output.
          if (!code || code.length>2305 || /[\r\n\x00]/.test(code)) return false;
          input.code=code;
        }
        const result=await client.setupSignin(action==='retry' ? 'begin' : action,input); input=null; signinRetryFrom=null;
        if (disposed || ticket!==epoch || !active()) return false;
        signinReceipt(result);
        if (action==='code') { signinRefusal=null; if (signinState==='waiting') signinCodeSent=signinAttempt.attempt_id; }
      } else if (operation === 'preview') {
        if (!context || context.launch_reserved || pending || legacyBlocked() || !signinReady() || !goal.trim() || context.expires_at <= now()
            || !previewOpen()) return false;
        const selected = context, authored = goal; previewWatch = true; previewTried = null;
        const value = await client.setupPreview({generation:selected.generation,goal:authored});
        if (disposed || ticket !== epoch || selected !== context || !active()) return false;
        if (badPlan(value,selected) || value.expires_at <= now()) throw new Error();
        pending = binding(value); plan = Object.freeze({...value}); planGoal = authored;
        save(pending); launchable = true;
        note = 'Preview ready. Review it below, then choose Launch team.';
      } else if (operation === 'launch') {
        if (!context || !plan || !pending || !launchable || legacyBlocked() || !signinReady() || !same(plan,pending)
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
      const restored = operation==='signin-retry' ? restoreRetry(error) : ''; // even if the connection moved on meanwhile
      if (!disposed && ticket === epoch) {
        // A failed read leaves no approval shown beside its "could not confirm" note.
        // A Preview that failed leaves the approval and the finished steps as they were; one read then asks the computer
        // what became of it. A failed read drops them only where nothing was previewed yet, or beside a launchable plan.
        if (operation === 'preview') launchable = false;
        if (reading && !keep) { context = null; launchable = false; }
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
        if (operation.startsWith('signin-')) {
          // A completed refusal is the computer's answer; a lost or unknown answer proves nothing.
          const refused=[400,401,403,409].includes(error?.status);
          // A refused code leaves the attempt waiting (the host keeps only a valid code), so its link and checks stay.
          if (refused && operation==='signin-code') { signinRefusal ??= 'code'; return false; }
          if (restored) { clearSignin(restored); return false; }
          // An unsettled Try again: the host may have moved to the other ID. One context read (never a begin)
          // lets this browser follow whichever attempt the host now names.
          if (refused && operation==='signin-status' && (signinPredecessor || signinSuccessor)) {
            signinHalted=true; queueMicrotask(() => { if (!disposed && ticket===epoch) void run('context'); });
            return false;
          }
          signinHalted=true; // no automatic retry after an error; Check sign-in progress resumes it
          signinRetryable=false; // only a later host answer can offer Try again again
          signinRefusal = refused ? 'attempt' : signinRefusal === 'attempt' ? null : signinRefusal;
          clearSignin(refused ? `This computer refused this browser’s Claude sign-in. ${NO_RETRY}`
            : 'Could not reach this computer to check sign-in. Choose Check sign-in progress to look again; it never starts a second sign-in.');
          return false;
        }
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
        note = error?.code === 'setup_storage_unavailable' ? error.message : pending
          ? 'The launch could not be confirmed. Choose Check launch status, or inspect the update on the computer if approval expired or was revoked. Do not launch again.'
          : 'Could not confirm setup approval. Make sure setup was approved on this computer, then check again.';
      }
      return false;
    } finally { busy = false; busyOperation = null; if (!disposed) { changed(); scheduleHandoff(); scheduleSigninPoll(); schedulePreviewPoll(); } }
  }
  // A refused Try again rolled the host back and an unsent one reached nothing: the ended attempt is still
  // the current one, so this browser follows it again (memory and storage). Returns the notice, or '' if neither.
  function restoreRetry(error) {
    const from=signinRetryFrom, refused=[400,401,403,409].includes(error?.status); signinRetryFrom=null;
    if (disposed || !from || !refused && error?.code!=='signin_not_started') return '';
    signinAttempt=from.attempt; signinPredecessor=null; signinSuccessor=from.successor; signinState=from.state; signinReason=from.reason;
    signinRetryable=!refused && from.retryable; // after a refusal only the host's next answer offers it again
    try { storage.setItem(storageKey+':claude',JSON.stringify(signinRecord())); } catch { signinBlocked=true; }
    return refused ? 'This computer refused the new Claude sign-in. Choose Check sign-in progress to look again.'
      : error.message; // the client's own text for a begin it never sent
  }
  function signinReceipt(value) {
    // While a Try again is unsettled the host may name either ID. Naming the ended one means the new one is not
    // admitted yet (a begin may still be committing); admitting it retires the ended one for good. Either is safe to
    // follow, and only an answer naming the new ID settles which is current.
    const other = signinPredecessor ?? signinSuccessor;
    const follows = !!signinAttempt && !!other && value?.attempt_id===other && value.generation===signinAttempt.generation;
    if (!context || !keys(value,['attempt_id','generation','state','reason',...['url','expires_at','retryable'].filter(key => key in value)])
        || !uuid(value.attempt_id) || value.generation!==context.generation
        || signinAttempt && !follows && (value.attempt_id!==signinAttempt.attempt_id || value.generation!==signinAttempt.generation)
        || !['starting','waiting','connected','needs_attention','cancelled','unknown'].includes(value.state)
        || value.reason!==null && (typeof value.reason!=='string' || !/^[a-z_]{1,64}$/.test(value.reason))
        // retryable comes only as true, and only on an attempt that ended without a saved sign-in.
        || 'retryable' in value && (value.retryable!==true || !retryableEnding(value.state,value.reason))
        // expires_at comes only beside a link, as whole positive seconds.
        || 'expires_at' in value && (!('url' in value) || !Number.isSafeInteger(value.expires_at) || value.expires_at<=0)) throw new Error();
    let url=null;
    if (value.url!==undefined) {
      const selected=new URL(value.url);
      if (value.state!=='waiting' || selected.origin!=='https://claude.com' || selected.pathname!=='/cai/oauth/authorize'
          || selected.hash || selected.searchParams.get('redirect_uri')!=='https://platform.claude.com/oauth/code/callback'
          || selected.searchParams.get('scope')!=='user:inference' || value.url.length>8192) throw new Error();
      url=value.url;
    }
    if (follows && signinPredecessor) { signinSuccessor=signinAttempt.attempt_id; signinPredecessor=null; } // set the new ID aside
    else if (follows) signinSuccessor=null; // the host named the new ID: settled
    else signinPredecessor=null; // an answer for the ID held: settled if it was the new one; an ended one keeps its successor
    signinAttempt=Object.freeze({generation:value.generation,attempt_id:value.attempt_id});
    storage.setItem(storageKey+':claude',JSON.stringify(signinRecord()));
    signinState=value.state; signinReason=value.reason; signinUrl=url; signinHalted=false; signinNotice='';
    signinRetryable=value.retryable===true; signinDeadline=url!==null && value.expires_at!==undefined ? value.expires_at : null;
    if (signinRefusal==='attempt' || value.state!=='waiting') signinRefusal=null;
    if (value.state!=='waiting') { signinCodeSent=null; if (mount) mount.code.value=''; }
  }
  function receipt(value) {
    if (!value || ![2,3].includes(value.version) || context && value.version !== context.version || !pending || !same(value,pending) || !phases.includes(value.state)
        || !continuation(value.continuation) || typeof value.launch_reserved !== 'boolean' || typeof value.config_committed !== 'boolean'
        || !value.health_errors || typeof value.health_errors !== 'object' || Array.isArray(value.health_errors)
        || value.managed_owner !== undefined && value.managed_owner !== true
        || 'plan_id' in value || 'release_id' in value || 'release_seal' in value) throw new Error();
    if (value.launch_reserved || value.state === 'unknown') launchable = false;
    holdLaunch(value.launch_reserved);
    const done = value.state === 'done' && value.config_committed && Object.keys(value.health_errors).length === 0;
    launchedFor = done ? pending : null;
    note = done ? standalone ? 'Team launched. Your team is running on this computer.'
      : 'Team launched. Once its bots show as running, message the lead with one concrete request; the setup goal alone does not start a task.'
      : value.state === 'unknown' || Object.keys(value.health_errors).length
      ? 'The launch needs attention on the computer. Do not launch again; inspect the update there using the identifiers under Details.'
      : value.state === 'quiesce' && !value.launch_reserved
      ? `Prepared but not launched. ${localContinue(value)} This page never resends it after reconnecting.`
      : value.state === 'quiesce' && value.launch_reserved
      ? value.managed_owner ? 'Launch received. Your team’s connection is starting and will reconnect here. Do not launch again.'
        : 'Launch received. Let the setup guide on the computer finish its handoff, then use its status and reconnect commands. Do not launch again.'
      : 'Still in progress. Checking status never repeats or resumes setup.';
  }
  const SIGNIN_ENDED = ['connected','needs_attention','cancelled','unknown'];
  const signinSent = () => signinState === 'waiting' && codeSent();
  const signinLinkLive = () => signinState === 'waiting' && !!signinUrl && !signinSent();
  // The host's own deadline in this viewer's local time (minutes, never rounded up); no client-side timer.
  const pasteTime = () => signinDeadline === null ? ''
    : new Date(signinDeadline*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
  function signinStatusLine() { // [text, tone]
    if (signinBlocked) return ['This browser cannot keep track of a Claude sign-in. Allow site storage for this page, then reopen it.','attention'];
    if (signinState === 'connected') return ['Claude connected','done'];
    // The same words the computer's 'starting' answer shows, so the line neither changes nor is announced twice.
    if (signinLineSending()) return ['Starting Claude sign-in…','working']; // over any earlier attempt's note
    if (signinNotice) return [signinNotice,'attention'];
    if (SIGNIN_ENDED.includes(signinState)) {
      const retry = signinRetryOffered(); // a replaceable cancel stopped before any code reached Claude
      // Too little approval left for the host to start another sign-in; a new approval keeps this retained attempt.
      const ending = !retry && retryableEnding(signinState,signinReason) && !!context && context.expires_at-now() < SIGNIN_APPROVAL_ROOM;
      const why = !ending ? '' : context.expires_at <= now() ? 'Setup approval expired. ' : 'Setup approval ends too soon for a new sign-in. ';
      return [`${retry && signinState === 'cancelled' ? 'The sign-in stopped before a code was sent.' : signinOutcome(signinState,signinReason)} ${retry ? RETRY : why+NO_RETRY}`,'attention'];
    }
    if (!signinAttempt) return ['Connect your Claude account.',''];
    if (signinSent()) return ['Code sent, finishing sign-in…',signinWorking() ? 'working' : ''];
    if (signinRefusal === 'code' && signinState === 'waiting') return [`The computer did not accept that code. Copy the whole code Claude shows you and submit it again; if a code was already sent, wait while sign-in finishes.${pasteTime() && ` Paste the code by ${pasteTime()}.`}`,'attention'];
    // One short line, so Open Claude sign-in takes the held button's place without moving down.
    if (signinLinkLive()) return [`Open Claude sign-in, then paste the code here${pasteTime() && ` by ${pasteTime()}`}.`,''];
    if (signinState === 'starting') return ['Starting Claude sign-in…',signinWorking() ? 'working' : ''];
    return ['A sign-in was already started in this browser. Check its progress.',''];
  }
  let detailsFor = null; // the plan, pending and identifier counts the Details JSON was last built from
  const LEGACY = 'This browser holds identifiers from an older setup, so it cannot launch a new team here. Inspect them on the computer; they are listed under Details.';
  function render() {
    // Check preview needs a live approval: once this page's clock says it ended, a note offering it says so instead.
    if (previewWatch && context && context.expires_at <= now() && /Check preview/.test(note)) note = PREVIEW_NOTES.readExpired;
    if (!mount) return;
    const m = mount, live = active(), time = now();
    // A launch the computer holds for this request was approved, even once the approval context is gone.
    const holding = launchHolding(), underway = standalone && launchUnderway;
    const approved = holding || underway && !!pending || !!context && context.expires_at > time;
    // It keeps a sign-in this page saw connected for the same approval.
    const signinKnown = context?.provider_signin === true || holding && !context && signinState === 'connected'
      && signinAttempt?.generation === pending.generation;
    // Sign-in is a step when this approval asks for it; until approval is known it is shown as upcoming.
    const signinShown = pending ? signinKnown && signinState === 'connected' : signinKnown || !context;
    const signinDone = signinKnown && signinReady();
    const states = {approve:approved ? 'done' : 'current',
      signin:!signinShown ? null : signinDone ? 'done' : approved ? 'current' : 'upcoming',
      goal:pending ? 'done' : approved && (!signinKnown || signinDone) ? 'current' : 'upcoming',
      launch:pending ? 'current' : 'upcoming'};
    // With a Connect step, nothing after it is current until this computer is connected.
    const settling = holding && handoffChecking && ['checking','renewing'].includes(state.state);
    const connecting = !!m.steps.connect && !live && !settling;
    if (m.steps.connect) states.connect = live || settling ? 'done' : 'current';
    if (connecting) for (const name of ['approve','launch']) if (states[name] === 'current') states[name] = 'upcoming';
    // The whole view ends on finished steps, even after the handoff's status check moved the connection
    // on and cleared the approval context: nothing is left to approve or check again.
    const finished = standalone && launched();
    if (finished) for (const name of Object.keys(states)) if (states[name] !== null) states[name] = 'done';
    const progress = {approve:'',signin:'',goal:'',launch:''};
    progress.connect = connecting && connectView?.busy ? connectView.busy === 'check' ? 'Checking connection…' : 'Connecting…' : '';
    // A starting sign-in says so in its own line above the buttons instead, wherever that line is shown.
    const lineSaysIt = signinLineSending() && ['current','done'].includes(states.signin);
    if (busy && busyOperation && !lineSaysIt) { const [step,text] = PROGRESS[busyOperation]; progress[step] = text; }
    if (!progress.signin && signinKnown && !pending && signinPolling()) progress.signin = POLL_NOTE;
    if (!progress.launch && managedHandoff) progress.launch = 'Checking automatically while your team connects…';
    // While the computer prepares the preview the goal step keeps Preview team's own line, so nothing changes between checks.
    if (!progress.goal && !pending && previewPolling()) progress.goal = PROGRESS.preview[1];
    for (const [name,step] of Object.entries(m.steps)) {
      const state = states[name]; step.root.hidden = state === null;
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
    m.lede.hidden = standalone && !!pending && !!launchSeen && same(launchSeen,pending) || launched() || underway;
    // Once a preview exists for this approval the note says what is true; a new check cannot continue an ended approval.
    setText(m.approveHint,context && !approved && !previewWatch ? 'Setup approval expired. Check it again to continue.' : '');
    // Hidden until approved; never while launchable (re-checking would retire a fresh preview's launch).
    m.recheck.hidden = states.approve !== 'done' || launchable || finished;
    const ended = SIGNIN_ENDED.includes(signinState), sent = signinSent(), waitingLink = signinLinkLive();
    m.codeHad = !!m.code.value; const typed = m.codeHad && !sent;
    const launchShown = !!plan && launchable;
    // Check preview: once the computer said a preview exists that this page cannot move on, and it is not checking by itself.
    const checkShown = approved && !pending && previewWatch && !previewOpen() && !previewPolling() && !previewChecking
      && !['refused','expired'].includes(previewProblem); // a refusal or an ended approval cannot change by looking again
    // While this page checks by itself a manual check only repeats it, so it is offered once automatic checks stop.
    // Nor beside Try again: the computer already said the ended attempt may be replaced, so there is nothing to check,
    // unless an earlier Try again's ID was set aside and a check may find the computer took it after all.
    const manualCheck = !!signinAttempt && signinState !== 'connected' && !signinPolling() && !signinUnanswered()
      && !(signinRetryOffered() && !signinSuccessor);
    // One filled action: the step that can move leads. Preview team and Launch team are filled whenever shown;
    // so is Sign in to Claude, except while it is held as sign-in starts.
    const lead = pending ? launchShown ? null : 'receipt' // a retained launch's status check leads
      : !approved ? 'context'
      : checkShown ? 'check'
      : states.signin !== 'current' ? null
      : typed ? 'code' : waitingLink ? 'link' : signinRetryOffered() ? 'retry'
      : manualCheck && !ended && signinRefusal !== 'attempt' ? 'status' : null;
    const filled = (name, quiet) => (connecting ? 'connect' : lead) === name ? 'pill setup-primary' : quiet;
    const [text,tone] = signinStatusLine();
    m.signinStatus.hidden = !['current','done'].includes(states.signin);
    setText(m.signinStatus,text);
    if (tone) m.signinStatus.dataset.tone = tone; else delete m.signinStatus.dataset.tone;
    const {begin,status:check,code:submit,cancel} = m.signinControls;
    // While sign-in starts, Sign in to Claude stays in place, held and quiet, so a second click lands on it, never on Cancel sign-in.
    const beginHeld = signinState === 'starting' && signinPolling() || signinUnanswered();
    begin.hidden = signinBlocked || !!signinAttempt && !beginHeld;
    begin.className = beginHeld ? 'pill ghost' : 'pill setup-primary';
    check.hidden = !manualCheck;
    cancel.hidden = !signinAttempt || ended;
    m.codeRow.hidden = signinState !== 'waiting';
    controls.context.className = filled('context','pill ghost');
    m.signinLink.className = filled('link','pill ghost');
    submit.className = filled('code','pill ghost');
    check.className = filled('status','pill ghost');
    controls.receipt.className = filled('receipt','pill ghost');
    controls.previewCheck.className = filled('check','pill ghost'); controls.previewCheck.hidden = !checkShown;
    controls.preview.hidden = !previewOpen();
    m.signinLink.hidden = !waitingLink; if (signinUrl) m.signinLink.setAttribute('href',signinUrl); else m.signinLink.removeAttribute?.('href');
    m.signinCopyButton.hidden = !waitingLink;
    const copy = waitingLink && signinCopy?.url === signinUrl ? signinCopy : null;
    // A repeated result toggles a trailing no-break space so the status region announces it again.
    setText(m.signinCopyNote,!copy ? '' : (copy.kind === 'copied' ? 'Link copied. Paste it into a new tab.'
      : 'Copying is blocked here. Copy the link from the box below.') + (copy.n % 2 ? '' : ' '));
    m.signinField.hidden = copy?.kind !== 'manual'; m.signinField.value = m.signinField.hidden ? '' : signinUrl;
    m.code.disabled = !live || signinState !== 'waiting' || sent; // not held while busy, so typing keeps focus during a check
    for (const [operation,button] of Object.entries(m.signinControls)) button.disabled=!live || busy || signinBlocked
      || operation==='begin' && !!signinAttempt || operation!=='begin' && !signinAttempt
      || operation==='code' && signinState!=='waiting'
      || operation==='code' && sent;
    // Try again shows, filled, only where the host said a new attempt may replace the ended one.
    const retry = m.steps.signin.retry, retryShown = signinRetryOffered();
    retry.hidden = !retryShown; retry.className = filled('retry','pill ghost');
    retry.disabled = !live || busy || !retryShown;
    m.lead = {context:controls.context,receipt:controls.receipt,check:controls.previewCheck,code:submit,link:m.signinLink,retry,status:check}[lead] ?? null;
    setText(m.goalDone,pending ? planGoal ? `Goal: ${planGoal}` : 'Team preview prepared' : '');
    setText(m.goalText,context ? `Your team works in ${context.template === 'local-project' ? context.project_directory : context.repository}.` : '');
    setText(m.goalHint,goal.trim() ? '' : 'Write a goal to preview your team.');
    m.area.disabled = !live || busy || !!pending || legacyBlocked() || !previewOpen();
    controls.launch.hidden = !launchShown;
    setText(m.summary,launchShown ? `Your team is ready. Launching changes ${count(plan.changes.length,'generated file','generated files')} and starts or restarts ${count(plan.effects?.restart_bots?.length || 0,'bot','bots')}.`
      : 'Check launch status to see where setup stands. This page never sends a launch twice.');
    setText(m.launchHint,plan && plan.expires_at <= time ? 'This preview expired. Inspect the update on the computer.' : '');
    setText(m.status,[note, legacyBlocked() ? LEGACY : !live && !m.connect ? 'Connect this computer to continue setup.' : ''].filter(Boolean).join(' '));
    setText(m.declaration,context ? context.template === 'local-project'
      ? `Your team ${context.fleet} works in ${context.project_directory}. Once the team has finished starting and you’ve allowed messages to it, tell the lead what to do. The lead writes down the task and coordinates the work and an independent review.`
      : `Your team ${context.fleet} works on ${context.repository}, with a lead, an engineer and a reviewer. The team needs your permission before it merges a pull request.` : '');
    setText(m.authority,context ? `This approval covers ${context.template === 'local-project' ? `reading and changing files in ${context.project_directory}` : `work on ${context.repository}`}, for app version ${context.source_revision} only. Launching starts the team and the app’s background services on this computer and may use the provider accounts you set up. Some folders, tools or accounts may still need your approval on this computer. Merging, deploying, extra spending and destructive changes each need your separate permission.` : '');
    // conflicts and legacy only grow (retain), so their lengths track every change.
    const detailsKey = [plan, pending, conflicts.length, legacy.length];
    if (!detailsFor || detailsKey.some((value,index) => value !== detailsFor[index])) {
      detailsFor = detailsKey;
      setText(m.details,plan || pending || conflicts.length || legacy.length ? JSON.stringify({pending,conflicting_identifiers:conflicts,
        historical_identifiers:legacy,...(plan ? {changes:plan.changes,effects:plan.effects,expires_at:plan.expires_at} : {})},null,2) : '');
    }
    m.disclosure.hidden = !context && !m.details.textContent;
    for (const [operation,button] of Object.entries(controls)) button.disabled = !live || busy
      || operation === 'preview' && (!context || !!pending || legacyBlocked() || !signinReady() || !goal.trim() || context.expires_at <= time || !previewOpen())
      || operation === 'previewCheck' && !checkShown
      || operation === 'launch' && (!context || !plan || !pending || !launchable || legacyBlocked() || !signinReady() || goal !== planGoal || plan.expires_at <= time || context.expires_at <= time)
      || operation === 'receipt' && !pending;
    m.recheck.disabled = controls.context.disabled;
    if (m.connect) {
      const {button,hint} = m.connect;
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
        if (client.snapshot().state === 'setup_only') await run('context');
        if (!disposed && mount === m && focusFree()) nextAction(m)?.focus?.();
      });
    }
    else if (connectFocus === 'busy' && !connectView?.busy) {
      connectFocus = null; if (!m.connect.button.disabled && focusFree()) m.connect.button.focus?.();
    }
  }
  // Steps in order, each with a mark workspace.css draws from its state (✓ once done); only the current step shows its body. Long approval text and identifiers sit under Details.
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
      connectRow.body.append(make('p','setup-step-text','Connect so you can finish setup.'),row(button),hint);
      connectStep = {button,hint};
    }
    const approve = step('approve','Approve setup'), approveDone = make('span');
    const recheck = action('Check setup approval again','pill ghost','context'), approveHint = make('p','setup-hint');
    const contextButton = action('Check setup approval','pill setup-primary','context');
    approve.done.append(approveDone);
    approve.body.append(make('p','setup-step-text','Check that this computer approved setup for this workspace.'),row(contextButton),approveHint);
    const signinStatus = make('p','setup-signin-status'); signinStatus.setAttribute('role','status');
    const signin = step('signin','Sign in to Claude',{done:false,before:[signinStatus]});
    const begin = action('Sign in to Claude','pill setup-primary','signin-begin');
    const retry = signin.retry = action('Try again','pill ghost','signin-retry'); // held on its step for render
    const signinLink = make('a','pill setup-primary','Open Claude sign-in'); signinLink.setAttribute('target','_blank'); signinLink.setAttribute('rel','noreferrer noopener');
    const signinCopyButton = make('button','pill ghost','Copy link'); signinCopyButton.type = 'button';
    signinCopyButton.addEventListener('click',() => { void copySigninLink(); });
    const signinCopyNote = make('span','signin-copy-note'); signinCopyNote.setAttribute('role','status');
    const signinField = make('input','signin-link-field'); signinField.type = 'text'; signinField.readOnly = true; signinField.setAttribute('aria-label','Claude sign-in link');
    const code = make('input','setup-input'); code.type = 'password'; code.maxLength = 2305; code.dataset.field = 'signin-code';
    for (const [name,value] of [['autocomplete','off'],['autocapitalize','off'],['spellcheck','false']]) code.setAttribute(name,value);
    // Emphasis only (the code is read solely by Submit code): repaint when the field turns empty or non-empty.
    code.addEventListener('input',() => { if (!!code.value !== mount.codeHad) render(); });
    const codeLabel = make('label','setup-field'), submit = action('Submit code','pill ghost','signin-code'), codeRow = make('div','setup-code');
    codeLabel.append(make('span','setup-field-label','Code from Claude'),code); codeRow.append(codeLabel,submit);
    const check = action('Check sign-in progress','pill ghost','signin-status'), cancel = action('Cancel sign-in','pill ghost','signin-cancel');
    signin.body.append(row(begin,retry,signinLink,signinCopyButton,signinCopyNote),signinField,codeRow,row(check,cancel));
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
    const launch = action('Launch team','pill setup-primary','launch'), receiptButton = action('Check launch status','pill ghost','receipt');
    launchStep.body.append(summary,row(launch,receiptButton),launchHint);
    const launchDone = standalone ? make('span') : null; launchStep.done?.append(launchDone);
    const disclosure = make('details','setup-details'), declaration = make('p'), authority = make('p'), details = make('pre');
    disclosure.append(make('summary','','Details'),declaration,authority,row(recheck),details); element.append(disclosure);
    mount = {root:element,document,lede,status,steps,connect:connectStep,launchDone,approveDone,recheck,approveHint,signinStatus,signinLink,signinCopyButton,signinCopyNote,signinField,code,codeRow,
      signinControls:{begin,status:check,code:submit,cancel},goalDone,goalText,area,goalHint,summary,launchHint,disclosure,declaration,authority,details,
      controls:{context:contextButton,preview,previewCheck,launch,receipt:receiptButton}};
    detailsFor = null; render();
  }
  return Object.freeze({mountPanel,run,connectionChanged,snapshot:() => ({busy,context,plan,pending,conflicts:[...conflicts],legacy:[...legacy],note,goal,previewState}),
    setGoal(value) { if (typeof value === 'string' && value.length <= 2000 && !pending && !legacyBlocked()) { goal = value; if (mount) mount.area.value = value; changed(); } },
    resumeHandoff:() => { scheduleHandoff(); scheduleSigninPoll(); schedulePreviewPoll(); },
    // Whether this page sent a Launch or follows one (see followed); the entry keeps the panel while it does.
    following:() => followed,
    // Presentation only: how the entry's Connect looks now. It never connects or sends anything itself.
    showConnect(value) {
      const next = value && typeof value === 'object' ? {operation:value.operation === 'check' ? 'check' : 'connect',
        disabled:!!value.disabled, busy:['connect','check'].includes(value.busy) ? value.busy : null,
        hint:typeof value.hint === 'string' ? value.hint : ''} : null;
      if (JSON.stringify(next) === JSON.stringify(connectView)) return;
      connectView = next; if (!disposed) render();
    },
    showLaunchUnderway(value) { if (!!value === launchUnderway) return; launchUnderway = !!value; if (!disposed) render(); },
    dispose() { disposed = true; stopHandoff(); stopSigninPoll(); stopPreviewPoll(); epoch++; if (mount) mount.code.value=''; signinUrl=null; mount = null; }});
}
