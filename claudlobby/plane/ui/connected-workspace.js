// Account metadata selects navigation; only host-issued core handles admit reads.
import { createConnectedReadClient } from "./connected-read-client.js";
import { createWorkspaceEnrollment } from "./workspace-enrollment.js";
import { createWorkspaceReadTransport } from "./two-source-read.js";

const uuid = value => typeof value === 'string' && value.length === 36 && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const keys = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
function origin(value) {
  try {
    if (typeof value !== 'string' || value.length > 2048) return false;
    const url = new URL(value);
    return url.protocol === 'https:' && url.origin === value && !url.hostname.includes('*');
  }
  catch { return false; }
}
function references(value) {
  return Array.isArray(value) && value.length <= 2 && value.every(ref => keys(ref, ['reference_id', 'endpoint_origin', 'revision'])
    && uuid(ref.reference_id) && origin(ref.endpoint_origin) && Number.isSafeInteger(ref.revision) && ref.revision > 0)
    && new Set(value.map(ref => ref.reference_id)).size === value.length
    && new Set(value.map(ref => ref.endpoint_origin)).size === value.length;
}
const referenceKey = refs => JSON.stringify(refs.map(ref => [ref.reference_id, ref.endpoint_origin, ref.revision]).sort());
const failure = (state = 'unavailable', remediation = 'Open this workspace again to check its connection.') => ({state, provenance:null, remediation});

