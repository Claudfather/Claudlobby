// The Claude sign-in step on its own: its state, its four calls (begin, status, code, cancel), its retry rules,
// its status lines and its markup. It was lifted out of guided-setup.js without a change in behaviour, so that the
// team road and any other road that signs in to Claude share one step. Since then it can also stop following an
// attempt kept under another approval (setAside), for a computer set up again from nothing.
// It is given: the four calls, a storage policy (where the one kept attempt lives, or nowhere) and a host that
// answers about the approval it belongs to (its generation and end) and the page around it.
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const keys = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
// Skips unchanged writes, so a status region is not re-announced on every two-second check.
const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
// Presentation only: the step that shows each operation's progress, and its text.
export const SIGNIN_PROGRESS = {'signin-begin':['signin','Starting Claude sign-in…'],
  'signin-retry':['signin','Starting a new Claude sign-in…'],
  'signin-status':['signin','Checking sign-in…'],'signin-code':['signin','Sending your code…'],'signin-cancel':['signin','Cancelling sign-in…']};
// The one progress line that is a note, not a wait: every other shown progress line turns a ring.
export const POLL_NOTE = 'This page checks for progress automatically.';
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
const lostNotice='The sign-in link was cleared because this connection or approval changed. Check setup approval, then check sign-in progress.';
const SIGNIN_ENDED = ['connected','needs_attention','cancelled','unknown'];
// One status check at a time, about every two seconds, well inside the conversation's call
// bound (CALL_LIMIT in owner_api_stdio.py), so the manual checks offered once it stops still fit.
// While this page is hidden (Claude's tab in front), a slower read-only check keeps the owning
// conversation inside its 45-second idle bound, so it is not drained and cancelled meanwhile.
const SIGNIN_POLL_MS=2000, SIGNIN_HIDDEN_POLL_MS=25000, SIGNIN_TERMINAL=['connected','needs_attention','cancelled'];

// Where the one kept attempt lives. A road that keeps nothing in the browser passes NO_STORAGE: it reads nothing,
// and writes nothing (a write is neither sent anywhere nor refused).
export const NO_STORAGE = Object.freeze({read:() => null, write:() => {}});

