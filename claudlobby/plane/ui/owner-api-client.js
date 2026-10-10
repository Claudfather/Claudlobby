const readHandles = new WeakSet();
export const isOwnerReadHandle = handle => readHandles.has(handle);

// Selected only by the trusted owner browser server, inside its read gate.
// Explicit renewal avoids assuming when a restored page's session began.
export function createOwnerTransport({ fetch = globalThis.fetch.bind(globalThis),
  EventSource = globalThis.EventSource, location = globalThis.location,
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout,
  monotonicNow = () => globalThis.performance.now(),
  onAccessEnded = () => location.replace('/owner') } = {}) {
  let generation = 0, mode = 'checking', busy = false, disposed = false;
  let actionChecking = null, recoveryUsed = false, readRecoveryUsed = false, streamRecoveryUsed = false, readRecoveryTimer = null;
  let mount = null, statusNote = null, readHost = null;
  const reads = new Set(), streams = new Set(), stateListeners = new Set();
  const labels = {
    checking: 'Checking owner session…', ready: 'Owner session · renew before it expires.',
    renewing: 'Renewing owner session…', logout: 'Signing out…',
    unavailable: 'Connection unavailable. Session state is unknown; check again.',
    denied: 'Owner access ended. Returning to sign-in…',
    signed_out: 'Signed out. Local owner pairing remains.',
  };
  function readerSnapshot() {
    return Object.freeze({ state: mode, epoch: generation, remediation: statusNote,
      read_profile: mode === 'ready' ? Object.freeze({ version: 1,
        profile: 'direct-owner-read-v1', host_uid: readHost }) : null });
  }
  function render(note) {
    if (note) statusNote = note;
    for (const listener of stateListeners) listener(readerSnapshot());
    if (!mount) return;
    mount.status.textContent = statusNote || labels[mode];
    mount.renew.disabled = busy || mode !== 'ready';
    mount.logout.disabled = busy || !['ready', 'unavailable'].includes(mode);
    mount.check.disabled = busy;
  }
  function pause(next) {
    clearTimeout(readRecoveryTimer); readRecoveryTimer = null;
    generation++;
    mode = next;
    statusNote = null;
    for (const controller of reads) controller.abort();
    for (const stream of streams) stream.pause();
    mount?.onPause();
    render();
  }
  function admitReady(data) {
    const profile = data?.read_profile;
    if (!profile || Object.keys(profile).sort().join() !== 'host_uid,profile,version' ||
        profile.version !== 1 || profile.profile !== 'direct-owner-read-v1' ||
        typeof profile.host_uid !== 'string' || profile.host_uid.length !== 37 ||
        !/^host_[0-9a-f]{32}$/.test(profile.host_uid)) {
      pause('unavailable');
      render('Plane browser/server compatibility could not be verified. Update both to the same release and reload.');
      return false;
    }
    if (readHost !== null && readHost !== profile.host_uid) {
      pause('unavailable');
      render('The connected host changed. Reopen Plane on the intended host before continuing.');
      return false;
    }
    readHost = profile.host_uid; // Pinned for this transport lifetime, including renewal/recovery.
    return true;
  }
  function resume(note) {
    mode = 'ready';
    for (const stream of streams) stream.open();
    render(note);
    mount?.onResume(); // First paint does not depend on a proxy flushing SSE.
  }
  function leave(next) {
    pause(next);
    onAccessEnded(next); // Direct-host default redirects; read handles report only their source.
  }
  async function request(url, action = false, body = {}, timeout = 8000) {
    const controller = new AbortController();
    reads.add(controller);
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, { method: action ? 'POST' : 'GET',
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
        ...(action ? { headers: { 'Content-Type': 'application/json', 'X-Claudlobby-Owner': '1' }, body: JSON.stringify(body) } : {}),
      });
      // Keep the timeout through body consumption, not only response headers.
      let data;
      try { data = await response.json(); }
      catch (error) { if (response.status !== 403 && !(error instanceof SyntaxError)) throw error; }
      if (controller.signal.aborted) throw new Error();
      return { status: response.status, data };
    } finally {
      clearTimeout(timer);
      reads.delete(controller);
    }
  }
  async function checkSession(recover = false, note) {
    if (disposed || busy) return;
    if (recover && recoveryUsed) { pause('unavailable'); return; }
    if (recover) recoveryUsed = true;
    busy = true;
    pause('checking');
    const gen = generation;
    try {
      const result = await request('/api/owner/status');
      if (disposed || gen !== generation) return;
      if (result.status === 200 && result.data?.state === 'ready') {
        if (admitReady(result.data)) resume(note);
      }
      else if (result.status === 403 || (result.status === 200 &&
        ['needs_pairing', 'sign_in_required'].includes(result.data?.state))) leave('denied');
      else pause('unavailable');
    } catch {
      if (!disposed && gen === generation) pause('unavailable');
    } finally {
      busy = false;
      render();
    }
  }
  async function mutate(action) {
    if (disposed || busy || (action === 'renew' && mode !== 'ready')) return;
    busy = true;
    pause(action === 'renew' ? 'renewing' : 'logout');
    const gen = generation;
    let refusal = false;
    try {
      const result = await request(`/api/owner/${action}`, true);
      if (disposed || gen !== generation) return;
      if (result.status === 200 && result.data?.state === (action === 'renew' ? 'ready' : 'signed_out')) {
        if (action === 'logout') leave('signed_out');
        else if (admitReady(result.data)) { recoveryUsed = false; readRecoveryUsed = false; streamRecoveryUsed = false; resume('Session renewed. Your browser session stays signed in.'); }
      } else if (result.status === 403) {
        refusal = true;
      } else pause('unavailable'); // A lost logout reply is not success.
    } catch {
      if (!disposed && gen === generation) pause('unavailable');
    } finally {
      busy = false;
      render();
    }
    // Another tab may have rotated the shared cookie during this operation.
    // Check the current cookie once; never replay a mutation or auto-login.
    if (refusal && !disposed) {
      recoveryUsed = false;
      await checkSession(true, action === 'logout' ? 'Sign-out did not complete. You are still signed in; try again.' : undefined);
    }
  }
  function unavailableRead() {
    pause('unavailable');
    // One read-only recovery probe per outage. A successful status response
    // alone does not rearm it: require an admitted stream message or explicit action.
    if (readRecoveryUsed || disposed) return;
    readRecoveryUsed = true;
    const gen = generation;
    readRecoveryTimer = setTimeout(() => {
      readRecoveryTimer = null;
      if (!disposed && gen === generation && mode === 'unavailable' && !busy) void checkSession();
    }, 1000);
  }
  async function jget(url) {
    if (disposed || mode !== 'ready') return null;
    const gen = generation;
    try {
      const result = await request(url);
      if (disposed || gen !== generation || mode !== 'ready') return null;
      if (result.status === 403) {
        if (recoveryUsed) leave('denied');
        else await checkSession(true);
        return null;
      }
      if (result.status === 503) { unavailableRead(); return null; }
      if (result.status !== 200) return null; // Only the affected panel degrades.
      recoveryUsed = false;
      return result.data ?? null;
    } catch {
      if (!disposed && gen === generation && mode === 'ready') unavailableRead();
      return null;
    }
  }
  const unknown = () => new Error('Action outcome unknown. Check the original receipt.');
  async function ownerAction(path, body, timeout = 8000) {
    // A request may have reached delivery even when its reply or session is lost.
    // Throw on uncertainty so ActionState keeps the original saved request ID.
    if (disposed || mode !== 'ready') throw unknown();
    const gen = generation;
    let result;
    try {
      result = await request(`/api/owner/actions/${path}`, true, body, timeout);
    } catch {
      throw unknown();
    }
    if (disposed || gen !== generation || mode !== 'ready') throw unknown();
    if (result.status === 403) {
      // A missing/revoked message grant is independent of read access. Probe
      // the current cookie without pause/resume, which would reload context.
      // Context failures are already fenced by the controller room epoch.
      // A mutation/receipt refusal may invalidate only its original scope.
      if (path !== 'context') mount?.onActionPause(body.scope, body.kind || 'message', body.target?.recipient);
      await checkActionSession(gen);
      if (disposed || gen !== generation || mode !== 'ready') throw unknown();
      if (path === 'send' && result.data?.effect === 'not_started') {
        const refusal = new Error('This submission was refused before delivery; your draft is kept.');
        refusal.effect = 'not_started';
        throw refusal;
      }
      throw unknown();
    }
    if (path === 'send' && result.status === 503 && result.data?.effect === 'not_started') {
      const refusal = new Error('This submission was refused before delivery; your draft is kept.');
      refusal.effect = 'not_started';
      throw refusal;
    }
    if (result.status !== 200) throw unknown();
    recoveryUsed = false;
    return result.data;
  }
  async function checkActionSession(gen) {
    if (actionChecking?.gen === gen) return actionChecking.promise;
    const promise = (async () => {
      try {
        const result = await request('/api/owner/status');
        if (disposed || gen !== generation || mode !== 'ready') return;
        if (result.status === 200 && result.data?.state === 'ready') { admitReady(result.data); return; }
        if (result.status === 403 || (result.status === 200 &&
          ['needs_pairing', 'sign_in_required'].includes(result.data?.state))) leave('denied');
        else pause('unavailable');
      } catch {
        if (!disposed && gen === generation && mode === 'ready') pause('unavailable');
      }
    })();
    actionChecking = { gen, promise };
    try { await promise; }
    finally { if (actionChecking?.promise === promise) actionChecking = null; }
  }
  async function interactionContext(room) {
    if (typeof room !== 'string' || !room || room === 'all') return null;
    try {
      const context = await ownerAction('context', { room });
      // This transport exposes ordinary messages only. The shared controller
      // validates the complete versioned context before enabling its composer.
      if (context?.version !== 1 || context.simulation !== false || context.room !== room ||
          context.scope?.fleet !== room || !Array.isArray(context.actions) ||
          context.actions.length !== 1 || context.actions[0] !== 'message') return null;
      return context;
    } catch { return null; }
  }
  async function taskContext(room, kind) {
    if (typeof room !== 'string' || !room || room === 'all') return null;
    try {
      const context = await ownerAction('context', { room, kind });
      if (context?.version !== 2 || context.simulation !== false || context.room !== room || context.scope?.fleet !== room
          || !Array.isArray(context.actions) || context.actions.length !== 1 || context.actions[0] !== kind
          || !Array.isArray(context.recipients) || context.recipients.length !== 1 || context.recipients[0].lead !== true
          || !/^r-[0-9a-f]{64}$/.test(context.release_id)) return null;
      return context; // Shared controller validates the complete capability.
    } catch { return null; }
  }
  const nudgeContext = room => taskContext(room, 'nudge');
  const feedbackContext = room => taskContext(room, 'feedback');
  function actionMetadata(value, preparing = false) {
    const scope = { workspace: value?.scope?.workspace, host: value?.scope?.host,
      fleet: value?.scope?.fleet, viewer: value?.scope?.viewer };
    if (value?.version === 2 && ['nudge', 'feedback'].includes(value.kind)) {
      const target = value.target;
      if (!/^actor_[0-9a-f]{32}$/.test(target?.recipient) || !/^wi_[0-9a-f]{32}$/.test(target?.task_id)
          || !Object.hasOwn(target, 'assignment_id') || !(target.assignment_id === null || /^asg_[0-9a-f]{32}$/.test(target.assignment_id))
          || !/^r-[0-9a-f]{64}$/.test(target.release_id)
          || (!preparing && !/^[0-9a-f]{64}$/.test(value.semantic_sha256))) throw new Error('Unsupported task action.');
      return { version: 2, request_id: value.request_id, kind: value.kind, scope,
        target: { recipient: target.recipient, task_id: target.task_id, assignment_id: target.assignment_id, release_id: target.release_id },
        submitted_at: value.submitted_at, ...(!preparing ? { semantic_sha256: value.semantic_sha256 } : {}) };
    }
    if (preparing || value?.kind !== 'message' || value.target?.task_id !== null || value.version !== undefined)
      throw new Error('Unsupported action.');
    return { request_id: value.request_id, kind: value.kind, scope,
      target: { recipient: value.target.recipient, task_id: null }, submitted_at: value.submitted_at };
  }
  async function prepareAction(value) {
    return ownerAction('prepare', { ...actionMetadata(value, true), body: value.body });
  }
  async function sendAction(value) {
    // One bounded attempt. Timeout/abort never means native work stopped.
    return ownerAction('send', { ...actionMetadata(value), body: value.body }, 45000);
  }
  async function actionReceipt(value) {
    return ownerAction('receipt', actionMetadata(value));
  }
  function createEventSource(url) {
    const listeners = new Map();
    let native = null, closed = false, openedAt = null;
    const stream = {
      onmessage: null, onerror: null, onopen: null,
      removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
      addEventListener(name, callback) {
        if (!listeners.has(name)) listeners.set(name, new Set());
        listeners.get(name).add(callback);
        if (native) attach(name, native, generation);
      },
      pause() { native?.close(); native = null; openedAt = null; },
      close() { closed = true; stream.pause(); streams.delete(stream); },
      open() {
        if (closed || disposed || mode !== 'ready' || native) return;
        const gen = generation;
        native = new EventSource(url, { withCredentials: true });
        const source = native;
        source.onopen = event => {
          if (source !== native || gen !== generation || mode !== 'ready') return;
          openedAt = monotonicNow();
          stream.onopen?.(event);
          // Refresh after the new stream reaches HEAD, closing the gap
          // between the previous board snapshot and reconnection.
          mount?.onResume();
        };
        source.onmessage = event => {
          if (source !== native || gen !== generation || mode !== 'ready') return;
          recoveryUsed = false;
          readRecoveryUsed = false;
          streamRecoveryUsed = false;
          stream.onmessage?.(event);
        };
        source.onerror = event => {
          if (source !== native || gen !== generation || mode !== 'ready') return;
          stream.onerror?.(event);
          // Quiet fleets send comment pings, not message events. A connection
          // that really stayed open for 30 seconds earns one more probe; a fast
          // open/error cycle and successful HTTP reads never rearm the budget.
          const elapsed = openedAt === null ? 0 : monotonicNow() - openedAt;
          if (streamRecoveryUsed && !(Number.isFinite(elapsed) && elapsed >= 30000)) {
            pause('unavailable'); return;
          }
          streamRecoveryUsed = true;
          void checkSession();
        };
        for (const name of listeners.keys()) attach(name, source, gen);
      },
    };
    function attach(name, source, gen) {
      // At most one native listener per event name per source.
      source._ownerEvents ??= new Set();
      if (source._ownerEvents.has(name)) return;
      source._ownerEvents.add(name);
      source.addEventListener(name, event => {
        if (source !== native || gen !== generation || mode !== 'ready') return;
        for (const callback of listeners.get(name) || []) callback(event);
      });
    }
    streams.add(stream);
    stream.open();
    return stream;
  }
  function mountSessionControls({ document, element, onPause = () => {}, onResume = () => {}, onActionPause = () => {} }) {
    mount = { status: document.getElementById('owner-session-status'),
      renew: document.getElementById('owner-session-renew'),
      logout: document.getElementById('owner-session-logout'),
      check: document.getElementById('owner-session-check'), onPause, onResume, onActionPause };
    element.hidden = false;
    mount.renew.addEventListener('click', () => { void mutate('renew'); });
    mount.logout.addEventListener('click', () => { void mutate('logout'); });
    mount.check.addEventListener('click', () => { recoveryUsed = false; readRecoveryUsed = false; streamRecoveryUsed = false; void checkSession(); });
    render();
    const ready = checkSession();
    return { ready, dispose };
  }
  function dispose() {
    disposed = true;
    pause('unavailable');
    for (const stream of [...streams]) stream.close();
  }
  function readHandle() {
    const handle = Object.freeze({ jget, createEventSource, dispose, snapshot: readerSnapshot,
      start() {
        recoveryUsed = false; readRecoveryUsed = false; streamRecoveryUsed = false;
        return checkSession(); // Explicit read-only Check; never login or replay.
      },
      subscribe(listener) {
        stateListeners.add(listener);
        return () => stateListeners.delete(listener);
      } });
    readHandles.add(handle);
    return handle;
  }
  return { jget, createEventSource, mountSessionControls, interactionContext, nudgeContext, feedbackContext, prepareAction, sendAction, actionReceipt, readHandle };
}

const owner = createOwnerTransport();
export const jget = owner.jget;
export const createEventSource = owner.createEventSource;
export const mountSessionControls = owner.mountSessionControls;
export const interactionContext = owner.interactionContext;
export const sendAction = owner.sendAction;
export const actionReceipt = owner.actionReceipt;

export const nudgeContext = owner.nudgeContext;
export const feedbackContext = owner.feedbackContext;
export const prepareAction = owner.prepareAction;

// Same authenticated admission and pinned-host checker, without global controls or actions.
// The caller supplies a trusted carrier; this does not provision another host's session.
export function createOwnerReadHandle(options = {}) {
  return createOwnerTransport({ ...options, onAccessEnded: () => {} }).readHandle();
}
