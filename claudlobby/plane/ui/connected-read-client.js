import { createOwnerTransport, ReadRequestUnavailable, MAX_SAVED_COMPUTERS } from "./owner-api-client.js";
import { createSourceActionInterface } from "./connected-actions.js";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const HOST = /^host_[0-9a-f]{32}$/;
const SECRET = /^[A-Za-z0-9_-]{43}$/;
const READS = { summary: [], overview: [], fleets: [], trust: [],
  channel: ['fleet', 'limit'], tasks: ['fleet'], identities: ['fleet'],
  inventory: ['fleet'], org: ['fleet'], utilization: ['fleet'], presence: ['fleet'],
  equipment: ['alias'], search: ['q', 'fleet', 'limit'], grid: ['fleet', 'focus'],
  stream: ['cursor', 'once'] };
const keys = (v, fields) => v !== null && typeof v === 'object' && !Array.isArray(v)
  && Object.keys(v).sort().join() === [...fields].sort().join();
const uuid = v => typeof v === 'string' && v.length === 36 && UUID.test(v);
const host = v => typeof v === 'string' && v.length === 37 && HOST.test(v);
const positive = v => Number.isSafeInteger(v) && v > 0;
const RENEW_WINDOW = 120000, ACTIVITY_WINDOW = 300000;
function origin(v) {
  if (typeof v !== 'string' || v.length > 2048) return false;
  try { const u = new URL(v); return u.protocol === 'https:' && u.origin === v && !u.hostname.includes('*') && !u.username && !u.password; }
  catch { return false; }
}
function secret(v) {
  // Canonical32-byte base64url: the final character's unused two bits are zero.
  return typeof v === 'string' && v.length === 43 && SECRET.test(v)
    && 'AEIMQUYcgkosw048'.includes(v.at(-1));
}
const cursor = v => typeof v === 'string' && /^(0|[1-9][0-9]*)$/.test(v)
  && Number.isSafeInteger(Number(v));

// Each connected source owns its slots. Context is a read despite its POST
// carrier; lifecycle and write requests never enter this queue.
function finiteReadGate() {
  let active = 0;
  const queue = [];
  function drain() {
    while (active < 2 && queue.length) {
      const job = queue.shift();
      job.signal.removeEventListener('abort', job.abort);
      if (job.signal.aborted) { job.reject(new ReadRequestUnavailable()); continue; }
      active++;
      Promise.resolve().then(job.dispatch).then(job.resolve, job.reject).finally(() => {
        active--; drain();
      });
    }
  }
  return (dispatch, signal) => new Promise((resolve, reject) => {
    if (signal.aborted || queue.length >= 8) { reject(new ReadRequestUnavailable()); return; }
    const job = { dispatch, signal, resolve, reject, abort() {
      const index = queue.indexOf(job);
      if (index !== -1) queue.splice(index, 1);
      signal.removeEventListener('abort', job.abort);
      reject(new ReadRequestUnavailable());
    } };
    signal.addEventListener('abort', job.abort, { once: true });
    queue.push(job); drain();
  });
}

/** Trusted document/account boundary in; only a real host handshake admits reads.
 * No credentials, assertion data, global controls or actions are exported.
 */
