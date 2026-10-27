// First-use approval consumer. Only the local terminal can bind a workspace.
const keys = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
const hostID = value => typeof value === 'string' && value.length === 37 && /^host_[0-9a-f]{32}$/.test(value);
const nonceID = value => typeof value === 'string' && value.length === 43 && /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/.test(value);
const jwt = value => typeof value === 'string' && value.length <= 8192 && /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(value);

export function createWorkspaceEnrollment({reference, issuer, operationalOrigin, currentContext,
  obtainAssertion, fetch = globalThis.fetch.bind(globalThis), now = () => Date.now() / 1000,
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout, onChange = () => {}}) {
  const endpoint = reference.endpoint_origin;
  let state = 'idle', code = null, expires = null, timer = null, epoch = 0, disposed = false, panel = null;
  let active = null;
  const busy = () => ['challenging','asserting','staging'].includes(state);
  const snapshot = () => Object.freeze({state, expires_at:expires, busy:busy()});
  const notify = () => { paint(); try { onChange(snapshot()); } catch {} };
  // These bounds limit local retention; the host decides validity with its own clock.
  const validTime = (value, lifetime = 300) => Number.isFinite(now()) && Number.isSafeInteger(value)
    && value > now() && value <= now() + lifetime + 5;
  const current = ticket => {
    if (disposed || ticket !== epoch) return false;
    if (!currentContext()) { forget('refused'); return false; }
    return true;
  };
  function forget(next = 'idle') {
    epoch++; active?.abort(); active = null; clearTimeout(timer); timer = null; code = null; expires = null;
    state = next; notify();
  }
  function deadline(value) {
    expires = expires === null ? value : Math.min(expires, value);
    clearTimeout(timer); timer = setTimeout(() => forget('expired'), Math.max(0, (expires - now()) * 1000));
  }
  async function hostRequest(path, body, signal) {
    const abort = new AbortController(), cancel = () => abort.abort();
    signal.addEventListener('abort', cancel, {once:true}); if (signal.aborted) cancel();
    let timeout, reader;
    const request = async () => {
      const response = await fetch(endpoint + '/api/owner/workspace/' + path, {method:'POST',
        credentials:'omit', cache:'no-store', redirect:'error', signal:abort.signal,
        headers:{'Content-Type':'application/json','X-Claudlobby-Owner':'1'}, body:JSON.stringify(body)});
      if (abort.signal.aborted) throw new Error();
      if (response.status !== 202) throw new Error();
      if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') throw new Error();
      reader = response.body?.getReader(); if (!reader) throw new Error();
      const chunks = []; let length = 0;
      for (;;) { const part = await reader.read(); if (part.done) break;
        length += part.value.byteLength; if (length > 16384) throw new Error(); chunks.push(part.value); }
      if (abort.signal.aborted) throw new Error();
      const bytes = new Uint8Array(length); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.byteLength; }
      return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
    };
    try { return await Promise.race([request(), new Promise((_,reject) => {
      abort.signal.addEventListener('abort',() => reject(new Error()),{once:true});
      if (abort.signal.aborted) reject(new Error());
      timeout = setTimeout(() => abort.abort(),35000);
    })]); }
    finally { clearTimeout(timeout); abort.abort(); signal.removeEventListener('abort',cancel);
      if (reader) void reader.cancel().catch(() => {}).finally(() => { try { reader.releaseLock(); } catch {} }); }
  }
  async function start() {
    // A retained or uncertain proposal must be inspected, never replayed by a click.
    if (disposed || busy() || ['awaiting_local_approval','unknown'].includes(state) || !currentContext()) return false;
    forget(); const ticket = epoch; active = new AbortController(); const signal = active.signal;
    let staged = false;
    try {
      state = 'challenging'; notify();
      const challenge = await hostRequest('challenge',{},signal);
      if (!current(ticket)) return false;
      if (!keys(challenge,['version','state','issuer','host_uid','nonce','operational_origin','expires_at'])
        || challenge.version !== 1 || challenge.state !== 'awaiting_assertion' || challenge.issuer !== issuer
        || challenge.operational_origin !== operationalOrigin || !hostID(challenge.host_uid)
        || !nonceID(challenge.nonce) || !validTime(challenge.expires_at)) throw new Error();
      code = challenge.nonce; deadline(challenge.expires_at); state = 'asserting'; notify();
      if (!current(ticket)) return false;
      const result = await obtainAssertion({workspace_id:currentContext().workspace.id,host_uid:challenge.host_uid,
        nonce:code,operational_origin:operationalOrigin},{signal});
      if (!current(ticket)) return false;
      if (!keys(result,['version','assertion','expires_at']) || result.version !== 1 || !jwt(result.assertion)
        || !validTime(result.expires_at,120)) throw new Error();
      deadline(result.expires_at);
      if (!validTime(expires)) { forget('expired'); return false; }
      state = 'staging'; staged = true; notify();
      if (!current(ticket)) return false;
      const response = await hostRequest('stage',{nonce:code,assertion:result.assertion},signal);
      if (!current(ticket)) return false;
      if (!keys(response,['version','state','expires_at']) || response.version !== 1
        || response.state !== 'awaiting_local_approval' || !validTime(response.expires_at)
        || response.expires_at > expires) throw new Error();
      deadline(response.expires_at); state = 'awaiting_local_approval'; notify(); return true;
    } catch {
      if (!current(ticket)) return false;
      if (staged && code && validTime(expires)) { state = 'unknown'; notify(); }
      else forget('refused');
      return false;
    } finally { if (ticket === epoch) active = null; }
  }
  function paint() {
    if (!panel) return;
    const {label, note, secret, instructions, cancel} = panel;
    label.textContent = disposed ? '' : `Local approval for workspace ${currentContext()?.workspace.name || ''} from ${issuer} on ${new URL(endpoint).hostname}. Disconnect an existing session first. Approval is only available at this computer’s local terminal.`;
    const visible = ['awaiting_local_approval','unknown'].includes(state) && !!code;
    note.textContent = state === 'awaiting_local_approval' ? 'Workspace proposal saved. Approve it locally on this computer, then choose Connect.'
      : state === 'unknown' ? 'The proposal may have been saved. Inspect it locally with this code. It has not been confirmed here; do not resend automatically.'
      : state === 'refused' ? 'Workspace approval could not be staged. Check the local owner/workspace status on this computer. An existing binding must be managed locally.'
      : state === 'expired' ? 'This approval code expired. Check local status before starting again.'
      : busy() ? 'Preparing local workspace approval…' : '';
    secret.textContent = visible ? 'Approval code: ' + code : '';
    instructions.hidden = !visible; cancel.hidden = state === 'idle' || disposed;
  }
  function cancel() { if (!disposed) forget(); }
  function mountPanel({document, element, onForget = () => {}}) {
    const label = document.createElement('span');
    const note = document.createElement('span'); note.setAttribute('role','status'); note.setAttribute('aria-live','polite');
    const secret = document.createElement('code');
    const instructions = document.createElement('span');
    instructions.textContent = 'On this computer, replace DATA_ROOT with its existing Claudlobby installation data root. Run claudlobby --root DATA_ROOT host owner confirm-workspace. Paste the approval code at the hidden prompt, inspect the issuer, account and workspace in the local preview, and approve there. Staging does not connect or grant fleet actions.';
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'pill ghost'; cancel.textContent = 'Forget approval code';
    cancel.addEventListener('click',() => { if (disposed) return; forget(); onForget(); }); element.append(label,note,secret,instructions,cancel);
    panel = {label,note,secret,instructions,cancel}; paint();
  }
  return Object.freeze({start,cancel,snapshot,mountPanel,refresh:paint,
    dispose() { if (disposed) return; disposed = true; forget('disposed'); }});
}