export function createConnectedWorkspace({ documentContext, documentOrigin = globalThis.location?.origin,
  fetch = globalThis.fetch.bind(globalThis), createClient = createConnectedReadClient,
  createTransport = createWorkspaceReadTransport, createEnrollment = createWorkspaceEnrollment, setTimeout = globalThis.setTimeout,
  clearTimeout = globalThis.clearTimeout, now = () => Date.now() / 1000, MutationObserver = globalThis.MutationObserver } = {}) {
  const validDocument = keys(documentContext, ['version', 'user_id', 'workspace_id', 'context_generation', 'shell_origin', 'operational_origin', 'references', 'expires_at'])
    && documentContext.version === 1 && [documentContext.user_id, documentContext.workspace_id, documentContext.context_generation].every(uuid)
    && origin(documentContext.shell_origin) && origin(documentContext.operational_origin)
    && documentContext.operational_origin === documentOrigin && documentContext.shell_origin !== documentOrigin
    && references(documentContext.references) && Number.isSafeInteger(documentContext.expires_at) && documentContext.expires_at > 0;
  // Copy before asynchronous work; callers cannot retarget a live document.
  const doc = validDocument ? JSON.parse(JSON.stringify(documentContext)) : null;
  let current = null, transport = null, clients = [], unsubscribes = [], mount = null;
  let phase = doc ? 'checking' : 'denied', accountUnavailable = false, disposed = false;
  let sequence = 0, loading = null, refreshTimer = null, paused = true;
  const requests = new Set(), streams = new Set(), listeners = new Set(), pending = new Set();
  const snapshot = () => Object.freeze({phase, accountUnavailable,
    computers: clients.map(({reference, client}) => ({reference_id:reference.reference_id,
      endpoint_origin:reference.endpoint_origin, ...client.snapshot()}))});
  const invoke = (fn, ...args) => { try { fn?.(...args); } catch { /* consumer callbacks cannot change admission */ } };
  function changed() {
    render();
    for (const listener of listeners) invoke(listener, snapshot());
  }
  function retire(next) {
    phase = next; current = null; sequence++;
    clearTimeout(refreshTimer); refreshTimer = null;
    for (const request of requests) request.abort();
    for (const stop of unsubscribes) stop(); unsubscribes = [];
    for (const {client,enrollment} of clients) { enrollment.dispose(); client.dispose(); }
    transport?.dispose(); transport = null;
    for (const stream of streams) stream.detach();
    paused = true; invoke(mount?.onPause);
    if (mount) {
      // A changed account cannot leave private records in a paused old document.
      mount.document.querySelector('main')?.replaceChildren();
      for (const id of ['fleet-totals', 'presence-strip', 'task-detail-content', 'focus-pane', 'focus-title'])
        mount.document.getElementById(id)?.replaceChildren();
      const title = mount.document.getElementById('focus-title');
      if (title) for (const key of Object.keys(title.dataset)) delete title.dataset[key];
      for (const button of mount.document.querySelectorAll('#view-nav button, #debug-toggle, [data-bot-inspect]')) {
        button.disabled = true; button.title = 'Close this view and open your workspace again.';
      }
      mount.document.getElementById('task-detail')?.close();
      const overlay = mount.document.getElementById('focus-overlay'); if (overlay) overlay.hidden = true;
    }
    changed();
  }
  async function jsonRequest(url, options) {
    const abort = new AbortController(); requests.add(abort);
    const external = options?.signal, cancel = () => abort.abort();
    external?.addEventListener('abort', cancel, {once:true});
    if (external?.aborted) abort.abort();
    let timeout;
    let reader;
    const operation = async () => {
      const response = await fetch(url, {...options, signal:abort.signal, cache:'no-store', redirect:'error', credentials:'include'});
      if (abort.signal.aborted) throw new Error();
      if (!response.ok) return {status:response.status, data:null};
      if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') throw new Error();
      reader = response.body?.getReader(); if (!reader) throw new Error();
      let length = 0; const chunks = [];
      for (;;) {
        const part = await reader.read(); if (part.done) break;
        length += part.value.byteLength; if (length > 16384) throw new Error(); chunks.push(part.value);
      }
      if (abort.signal.aborted) throw new Error();
      const bytes = new Uint8Array(length); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      return {status:response.status, data:JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes))};
    };
    try {
      return await Promise.race([operation(), new Promise((_, reject) => {
        const refused = () => reject(new Error('Account request stopped.'));
        abort.signal.addEventListener('abort', refused, {once:true});
        if (abort.signal.aborted) refused();
        timeout = setTimeout(() => { cancel(); reject(new Error('Account request timed out.')); }, 8000);
      })]);
    } finally {
      clearTimeout(timeout); abort.abort(); external?.removeEventListener('abort', cancel); requests.delete(abort);
      if (reader) void reader.cancel().catch(() => {}).finally(() => { try { reader.releaseLock(); } catch {} });
    }
  }
  function matches(data) {
    return keys(data, ['version','user','workspace','context_generation','references']) && data.version === 2
      && keys(data.user, ['id']) && data.user.id === doc.user_id
      && keys(data.workspace, ['id','name']) && data.workspace.id === doc.workspace_id
      && typeof data.workspace.name === 'string' && data.workspace.name.length > 0 && data.workspace.name.length <= 80
      && data.context_generation === doc.context_generation && references(data.references)
      && referenceKey(data.references) === referenceKey(doc.references);
  }
  async function accountAssertion(path, input, {signal}) {
    if (!current || disposed || phase !== 'ready') throw new Error('Reopen this workspace.');
    const before = sequence;
    const result = await jsonRequest(doc.shell_origin + path, {method:'POST', signal,
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(input)});
    if (disposed || before !== sequence || !current) throw new Error('The workspace changed.');
    if ([401,403,409].includes(result.status)) retire('denied');
    if (result.status !== 200) throw new Error('Sign-in could not be confirmed. Check the account and try again explicitly.');
    return result.data; // The host carrier owns the finite assertion DTO; never expose it through UI.
  }
  function admit(data) {
    const context = Object.freeze({...data, user:Object.freeze({...data.user}), workspace:Object.freeze({...data.workspace}),
      references:Object.freeze(data.references.map(ref => Object.freeze({...ref})))});
    if (clients.length || !doc.references.length) { current = context; return false; }
    const nextClients = [], nextSubscriptions = [];
    let nextTransport = null;
    try {
      for (const reference of doc.references) {
        const row = {reference,key:'c-' + BigInt('0x' + reference.reference_id.replaceAll('-','')).toString(36), client:createClient({reference,
        documentContext:{issuer:doc.shell_origin, operational_origin:doc.operational_origin, subject:doc.user_id,
          workspace_id:doc.workspace_id, context_generation:doc.context_generation},
        currentContext:() => current, obtainSessionAssertion:(input,options) => accountAssertion('/api/plane/session-assertion',input,options), documentOrigin, fetch, setTimeout, clearTimeout})};
        nextClients.push(row);
        row.actions = row.client.createActions({key:row.key,
          onPause:(...args) => invoke(mount?.onActionPause,...args),
          onSourceChange:() => { row.actionRecoveryPending = {}; invoke(mount?.onActionSourceChange,row.key); }});
        row.enrollment = createEnrollment({reference, issuer:doc.shell_origin, operationalOrigin:doc.operational_origin,
          currentContext:() => current, obtainAssertion:(input,options) => accountAssertion('/api/plane/host-assertion',input,options),
          fetch,now,setTimeout,clearTimeout,onChange:changed});
      }
      nextTransport = createTransport({sources:nextClients.map(({client,key}, index) => ({key,
        label:`Computer ${index + 1}`, reader:client.readHandle}))});
      for (const {client,actions,key} of nextClients) {
        let previous = client.snapshot();
        nextSubscriptions.push(client.subscribe(state => {
          if (state.epoch !== previous.epoch || state.state !== previous.state) {
            previous = state; actions.invalidate(); invoke(mount?.onActionSourceChange,key);
          }
          changed();
        }));
      }
      current = context; clients = nextClients; transport = nextTransport; unsubscribes = nextSubscriptions;
      // Source coverage owns board refreshes; account checks only resume a pause.
      for (const stream of streams) stream.attach();
      return true;
    } catch (error) {
      for (const stream of streams) stream.detach();
      for (const stop of nextSubscriptions) invoke(stop);
      invoke(() => nextTransport?.dispose());
      for (const {client,enrollment} of nextClients) {
        invoke(() => enrollment?.dispose()); invoke(() => client.dispose());
      }
      current = null; clients = []; transport = null; unsubscribes = [];
      throw error;
    }
  }
  async function checkAccount() {
    if (disposed || !doc || phase === 'denied') return false;
    if (loading) return loading;
    const ticket = sequence;
    loading = (async () => {
      try {
        const result = await jsonRequest(doc.shell_origin + '/api/plane/context?workspace_id=' + encodeURIComponent(doc.workspace_id), {method:'GET'});
        if (disposed || ticket !== sequence) return false;
        if ([401,403,409].includes(result.status) || result.status === 200 && !matches(result.data)) { retire('denied'); return false; }
        if (result.status !== 200) throw new Error();
        const created = admit(result.data); accountUnavailable = false; phase = clients.length ? 'ready' : 'empty';
        if (created) for (const {client} of clients) void client.start();
        changed();
        if (paused && mount) { paused = false; invoke(mount.onResume); }
        return true;
      } catch {
        if (disposed || ticket !== sequence) return false;
        accountUnavailable = true;
        if (!current) phase = 'unavailable'; // An outage never creates an empty or authenticated context.
        changed(); return false;
      } finally {
        loading = null;
        if (!disposed) changed();
        clearTimeout(refreshTimer); refreshTimer = null;
        if (!disposed && doc && phase !== 'denied' && (!mount || mount.document.visibilityState !== 'hidden')) {
          refreshTimer = setTimeout(() => {
            refreshTimer = null;
            if (!mount || mount.document.visibilityState !== 'hidden') void checkAccount();
          }, 30000);
        }
      }
    })();
    changed();
    return loading;
  }
  async function jget(url) {
    if (disposed || phase === 'denied') return failure('denied');
    if (phase === 'empty') return failure('empty', 'Save a computer address in your website workspace, then open it again.');
    if (phase !== 'ready' || !transport) return failure('unavailable', 'Check your account before connecting a computer.');
    const ticket = sequence, active = transport, recovery = new Map(clients.map(row => [row.key,row.actionRecoveryPending]));
    const value = await active.jget(url);
    if (disposed || phase !== 'ready' || ticket !== sequence || active !== transport) return failure('denied');
    // Quiet recorder recovery is observed by the existing board refresh, not
    // a source:ok event. Refresh only once after an admitted healthy overview.
    if (url === '/api/overview') for (const source of value?.data?.sources || []) {
      const row = clients.find(row => row.key === source.key);
      if (row?.actionRecoveryPending && row.actionRecoveryPending === recovery.get(row.key) && source.state === 'ok') {
        row.actionRecoveryPending = false; invoke(mount?.onActionSourceChange,row.key);
      }
    }
    return value;
  }
  function actionSource(room) {
    if (disposed || phase !== 'ready' || !current || typeof room !== 'string') return null;
    const route = transport?.routeRoom(room);
    const row = route && clients.find(row => row.key === route.key);
    return row ? {row,fleet:route.fleet} : null;
  }
  async function actionContext(room,kind) {
    const source = actionSource(room); if (!source) return null;
    const ticket = sequence;
    const value = await source.row.actions[kind](source.fleet);
    return disposed || phase !== 'ready' || ticket !== sequence ? null : value;
  }
  async function actionOperation(name,value) {
    const source = actionSource(value?.scope?.fleet);
    if (!source) throw new Error('Action outcome unknown. Check the original receipt.');
    const ticket = sequence, result = await source.row.actions[name](value);
    if (disposed || phase !== 'ready' || ticket !== sequence) throw new Error('Action outcome unknown. Check the original receipt.');
    return result;
  }
  function createEventSource(url) {
    if (url !== '/api/stream') throw new Error('Only the canonical stream is supported.');
    let child = null, closed = false; const handlers = new Map(), attached = new Set();
    const stream = {onmessage:null, onopen:null, onerror:null,
      addEventListener(name, callback) {
        if (!handlers.has(name)) handlers.set(name,new Set());
        handlers.get(name).add(callback); stream.bind(name);
      },
      removeEventListener(name, callback) { handlers.get(name)?.delete(callback); },
      attach() {
        if (closed || child || !transport) return;
        const active = transport, ticket = sequence;
        child = active.createEventSource(url); const source = child;
        const deliver = fn => event => {
          if (!closed && child === source && transport === active && sequence === ticket && phase === 'ready' && !disposed) invoke(fn, event);
        };
        for (const name of ['onmessage','onopen','onerror']) child[name] = deliver(event => stream[name]?.(event));
        for (const name of handlers.keys()) stream.bind(name);
      },
      bind(name) {
        if (!child || attached.has(name)) return;
        attached.add(name); const source = child, active = transport, ticket = sequence;
        child.addEventListener(name,event => {
          if (!closed && child === source && transport === active && sequence === ticket && phase === 'ready' && !disposed)
            for (const callback of handlers.get(name) || []) invoke(callback,event);
        });
      },
      detach() { child?.close(); child = null; attached.clear(); },
      close() { closed = true; stream.detach(); streams.delete(stream); }};
    streams.add(stream); stream.attach(); return stream;
  }
  async function act(index, operation) {
    if (disposed || phase !== 'ready' || !current) return false;
    const row = clients[index]; if (!row || pending.has(index) || !['connect','check','renew','logout','enroll'].includes(operation) || row.enrollment.snapshot().busy) return false;
    pending.add(index); changed();
    try {
      if (operation === 'enroll') {
        if (row.client.snapshot().state === 'ready' || !await checkAccount()) return false;
        return await row.enrollment.start();
      }
      const result = await row.client[operation]();
      if (operation === 'connect' && result) row.enrollment.cancel();
      return result;
    }
    catch { return false; }
    finally { pending.delete(index); if (!disposed) changed(); }
  }
  function render() {
    if (!mount) return;
    const {document, element} = mount; element.hidden = false;
    if (!mount.controls) {
      element.replaceChildren();
      const note = document.createElement('span'); note.setAttribute('role','status');
      note.setAttribute('aria-live','polite'); element.append(note);
      const check = document.createElement('button'); check.type = 'button'; check.className = 'pill ghost';
      check.textContent = 'Check account'; check.addEventListener('click', () => { if (!check.disabled && !loading) void checkAccount(); }); element.append(check);
      mount.controls = {note, check, rows:[]};
    }
    const controls = mount.controls;
    controls.note.textContent = phase === 'denied' ? 'Your account or saved computers changed. Close this view and open your workspace again.'
      : phase === 'empty' ? 'No computers saved yet. Add one in your workspace, then reopen this view.'
      : phase === 'checking' ? 'Checking your workspace…'
      : phase === 'unavailable' ? 'Account check unavailable. No computer has been connected.'
      : phase === 'disposed' ? 'Workspace closed.'
      : accountUnavailable ? 'Account check unavailable. Existing computer sessions remain independent.'
      : 'Connect each computer to view its teams.';
    controls.check.hidden = ['denied','disposed'].includes(phase);
    controls.check.disabled = controls.check.hidden;
    controls.check.setAttribute('aria-disabled', String(!!loading || controls.check.hidden));
    if (phase === 'ready' && !controls.rows.length) clients.forEach(({reference}, index) => {
      const row = document.createElement('span'); row.setAttribute('role','group'); row.tabIndex = -1;
      row.setAttribute('aria-label', new URL(reference.endpoint_origin).hostname);
      const label = document.createElement('span'); row.append(label);
      const buttons = [];
      for (const [operation, title] of [['connect','Connect'],['check','Check connection'],['renew','Keep connected'],['logout','Disconnect'],['enroll','Approve workspace on this computer']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'pill ghost'; button.textContent = title;
        button.setAttribute('aria-label', title + ' ' + new URL(reference.endpoint_origin).hostname);
        button.addEventListener('click', () => { if (button.isConnected && !button.disabled) void act(index,operation); });
        row.append(button); buttons.push({button,operation});
      }
      const detail = document.createElement('small'); row.append(detail);
      const proposal = document.createElement('span'); proposal.setAttribute('role','group');
      proposal.setAttribute('aria-label','Local workspace approval ' + new URL(reference.endpoint_origin).hostname);
      row.append(proposal); clients[index].enrollment.mountPanel({document,element:proposal,
        onForget:() => { if (phase === 'ready') {
          const button = buttons.find(({operation}) => operation === 'enroll').button;
          (button.disabled ? row : button).focus();
        } }}); element.append(row);
      controls.rows.push({row,label,detail,buttons});
    });
    controls.rows.forEach(({row,label,detail,buttons},index) => {
      row.hidden = phase !== 'ready'; const {reference,client,enrollment} = clients[index], status = client.snapshot();
      enrollment.refresh?.(); const approval = enrollment.snapshot();
      label.textContent = `${new URL(reference.endpoint_origin).hostname} · ${status.state}`;
      detail.textContent = status.remediation || '';
      for (const {button,operation} of buttons) button.disabled = row.hidden || pending.has(index)
        || ['checking','renewing','logout'].includes(status.state)
        || approval.busy
        || ['connect','enroll'].includes(operation) && status.state === 'ready'
        || operation === 'enroll' && ['awaiting_local_approval','unknown'].includes(approval.state)
        || operation === 'renew' && status.state !== 'ready'
        || operation === 'logout' && !['ready','unavailable'].includes(status.state);
    });
  }
  function mountSessionControls({document, element, onPause = () => {}, onResume = () => {}, onActionPause = () => {}, onActionSourceChange = () => {}}) {
    mount = {document,element,onPause,onResume,onActionPause,onActionSourceChange}; paused = true; invoke(onPause); changed();
    const unavailable = 'Unavailable in this connected read view. Activity and task records remain available.';
    const disable = () => {
      for (const button of document.querySelectorAll('#view-nav button, [data-bot-inspect]')) {
        if (button.dataset.view === 'channel' && !['denied','disposed'].includes(phase)) continue;
        button.disabled = true; button.title = unavailable;
      }
      const search = document.getElementById('search'); if (search) { search.disabled = true; search.title = unavailable; search.value = ''; }
    };
    disable();
    const observer = MutationObserver ? new MutationObserver(disable) : null;
    const roster = document.getElementById('fleet'); if (roster) observer?.observe(roster, {childList:true,subtree:true});
    const active = () => {
      clearTimeout(refreshTimer); refreshTimer = null;
      if (document.visibilityState !== 'hidden') void checkAccount();
    };
    document.addEventListener('visibilitychange',active);
    mount.cleanup = () => { document.removeEventListener('visibilitychange',active); observer?.disconnect(); }; void checkAccount();
    return {dispose};
  }
  function dispose() {
    if (disposed) return; disposed = true;
    mount?.cleanup?.(); retire('disposed');
    for (const stream of [...streams]) stream.close(); listeners.clear();
  }
  return Object.freeze({jget,roomLabel:room => transport?.roomLabel(room) || room,createEventSource,mountSessionControls,dispose,checkAccount,snapshot,act,
    interactionContext:room => actionContext(room,'interactionContext'),
    nudgeContext:room => actionContext(room,'nudgeContext'),feedbackContext:room => actionContext(room,'feedbackContext'),
    prepareAction:value => actionOperation('prepareAction',value),sendAction:value => actionOperation('sendAction',value),actionReceipt:value => actionOperation('actionReceipt',value),
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }});
}