export function createConnectedReadClient({ reference, documentContext, currentContext,
  obtainSessionAssertion, fetch = globalThis.fetch.bind(globalThis),
  documentOrigin = globalThis.location?.origin,
  monotonicNow = () => globalThis.performance.now(),
  onContinuityChange = () => {},
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout } = {}) {
  if (!keys(reference, ['reference_id', 'endpoint_origin', 'revision']) || !uuid(reference.reference_id)
      || !origin(reference.endpoint_origin) || !positive(reference.revision)
      || !keys(documentContext, ['issuer', 'operational_origin', 'subject', 'workspace_id', 'context_generation'])
      || !origin(documentContext.issuer) || !origin(documentContext.operational_origin)
      || documentOrigin !== documentContext.operational_origin
      || ![documentContext.subject, documentContext.workspace_id, documentContext.context_generation].every(uuid)
      || typeof currentContext !== 'function' || typeof obtainSessionAssertion !== 'function')
    throw new Error('A current authenticated Plane document and exact saved reference are required.');
  const ref = Object.freeze({ ...reference }), doc = Object.freeze({ ...documentContext });
  let token = null, pending = null, binding = null, hostUID = null, expires = 0;
  let epoch = 0, disposed = false, ended = false, busy = false, lastNow = null, expiryTimer = null;
  let continuityTimer = null, visible = false, lastActivity = null, autoAttempted = false;
  let renewalUnknown = false, continuityPaused = false, actionRequests = 0;
  let renewing = false, disconnectRequested = false, reconnectRequired = false;
  let accessRejected = false, connectIncomplete = false;
  const readFailures = new Map();
  const requests = new Set(), cursors = new Map();
  let setupOnly = false, setupSignedOut = false; const setupListeners = new Set();
  const snapshot = () => setupOnly ? Object.freeze({...handle.snapshot(),state:"setup_only",epoch,remediation:"First-team setup only; dashboard reads are not admitted."})
    : setupSignedOut ? Object.freeze({...handle.snapshot(),state:"signed_out",epoch}) : handle.snapshot();
  const notify = () => { for (const listener of setupListeners) { try { listener(snapshot()); } catch {} } };
  let owner, handle, actions = null, actionEpoch = 0, actionSourceChanged = () => {}, unsubscribe = () => {};
  function now() {
    const value = monotonicNow();
    if (!Number.isFinite(value) || value < 0 || (lastNow !== null && value < lastNow)) {
      stop('The browser clock is unavailable. Reopen Plane before continuing.', true);
      throw new Error('Connected reads unavailable.');
    }
    lastNow = value;
    return value;
  }
  function wipe() {
    token = null; pending = null; expires = 0; renewalUnknown = false; setupOnly = false; setupSignedOut = false;
    clearTimeout(expiryTimer); expiryTimer = null;
    clearTimeout(continuityTimer); continuityTimer = null;
    for (const request of requests) request.abort();
  }
  function stop(note, permanent = false) {
    epoch++; ended ||= permanent; wipe();
    if (permanent) { reconnectRequired = false; connectIncomplete = false; } // Presentation cannot override a terminal refusal.
    owner?.sessionControls.suspend(note); notify();
  }
  function contextMatches() {
    let c;
    try { c = currentContext(); } catch { return null; }
    if (c == null) return null;
    if (!keys(c, ['version', 'user', 'workspace', 'context_generation', 'references']) || c.version !== 2
        || !keys(c.user, ['id']) || !keys(c.workspace, ['id', 'name'])
        || c.user.id !== doc.subject || c.workspace.id !== doc.workspace_id
        || c.context_generation !== doc.context_generation || !Array.isArray(c.references) || c.references.length > MAX_SAVED_COMPUTERS) return false;
    const matches = c.references.filter(r => r?.reference_id === ref.reference_id);
    return matches.length === 1 && keys(matches[0], ['reference_id', 'endpoint_origin', 'revision'])
      && matches[0].endpoint_origin === ref.endpoint_origin && matches[0].revision === ref.revision;
  }
  function valid(requireToken = false) {
    if (disposed || ended) return false;
    const matches = contextMatches();
    if (matches === null) {
      stop('Current authenticated workspace metadata is unavailable. Check it before signing in again.');
      return false;
    }
    if (!matches) {
      stop('The authenticated account, workspace or saved computer changed. Reopen Plane.', true);
      return false;
    }
    let time;
    try { time = now(); } catch { return false; }
    if (requireToken && (!token || time >= expires)) {
      reconnectRequired ||= !!token && time >= expires;
      stop('The connected session expired. Choose Connect again.');
      return false;
    }
    return true;
  }
  function armExpiry() {
    clearTimeout(expiryTimer);
    const gen = epoch;
    expiryTimer = setTimeout(() => {
      if (!disposed && gen === epoch) {
        reconnectRequired = true;
        stop('The connected session expired. Choose Connect again.');
      }
    }, Math.max(0, expires - now()));
  }
  function continuity() {
    const remaining = token ? Math.max(0, expires - now()) : 0;
    return Object.freeze({ warning: !!token && remaining <= RENEW_WINDOW,
      // An incomplete Connect needs a new explicit handshake, not a status probe.
      // Keep that recovery separate from known expiry and terminal refusals.
      connectRequired: connectIncomplete && !token && !ended && !accessRejected && handle.snapshot().state === 'unavailable',
      renewalUnknown, reconnectRequired, remainingSeconds: Math.ceil(remaining / 1000) });
  }
  function continuityChanged() {
    try { onContinuityChange(); } catch { /* presentation cannot change admission */ }
  }
  function scheduleContinuity() {
    clearTimeout(continuityTimer); continuityTimer = null;
    if (disposed || ended || !token || continuityPaused || renewalUnknown) return;
    const remaining = expires - now(), gen = epoch;
    if (remaining <= 0) return; // The existing expiry gate owns termination.
    if (remaining <= RENEW_WINDOW) {
      continuityChanged();
      if (!visible || lastActivity === null || now() - lastActivity > ACTIVITY_WINDOW
          || autoAttempted || busy || actionRequests || handle.snapshot().state !== 'ready') return;
    }
    continuityTimer = setTimeout(() => {
      continuityTimer = null;
      if (disposed || gen !== epoch || !valid(true)) return;
      if (expires - now() > RENEW_WINDOW) { scheduleContinuity(); return; }
      continuityChanged();
      if (visible && lastActivity !== null && now() - lastActivity <= ACTIVITY_WINDOW
          && !autoAttempted && !busy && !actionRequests && handle.snapshot().state === 'ready') {
        autoAttempted = true;
        void renew();
      }
    }, Math.max(0, remaining - RENEW_WINDOW));
  }
  function activity({ visible: isVisible, userInput = false }) {
    if (disposed || ended) return;
    visible = isVisible === true;
    if (visible && userInput) lastActivity = now();
    scheduleContinuity();
  }
  function readRoute(url, streaming = false) {
    if (typeof url !== 'string' || !url.startsWith('/api/') || url.startsWith('//')) throw new Error();
    const u = new URL(url, ref.endpoint_origin);
    if (u.origin !== ref.endpoint_origin || u.hash || u.username || u.password) throw new Error();
    const suffix = u.pathname.slice(5);
    const fields = /^tasks\/wi_[0-9a-f]{32}$/.test(suffix) ? ['fleet'] : Object.hasOwn(READS, suffix) ? READS[suffix] : undefined;
    if (!fields || (streaming && suffix !== 'stream') || (!streaming && suffix === 'stream')) throw new Error();
    const names = [...u.searchParams.keys()];
    if (names.length !== new Set(names).size || names.some(k => !fields.includes(k))) throw new Error();
    if (suffix === 'stream' && u.searchParams.has('cursor') && !cursor(u.searchParams.get('cursor'))) throw new Error();
    return { url: ref.endpoint_origin + '/api/owner/connected/read/' + suffix + u.search, key: u.pathname + u.search };
  }
  async function boundedJSON(response, limit) {
    // A synthetic fetch used by tests may supply json(), but shipped Fetch uses
    // a bounded streaming body rather than an unbounded Response.json().
    if (!response.body?.getReader) {
      const data = await response.json();
      if (new TextEncoder().encode(JSON.stringify(data)).length > limit) throw new Error();
      return data;
    }
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true });
    let text = '', bytes = 0;
    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > limit) throw new Error();
        text += decoder.decode(chunk.value, { stream: true });
      }
      return JSON.parse(text + decoder.decode());
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
  async function http(path, { method = 'GET', body, signal, timeout = 15000, bearer = token,
    limit = 16384 } = {}) {
    const start = now();
    const controller = new AbortController(), relay = () => controller.abort(signal?.reason);
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', relay, { once: true });
    requests.add(controller);
    const timer = setTimeout(() => controller.abort(new DOMException('Read request timed out.', 'TimeoutError')), timeout), gen = epoch;
    try {
      const response = await fetch(path, { method, credentials: 'omit', cache: 'no-store', redirect: 'error',
        signal: controller.signal, headers: { ...(bearer ? { Authorization: 'Bearer ' + bearer } : {}),
          ...(body !== undefined ? { 'Content-Type': 'application/json', 'X-Claudlobby-Owner': '1' } : {}) },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      let data;
      if (response.status !== 204) {
        try { data = await boundedJSON(response, limit); }
        catch { if (response.status === 403) data = { state: 'denied' }; else throw new Error(); }
      }
      if (controller.signal.aborted || disposed || gen !== epoch || (bearer && bearer !== token) || !valid()) throw new Error();
      if ([401,403].includes(response.status) && /\/connected\/(challenge|issue|status|renew)$/.test(path)) accessRejected = true;
      return { status: response.status, data, start };
    } catch (error) {
      if (controller.signal.reason?.name === 'TimeoutError') {
        const failure = new Error('Read request timed out.'); failure.readTimedOut = true; throw failure;
      }
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', relay);
      requests.delete(controller);
    }
  }
  function admission(data, start, needsToken) {
    const setup = data?.state === 'setup_only';
    const fields = ['version', 'state', 'expires_in', setup ? 'setup_profile' : 'read_profile', 'session_context', ...(needsToken ? ['token'] : [])];
    const c = data?.session_context;
    if (!keys(data, fields) || data.version !== 1 || !['ready','setup_only'].includes(data.state)
        || !Number.isSafeInteger(data.expires_in) || data.expires_in <= 0 || data.expires_in > 900
        || !keys(c, ['issuer', 'subject', 'workspace_id', 'binding_revision', 'binding_generation', 'operational_origin'])
        || c.issuer !== doc.issuer || c.subject !== doc.subject || c.workspace_id !== doc.workspace_id
        || c.operational_origin !== doc.operational_origin || c.binding_revision !== binding?.binding_revision
        || c.binding_generation !== binding?.binding_generation
        || (setup ? (!keys(data.setup_profile,['version','profile','host_uid']) || data.setup_profile.version !== 1 || data.setup_profile.profile !== 'first-team-setup-v1' || data.setup_profile.host_uid !== hostUID) : data.read_profile?.host_uid !== hostUID)
        || (needsToken && !secret(data.token)) || !valid() || start + data.expires_in * 1000 <= now()) {
      stop('Connected session compatibility or bound identity could not be verified. Reopen Plane.', true);
      throw new Error();
    }
    // The single canonical owner checker still validates and pins read_profile.
    pending = { data, token: needsToken ? data.token : token, expires: start + data.expires_in * 1000, setup };
    return data;
  }
  function commitSetup() {
    if (!pending?.setup || !valid()) throw new Error();
    token = pending.token; expires = pending.expires; pending = null; setupOnly = true; armExpiry(); notify();
  }
  async function carrier(url, options) {
    // A read-only probe cannot expire/abort an explicit sign-in before issue.
    // The pending issued response still goes through the canonical checker.
    if (url === '/api/owner/status' && busy && !token && !pending) throw new Error();
    if (!valid()) throw new Error();
    if (url === '/api/owner/status' && pending) return { status: 200, json: async () => pending.data };
    const action = { '/api/owner/status': 'status', '/api/owner/renew': 'renew', '/api/owner/logout': 'logout' }[url];
    if (action) {
      // No session has expired before the first admitted host challenge.
      // Retained binding/uncertain issuance still follows normal expiry handling.
      if ((action === 'status' && !token && !binding) || !valid(true)) return { status: 403, json: async () => ({ state: 'denied' }) };
      const result = await http(ref.endpoint_origin + '/api/owner/connected/' + action,
        { method: action === 'status' ? 'GET' : 'POST', ...(action !== 'status' ? { body: {} } : {}), signal: options.signal });
      if (result.status === 200 && action !== 'logout') admission(result.data, result.start, action === 'renew');
      if (result.status === 200 && action === 'logout'
          && (!keys(result.data, ['version', 'state']) || result.data.version !== 1 || result.data.state !== 'signed_out')) throw new Error();
      return { status: result.status, json: async () => result.data };
    }
    if (!valid(true)) throw new Error();
    const actionPath = /^\/api\/owner\/actions\/(context|prepare|send|receipt)$/.exec(url);
    if (actionPath) {
      if (options.method !== 'POST') throw new Error();
      actionRequests++;
      try {
        const result = await http(ref.endpoint_origin + '/api/owner/connected/actions/' + actionPath[1],
          {method:'POST',body:JSON.parse(options.body),signal:options.signal,timeout:actionPath[1] === 'send' ? 45000 : 15000,limit:actionPath[1] === 'context' ? 2097152 : 32768});
        return {status:result.status,json:async () => result.data};
      } finally { actionRequests--; scheduleContinuity(); }
    }
    const route = readRoute(url);
    try {
      const result = await http(route.url, { signal: options.signal, limit: 8 * 1024 * 1024 });
      readFailures.delete(url);
      return { status: result.status, json: async () => result.data };
    } catch (error) {
      readFailures.set(url, error.readTimedOut === true ? 'timed_out' : 'unavailable');
      throw new ReadRequestUnavailable();
    }
  }

  class FetchStream {
    constructor(url) {
      this.listeners = new Map(); this.closed = false; this.controller = null;
      this.live = () => false;
      this.onopen = null; this.onmessage = null; this.onerror = null;
      this.done = this.run(url);
    }
    addEventListener(name, fn) {
      if (!this.listeners.has(name)) this.listeners.set(name, new Set());
      this.listeners.get(name).add(fn);
    }
    close() { this.closed = true; this.controller?.abort(); }
    emit(name, event, delivered = () => {}) {
      if (!this.live(name === 'error')) return false;
      delivered(); // Commit only this admitted frame, before consumer callbacks.
      this['on' + name]?.(event);
      for (const fn of this.listeners.get(name) || []) {
        if (!this.live()) break;
        fn(event);
      }
      return true;
    }
    async run(url) {
      let reader;
      try {
        if (!valid(true)) throw new Error();
        const route = readRoute(url, true), gen = epoch, controller = new AbortController();
        this.controller = controller; requests.add(controller);
        this.live = (allowAborted = false) => !this.closed && (allowAborted || !controller.signal.aborted) && gen === epoch
          && handle.snapshot().state === 'ready' && valid(true);
        const timer = setTimeout(() => controller.abort(), 15000);
        let response;
        try {
          response = await fetch(route.url, { method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error',
            signal: controller.signal, headers: { Authorization: 'Bearer ' + token,
              ...(cursors.has(route.key) ? { 'Last-Event-ID': cursors.get(route.key) } : {}) } });
        } finally { clearTimeout(timer); }
        if (!this.live()
            || response.status !== 200 || !/^text\/event-stream(?:;|$)/i.test(response.headers.get('content-type') || '')
            || !response.body?.getReader) throw new Error();
        reader = response.body.getReader();
        this.emit('open', { type: 'open' });
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let buffer = '', data = [], event = 'message', id = null, size = 0;
        const line = text => {
          size += new TextEncoder().encode(text).length;
          if (size > 8 * 1024 * 1024) throw new Error();
          if (!text) {
            if (data.length) this.emit(event, { type: event, data: data.join('\n'), lastEventId: id ?? cursors.get(route.key) ?? '' },
              () => {
                if (id !== null) cursors.set(route.key, id);
                if (event === 'source') {
                  try { if (['absent','unreadable'].includes(JSON.parse(data.join('\n')).state)) { actionEpoch++; actions?.invalidate(); actionSourceChanged(); } } catch {}
                }
              });
            else if (id !== null && this.live()) cursors.set(route.key, id);
            data = []; event = 'message'; id = null; size = 0;
            return;
          }
          if (text.startsWith(':')) return; // canonical ping, no new recovery loop
          const colon = text.indexOf(':'), field = colon < 0 ? text : text.slice(0, colon);
          let value = colon < 0 ? '' : text.slice(colon + 1); if (value.startsWith(' ')) value = value.slice(1);
          if (field === 'data') data.push(value);
          else if (field === 'id') { if (!cursor(value)) throw new Error(); id = value; }
          else if (field === 'event') { if (!['message', 'source'].includes(value)) throw new Error(); event = value; }
          else if (field === 'retry' && (!cursor(value) || Number(value) > 60000)) throw new Error();
        };
        const lines = final => {
          while (this.live()) {
            const match = /[\r\n]/.exec(buffer); if (!match) break;
            const index = match.index;
            if (!final && buffer[index] === '\r' && index === buffer.length - 1) break;
            const width = buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1;
            line(buffer.slice(0, index)); buffer = buffer.slice(index + width);
          }
        };
        while (this.live()) {
          const chunk = await reader.read();
          if (!this.live()) return;
          if (chunk.done) { buffer += decoder.decode(); lines(true); break; } // never dispatch an incomplete frame
          if (chunk.value.byteLength > 8 * 1024 * 1024) throw new Error();
          buffer += decoder.decode(chunk.value, { stream: true });
          if (buffer.length > 8 * 1024 * 1024) throw new Error();
          lines(false);
        }
        if (!this.closed && gen === epoch) throw new Error();
      } catch {
        if (!this.closed && !disposed) this.emit('error', { type: 'error' });
      } finally {
        await reader?.cancel().catch(() => {}); reader?.releaseLock();
        if (this.controller) requests.delete(this.controller);
      }
    }
  }

  owner = createOwnerTransport({ fetch: carrier, EventSource: FetchStream,
    monotonicNow, setTimeout, clearTimeout, requestTimeout: 15000, actionTimeout:15000,
    readGate: finiteReadGate(),
    verifyReadFailures: true, onAccessEnded: () => {},
    onActionPause:(scope,kind,recipient) => actions?.pause(scope,kind,recipient),
    onDispose() {
      disposed = true; epoch++; wipe(); actions?.invalidate(); cursors.clear(); binding = null; unsubscribe();
    },
    onReadAdmitted() {
      if (!pending || !valid()) throw new Error();
      if (token !== pending.token) autoAttempted = false;
      token = pending.token; expires = pending.expires; pending = null; connectIncomplete = false; armExpiry();
      // A rotating token is retained only long enough to sign it out. Do not
      // publish a ready session after an explicit disconnect during renewal.
      if (disconnectRequested) throw new Error('Disconnect requested.');
      scheduleContinuity();
    } });
  handle = owner.readHandle();
  unsubscribe = handle.subscribe(state => {
    if (['denied', 'signed_out'].includes(state.state)) { epoch++; wipe(); }
    else if (state.state === 'unavailable') pending = null;
    if (state.state === 'ready') scheduleContinuity();
    notify();
  });
  async function connect() {
    if (disposed || ended || busy || !valid()) return false;
    busy = true; disconnectRequested = false; continuityPaused = false; renewalUnknown = false; autoAttempted = false; reconnectRequired = false;
    accessRejected = false; connectIncomplete = false;
    stop('Signing in to the selected computer…');
    const gen = epoch;
    try {
      const result = await http(ref.endpoint_origin + '/api/owner/connected/challenge', { method: 'POST', body: {}, bearer: null });
      const c = result.data;
      if (result.status !== 202 || !keys(c, ['version', 'state', 'nonce', 'issuer', 'host_uid', 'operational_origin',
          'binding_revision', 'binding_generation', 'expires_at']) || c.version !== 1 || c.state !== 'awaiting_session_assertion'
          || !secret(c.nonce) || c.issuer !== doc.issuer || c.operational_origin !== doc.operational_origin
          || !host(c.host_uid) || (hostUID !== null && hostUID !== c.host_uid)
          || !positive(c.binding_revision) || !uuid(c.binding_generation) || !positive(c.expires_at)
          || (binding && (binding.binding_revision !== c.binding_revision || binding.binding_generation !== c.binding_generation))) throw new Error();
      hostUID = c.host_uid;
      binding = { binding_revision: c.binding_revision, binding_generation: c.binding_generation };
      const signing = new AbortController(); requests.add(signing);
      const timer = setTimeout(() => signing.abort(), 15000);
      let evidence;
      try {
        evidence = await Promise.race([
          obtainSessionAssertion({ workspace_id: doc.workspace_id, host_uid: hostUID, nonce: c.nonce,
            operational_origin: doc.operational_origin, ...binding, reference_id: ref.reference_id,
            expected_context_generation: doc.context_generation }, { signal: signing.signal }),
          new Promise((_, reject) => signing.signal.addEventListener('abort', () => reject(new Error()), { once: true })),
        ]);
      } finally { clearTimeout(timer); requests.delete(signing); }
      if (disposed || gen !== epoch || !valid() || !keys(evidence, ['version', 'assertion', 'expires_at'])
          || evidence.version !== 1 || typeof evidence.assertion !== 'string' || evidence.assertion.length > 8192
          || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(evidence.assertion)
          || !positive(evidence.expires_at)) throw new Error();
      const issued = await http(ref.endpoint_origin + '/api/owner/connected/issue', { method: 'POST',
        body: { nonce: c.nonce, assertion: evidence.assertion }, bearer: null, timeout: 35000 });
      if (issued.status !== 200 || disposed || gen !== epoch) throw new Error();
      admission(issued.data, issued.start, true);
      if (pending.setup) commitSetup();
      else await handle.start(); // Only a real read profile reaches the canonical checker.
      return ['ready','setup_only'].includes(snapshot().state);
    } catch {
      if (!disposed && gen === epoch) {
        connectIncomplete = !ended && !accessRejected;
        stop('Sign-in did not complete. Its host outcome is unknown; sign in again explicitly.');
      }
      return false;
    } finally { busy = false; scheduleContinuity(); }
  }
  async function renew() {
    if (setupOnly) return lifecycle('renew');
    if (busy || !valid(true)) return;
    busy = true; renewing = true;
    try {
      await owner.sessionControls.renew();
      renewalUnknown = handle.snapshot().state !== 'ready';
    } finally {
      busy = false; renewing = false;
      if (disconnectRequested) await logout();
      else { continuityChanged(); scheduleContinuity(); }
    }
  }
  async function check() {
    if (setupOnly) return lifecycle('status');
    await handle.start();
    if (handle.snapshot().state === 'ready') {
      renewalUnknown = false; continuityPaused = false; autoAttempted = false;
      scheduleContinuity(); continuityChanged();
    }
  }
  async function logout() {
    if (setupOnly) return lifecycle('logout');
    disconnectRequested = true; continuityPaused = true;
    clearTimeout(continuityTimer); continuityTimer = null;
    if (renewing) return; // The renewal completion signs out its rotated token.
    if (busy) { stop('Disconnected. Choose Connect again to return.'); return; }
    if (!valid(true)) return;
    busy = true;
    try { await owner.sessionControls.logout(); }
    finally {
      busy = false;
      if (handle.snapshot().state !== 'signed_out')
        stop('Disconnected in this view. Host sign-out was not confirmed. Choose Connect again to return.');
    }
  }
  async function lifecycle(action) {
    if (!setupOnly && action === 'status') return handle.start();
    if (busy || !valid(true)) return false;
    busy = true;
    try {
      if (!setupOnly) return await (action === 'renew' ? owner.sessionControls.renew() : owner.sessionControls.logout());
      const result = await http(ref.endpoint_origin + '/api/owner/connected/' + action,
        {method:action === 'status' ? 'GET' : 'POST',...(action === 'status' ? {} : {body:{}})});
      if (result.status !== 200) { stop('Connection could not be confirmed. Check again explicitly.'); return false; }
      if (action === 'logout') {
        if (!keys(result.data,['version','state']) || result.data.version !== 1 || result.data.state !== 'signed_out') throw new Error();
        stop('Disconnected. Connect again explicitly.');
        setupSignedOut = true; notify(); return true;
      }
      admission(result.data,result.start,action === 'renew');
      if (pending.setup) commitSetup();
      else { setupOnly = false; await handle.start(); notify(); }
      return ['ready','setup_only'].includes(snapshot().state);
    } catch { stop('Connection outcome unknown. Check again explicitly.'); return false; }
    finally { busy = false; }
  }
  async function setupRequest(operation, input) {
    if (!valid(true)) throw new Error('Setup outcome unknown. Check the original activation receipt.');
    const result = await http(ref.endpoint_origin + '/api/owner/connected/setup/' + operation,
      {method:'POST',body:input,timeout:operation === 'launch' ? 45000 : 15000,limit:131072});
    if (result.status !== 200) throw new Error('Setup unavailable or refused. A lost launch must be inspected, never repeated.');
    return result.data;
  }
  function dispose() {
    if (disposed) return;
    handle.dispose(); setupListeners.clear();
  }
  return Object.freeze({ connect, start: () => setupOnly ? lifecycle('status') : handle.start(), check, activity, continuity,
    renew, logout,
    setupContext: () => setupRequest('context',{}), setupPreview: input => setupRequest('preview',input),
    setupLaunch: input => setupRequest('launch',input), setupReceipt: input => setupRequest('receipt',input),
    createActions({key,onPause,onSourceChange = () => {}} = {}) {
      if (disposed || actions) throw new Error('The selected source action interface is unavailable.');
      actionSourceChanged = onSourceChange;
      actions = createSourceActionInterface({key,owner,snapshot:handle.snapshot,
        stamp:() => `${epoch}:${actionEpoch}:${handle.snapshot().epoch}`,onPause});
      return actions;
    },
    // Presentation only: the original read handle remains the admission owner.
    connectionState() {
      if (setupOnly) return 'setup_only';
      const state = snapshot().state;
      if (state === 'ready') return 'ready';
      if (accessRejected) return 'denied';
      if (state === 'signed_out') return 'disconnected';
      return state === 'denied' && !binding ? 'not_connected' : state;
    },
    readFailure: url => readFailures.get(url),
    dispose, readHandle: handle, snapshot, subscribe(listener) { setupListeners.add(listener); return () => setupListeners.delete(listener); } });
}