// calls(action, input): the four sign-in calls, action 'begin' | 'status' | 'code' | 'cancel'.
// store: {read():string|null, write(text)}; a write may throw, and then the sign-in is not sent.
// host: {approval, pending, busy, busyOperation, active, disposed, epoch, changed, run}, each a function.
//   approval() is the live approval {generation, expires_at, provider_signin} or null; pending() is true while
//   the page's own launch request holds the step; run(operation, {quiet}) runs a page operation.
export function createClaudeSignin({calls, store, host, now = () => Date.now()/1000, visible = () => true,
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout, copyText}) {
  let refs = null, codeHad; // the nodes this step drew (see mount), and whether its code field held text at the last paint
  // signinRetryable: the host's latest answer for this attempt said it would admit a new attempt ID.
  // signinDeadline: the host's expires_at beside the current link (whole host-clock seconds), else null.
  // signinRetryFrom: while a Try again is in flight, the ended attempt it replaces, restored if nothing was admitted.
  // While a Try again is unsettled, the other ID is kept (also in storage) until the host names the new one:
  // signinPredecessor, the ended ID it replaces (this browser follows the new one); signinSuccessor, the
  // unanswered new ID this browser set aside after the host named the ended one. At most one is set.
  let signinAttempt=null, signinState=null, signinReason=null, signinUrl=null, signinNotice='', signinBlocked=false, signinRetryable=false, signinCancelledHere=null; // signinCancelledHere: the attempt ID this page's own Cancel sign-in press named
  let signinDeadline=null, signinRetryFrom=null, signinPredecessor=null, signinSuccessor=null;
  const signinRecord = () => ({...signinAttempt,...(signinPredecessor ? {replaces:signinPredecessor} : {}),
    ...(signinSuccessor ? {replaced_by:signinSuccessor} : {})});
  // signinCopy {url,kind,n} is live only while url is the current link; n counts repeats of one result.
  let signinTimer=null, signinHalted=false, signinCopy=null, copySeq=0;
  // The host keeps a waiting state until its worker consumes an accepted code, so remember that
  // this attempt's code was taken; and whether the computer refused (not merely missed) a code or the attempt.
  let signinCodeSent=null, signinRefusal=null; // signinRefusal: null | 'code' | 'attempt'
  const codeSent = () => !!signinAttempt && signinCodeSent===signinAttempt.attempt_id;
  const signinOpen = () => host.approval()?.provider_signin===true && !!signinAttempt && !SIGNIN_TERMINAL.includes(signinState);
  // Provider-required Preview/Launch needs this context's own attempt confirmed connected; a
  // missing, unknown, cancelled or needs_attention attempt is not readiness. Not required: no guard.
  const signinReady = () => host.approval()?.provider_signin!==true
    || !!signinAttempt && signinAttempt.generation===host.approval().generation && signinState==='connected';
  const signinPolling = () => signinAttempt && ['starting','waiting'].includes(signinState) && !signinHalted;
  // The computer is finishing on its own (starting, or a code was taken): nothing to press, and its line turns a ring.
  const signinWorking = () => (signinState === 'starting' || signinSent()) && !signinHalted;
  // This page's own Sign in to Claude or Try again is on its way and the computer has not answered yet.
  // Its line says so above the buttons from the click on, so nothing moves when the answer arrives.
  const signinSending = () => signinState === null && ['signin-begin','signin-retry'].includes(host.busyOperation());
  // The same with its attempt already kept (a session renewal before it re-renders): nothing is known to check.
  const signinUnanswered = () => !!signinAttempt && signinSending();
  // The line only: a Try again says so from its click, while the ended attempt it replaces is still the one kept.
  const signinLineSending = () => signinSending() || host.busyOperation() === 'signin-retry' && signinRetryOffered();
  // Try again: only for an ended attempt of this live approval that the host said a new attempt may replace.
  const signinRetryOffered = () => host.approval()?.provider_signin===true && host.approval().expires_at>now() && !host.pending() && !signinBlocked
    && !!signinAttempt && signinRetryable && retryableEnding(signinState,signinReason);
  function clearSignin(notice) {
    signinUrl=signinDeadline=null; if (refs) refs.code.value='';
    if (notice) signinNotice=notice;
  }
  // Copy is a fallback beside the new-tab link. Where the clipboard is blocked, show the link to copy by hand.
  async function copySigninLink() {
    const url=signinUrl, seq=++copySeq; if (!url) return;
    let copied=true; try { await copyText(url); } catch { copied=false; }
    if (host.disposed() || seq!==copySeq || url!==signinUrl) return; // a later click, a new link or a cleared one wins
    const kind=copied ? 'copied' : 'manual';
    signinCopy={url,kind,n:signinCopy?.url===url && signinCopy.kind===kind ? signinCopy.n+1 : 1}; host.changed();
    if (!copied) { // iOS ignores select() on a read-only field; an explicit range selects it there too.
      const field=refs?.signinField; field?.focus?.(); field?.select?.(); field?.setSelectionRange?.(0,url.length);
    }
  }
  function stopSigninPoll() { clearTimeout(signinTimer); signinTimer=null; }
  function scheduleSigninPoll() {
    stopSigninPoll();
    if (host.disposed() || !signinPolling() || !host.approval()?.provider_signin || host.pending() || signinBlocked || !host.active()) return;
    const ticket=host.epoch(), delay=visible() ? SIGNIN_POLL_MS : SIGNIN_HIDDEN_POLL_MS;
    signinTimer=setTimeout(() => {
      signinTimer=null;
      if (host.disposed() || ticket!==host.epoch() || !signinPolling()) return;
      if (host.approval()?.expires_at<=now()) { clearSignin(lostNotice); host.changed(); return; }
      if (host.busy()) { scheduleSigninPoll(); return; } // never overlaps another call
      void host.run('signin-status',{quiet:true}); // its completion schedules the next check
    },delay);
  }
  // The attempt this browser kept, if any (a failed read blocks the step instead).
  function load() {
    try {
    const text=store.read();
    if (text) {
      const value=JSON.parse(text), other='replaces' in value ? 'replaces' : 'replaced_by' in value ? 'replaced_by' : null;
      if (!keys(value,['generation','attempt_id',...(other ? [other] : [])]) || !uuid(value.generation) || !uuid(value.attempt_id)
          || other && (!uuid(value[other]) || value[other]===value.attempt_id)) throw new Error();
      signinAttempt=Object.freeze({generation:value.generation,attempt_id:value.attempt_id});
      signinPredecessor=value.replaces ?? null; signinSuccessor=value.replaced_by ?? null;
    }
    } catch { signinBlocked=true; }
  }
  // The computer's approval read named approval `generation` and holds no request there. An attempt this browser kept
  // under another approval is one no call can follow under this one (each names its approval), and the computer holds
  // none of it: it refuses that read while its current attempt belongs to another approval (provider_signin.py _row). So
  // it is no longer followed, and this approval's own sign-in is offered. Its record stays until the next sign-in replaces it.
  function setAside(generation) {
    if (!signinAttempt || signinAttempt.generation===generation) return;
    stopSigninPoll(); signinAttempt=signinPredecessor=signinSuccessor=signinCancelledHere=null;
    signinState=signinReason=signinUrl=signinDeadline=signinRefusal=signinCodeSent=signinCopy=null;
    signinRetryable=signinHalted=false; signinNotice=''; if (refs) refs.code.value='';
  }
  // One sign-in operation of the page's run: begin, retry, status, code or cancel. Returns what run returns for it.
  async function operate(operation, ticket) {
    const context=host.approval();
    if (!context?.provider_signin || host.pending() || signinBlocked) return false;
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
      try { store.write(JSON.stringify(replaces ? {...value,replaces} : value)); }
      catch { throw Object.assign(new Error('This browser could not save the new sign-in, so nothing was sent. Choose Try again.'),
        {code:'signin_not_started'}); }
      signinPredecessor=replaces; signinSuccessor=null; signinCancelledHere=null;
      signinAttempt=Object.freeze(value); // Retain before ONE effect; no uncertain resend.
      if (action==='retry') { // nothing of the ended attempt carries over to the new one
        signinState=signinReason=signinUrl=signinDeadline=signinRefusal=signinCodeSent=signinCopy=null;
        signinRetryable=signinHalted=false; signinNotice='';
      }
    }
    if (!signinAttempt || signinAttempt.generation!==context.generation) return false;
    if (action==='cancel') signinCancelledHere=signinAttempt.attempt_id;
    let input={...signinAttempt};
    if (action==='code') {
      if (codeSent()) return false; // the computer already took this attempt's code
      const code=refs?.code.value || '';
      if (refs) refs.code.value=''; // Never retain in model/storage/debug output.
      if (!code || code.length>2305 || /[\r\n\x00]/.test(code)) return false;
      input.code=code;
    }
    const result=await calls(action==='retry' ? 'begin' : action,input); input=null; signinRetryFrom=null;
    if (host.disposed() || ticket!==host.epoch() || !host.active()) return false;
    signinReceipt(result);
    if (action==='code') { signinRefusal=null; if (signinState==='waiting') signinCodeSent=signinAttempt.attempt_id; }
    return true;
  }
  // The failure side of operate, for a sign-in operation whose run threw. restored: what restoreRetry said (below).
  // Always false: a sign-in call that failed never counts as run.
  function failed(operation, error, restored, ticket) {
    // A completed refusal is the computer's answer; a lost or unknown answer proves nothing.
    const refused=[400,401,403,409].includes(error?.status);
    // A refused code leaves the attempt waiting (the host keeps only a valid code), so its link and checks stay.
    if (refused && operation==='signin-code') { signinRefusal ??= 'code'; return false; }
    if (restored) { clearSignin(restored); return false; }
    // An unsettled Try again: the host may have moved to the other ID. One context read (never a begin)
    // lets this browser follow whichever attempt the host now names.
    if (refused && operation==='signin-status' && (signinPredecessor || signinSuccessor)) {
      signinHalted=true; queueMicrotask(() => { if (!host.disposed() && ticket===host.epoch()) void host.run('context'); });
      return false;
    }
    signinHalted=true; // no automatic retry after an error; Check sign-in progress resumes it
    signinRetryable=false; // only a later host answer can offer Try again again
    signinRefusal = refused ? 'attempt' : signinRefusal === 'attempt' ? null : signinRefusal;
    clearSignin(refused ? `This computer refused this browser’s Claude sign-in. ${NO_RETRY}`
      : 'Could not reach this computer to check sign-in. Choose Check sign-in progress to look again; it never starts a second sign-in.');
    return false;
  }
  // A refused Try again rolled the host back and an unsent one reached nothing: the ended attempt is still
  // the current one, so this browser follows it again (memory and storage). Returns the notice, or '' if neither.
  function restoreRetry(error) {
    const from=signinRetryFrom, refused=[400,401,403,409].includes(error?.status); signinRetryFrom=null;
    if (host.disposed() || !from || !refused && error?.code!=='signin_not_started') return '';
    signinAttempt=from.attempt; signinPredecessor=null; signinSuccessor=from.successor; signinState=from.state; signinReason=from.reason;
    signinRetryable=!refused && from.retryable; // after a refusal only the host's next answer offers it again
    try { store.write(JSON.stringify(signinRecord())); } catch { signinBlocked=true; }
    return refused ? 'This computer refused the new Claude sign-in. Choose Check sign-in progress to look again.'
      : error.message; // the client's own text for a begin it never sent
  }
  function signinReceipt(value) {
    const context=host.approval();
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
    store.write(JSON.stringify(signinRecord()));
    signinState=value.state; signinReason=value.reason; signinUrl=url; signinHalted=false; signinNotice='';
    signinRetryable=value.retryable===true; signinDeadline=url!==null && value.expires_at!==undefined ? value.expires_at : null;
    if (signinRefusal==='attempt' || value.state!=='waiting') signinRefusal=null;
    if (value.state!=='waiting') { signinCodeSent=null; if (refs) refs.code.value=''; }
  }
  const signinSent = () => signinState === 'waiting' && codeSent();
  const signinLinkLive = () => signinState === 'waiting' && !!signinUrl && !signinSent();
  // The host's own deadline in this viewer's local time (minutes, never rounded up); no client-side timer.
  const pasteTime = () => signinDeadline === null ? ''
    : new Date(signinDeadline*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});
  function signinStatusLine() { // [text, tone]
    const context=host.approval();
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
      // A cancel the person pressed on this page is their own choice, not a fault: an ordinary note, with Try again the one
      // filled action. The host also reports a retryable cancel for a drained or idle conversation and a service shutdown:
      // those the person did not choose, so they keep the attention style.
      const chosen = retry && signinState === 'cancelled' && !!signinAttempt && signinCancelledHere === signinAttempt.attempt_id;
      return [`${retry && signinState === 'cancelled' ? 'The sign-in stopped before a code was sent.' : signinOutcome(signinState,signinReason)} ${retry ? RETRY : why+NO_RETRY}`,chosen ? '' : 'attention'];
    }
    if (!signinAttempt) return ['Connect your Claude account.',''];
    if (signinSent()) return ['Code sent, finishing sign-in…',signinWorking() ? 'working' : ''];
    if (signinRefusal === 'code' && signinState === 'waiting') return [`The computer did not accept that code. Copy the whole code Claude shows you and submit it again; if a code was already sent, wait while sign-in finishes.${pasteTime() && ` Paste the code by ${pasteTime()}.`}`,'attention'];
    // One short line, so Open Claude sign-in takes the held button's place without moving down.
    if (signinLinkLive()) return [`Open Claude sign-in, then paste the code here${pasteTime() && ` by ${pasteTime()}`}.`,''];
    if (signinState === 'starting') return ['Starting Claude sign-in…',signinWorking() ? 'working' : ''];
    return ['A sign-in was already started in this browser. Check its progress.',''];
  }
  // What the page's render needs to know before it paints: which action leads, and which checks are offered.
  function flags() {
    const ended = SIGNIN_ENDED.includes(signinState), sent = signinSent(), waitingLink = signinLinkLive();
    codeHad = !!refs.code.value; const typed = codeHad && !sent;
    // While this page checks by itself a manual check only repeats it, so it is offered once automatic checks stop.
    // Nor beside Try again: the computer already said the ended attempt may be replaced, so there is nothing to check,
    // unless an earlier Try again's ID was set aside and a check may find the computer took it after all.
    const manualCheck = !!signinAttempt && signinState !== 'connected' && !signinPolling() && !signinUnanswered()
      && !(signinRetryOffered() && !signinSuccessor);
    return {ended, sent, waitingLink, typed, manualCheck, retryOffered:signinRetryOffered(), refusal:signinRefusal};
  }
  // Paints the step's own nodes. shown: whether the step is current or done; filled(name, quiet): the page's one filled action.
  function paint({live, busy, shown, filled, flags:{ended, sent, waitingLink, manualCheck}}) {
    const m = refs;
    const [text,tone] = signinStatusLine();
    m.signinStatus.hidden = !shown;
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
    m.signinLink.className = filled('link','pill ghost');
    submit.className = filled('code','pill ghost');
    check.className = filled('status','pill ghost');
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
  }
  // The step's markup. step(before) draws its list entry (before: nodes shown above its body) and returns it;
  // make, action and row are the page's own builders; repaint draws the page again.
  function mount({make, action, row, step, repaint}) {
    const signinStatus = make('p','setup-signin-status'); signinStatus.setAttribute('role','status');
    const signin = step([signinStatus]);
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
    code.addEventListener('input',() => { if (!!code.value !== codeHad) repaint(); });
    const codeLabel = make('label','setup-field'), submit = action('Submit code','pill ghost','signin-code'), codeRow = make('div','setup-code');
    codeLabel.append(make('span','setup-field-label','Code from Claude'),code); codeRow.append(codeLabel,submit);
    const check = action('Check sign-in progress','pill ghost','signin-status'), cancel = action('Cancel sign-in','pill ghost','signin-cancel');
    signin.body.append(row(begin,retry,signinLink,signinCopyButton,signinCopyNote),signinField,codeRow,row(check,cancel));
    return refs = {signinStatus,signinLink,signinCopyButton,signinCopyNote,signinField,code,codeRow,
      signinControls:{begin,status:check,code:submit,cancel},steps:{signin}};
  }
  return Object.freeze({load,setAside,operate,failed,restoreRetry,receipt:signinReceipt,flags,paint,mount,
    schedule:scheduleSigninPoll,stopPoll:stopSigninPoll,
    // The connection or approval changed: nothing of a live link carries over.
    lost() { stopSigninPoll(); clearSignin(['starting','waiting'].includes(signinState) ? lostNotice : undefined); },
    // The page is gone: no code, no link.
    dispose() { if (refs) refs.code.value=''; signinUrl=null; refs=null; },
    ready:signinReady, polling:signinPolling, lineSending:signinLineSending, state:() => signinState, attempt:() => signinAttempt});
}
