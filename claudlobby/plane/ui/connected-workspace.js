// Account metadata selects navigation; only host-issued core handles admit reads.
import { createConnectedReadClient } from "./connected-read-client.js";
import { createWorkspaceEnrollment } from "./workspace-enrollment.js";
import { createGuidedSetup } from "./guided-setup.js";
import { createWorkspaceReadTransport, MAX_SAVED_COMPUTERS, MAX_WORKSPACE_CONTEXT_BYTES } from "./two-source-read.js";

export { MAX_WORKSPACE_CONTEXT_BYTES };

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
  return Array.isArray(value) && value.length <= MAX_SAVED_COMPUTERS && value.every(ref => (keys(ref, ['reference_id', 'endpoint_origin', 'revision'])
      || keys(ref, ['reference_id', 'endpoint_origin', 'revision', 'transport']) && ref.transport === 'mac_openssh')
    && uuid(ref.reference_id) && origin(ref.endpoint_origin) && Number.isSafeInteger(ref.revision) && ref.revision > 0)
    && new Set(value.map(ref => ref.reference_id)).size === value.length
    && new Set(value.map(ref => ref.endpoint_origin)).size === value.length;
}
// Legacy references keep their historical key; an explicit transport is part of identity.
const referenceKey = refs => JSON.stringify(refs.map(ref => [ref.reference_id, ref.endpoint_origin, ref.revision,
  ...(ref.transport === undefined ? [] : [ref.transport])]).sort());
const computerLabel = reference => {
  const name = new URL(reference.endpoint_origin).hostname;
  const label = name.length <= 80 ? name : name.slice(0, 77) + '…';
  return reference.transport === 'mac_openssh' ? label + ' (SSH)' : label;
};
const failure = (state = 'unavailable', remediation = 'Open this workspace again to check its connection.') => ({state, provenance:null, remediation});

export function createConnectedWorkspace({ documentContext, documentOrigin = globalThis.location?.origin,
  fetch = globalThis.fetch.bind(globalThis), createClient = createConnectedReadClient,
  createTransport = createWorkspaceReadTransport, createEnrollment = createWorkspaceEnrollment, setTimeout = globalThis.setTimeout,
  clearTimeout = globalThis.clearTimeout, onRetire = () => {}, now = () => Date.now() / 1000,
  monotonicNow = () => globalThis.performance.now(), MutationObserver = globalThis.MutationObserver } = {}) {
  const browserTransport = fetch.browserOwnerTransport === true; // Presentation only; core remains the grant authority.
  const validDocument = keys(documentContext, ['version', 'user_id', 'workspace_id', 'context_generation', 'shell_origin', 'operational_origin', 'references', 'expires_at'])
    && documentContext.version === 1 && [documentContext.user_id, documentContext.workspace_id, documentContext.context_generation].every(uuid)
    && origin(documentContext.shell_origin) && origin(documentContext.operational_origin)
    && documentContext.operational_origin === documentOrigin && documentContext.shell_origin !== documentOrigin
    && references(documentContext.references) && Number.isSafeInteger(documentContext.expires_at) && documentContext.expires_at > 0
    // An SSH computer has no HTTPS door: without the browser carrier it is refused, never fetched.
    && (browserTransport || documentContext.references.every(ref => ref.transport === undefined));
  // Copy before asynchronous work; callers cannot retarget a live document.
  const doc = validDocument ? JSON.parse(JSON.stringify(documentContext)) : null;
  let current = null, transport = null, clients = [], unsubscribes = [], mount = null;
  let phase = doc ? 'checking' : 'denied', accountUnavailable = false, disposed = false;
  let sequence = 0, loading = null, refreshTimer = null, paused = true;
  const requests = new Set(), streams = new Set(), listeners = new Set(), pending = new Map();
  // Presentation only: the browser carrier serves the one saved reference its parent bound, so only
  // that computer is shown. Before the offer, or for a carrier that never bound, every computer is shown.
  function boundReference() {
    if (!browserTransport || !doc) return null;
    let id; try { id = fetch.browserOwnerReference; } catch { return null; }
    return doc.references.some(ref => ref.reference_id === id) ? id : null;
  }
  const shown = reference => { const id = boundReference(); return id === null || id === reference.reference_id; };
  // An offer admitted after the account check repaints the computers and asks the boards to read again.
  let bridgeSettled = false;
  const settledMarker = browserTransport ? fetch.browserOwnerSettled : null;
  if (doc && typeof settledMarker?.then === 'function') Promise.resolve(settledMarker).then(() => {
    if (disposed) return;
    bridgeSettled = true;
    if (boundReference() !== null) transport?.presentationChanged?.();
    changed();
  }, () => {});
  // The browser carrier's frame is the body of the website's setup step: a setup-only view of the bound
  // computer (workspace.css hides the rest). Its line says, in plain sentences, why no panel shows yet;
  // [text, working]: a line that waits on a check turns a ring beside it.
  function setupOnlyNote() {
    return phase === 'checking' ? ['Checking your workspace…',true]
      : phase === 'unavailable' ? ['Your account could not be checked yet. This page tries again shortly.']
      : phase === 'empty' ? ['No computer is saved in your workspace yet.']
      : phase === 'denied' ? ['Your account or saved computers changed. Reopen setup from your workspace.']
      : bridgeSettled ? ['This page could not reach your computer. Reopen setup from your workspace.']
      : ['Opening the connection to your computer…',true];
  }
  // The Connect step's hint in the setup-only view: what happened and what to choose, without builder detail.
  function connectHint(status, connection, continuity, expired) {
    if (connection === 'not_connected' || ['ready','setup_only','checking','renewing','logout'].includes(status.state)) return '';
    if (expired) return 'Your connection to this computer expired. Choose Connect to continue.';
    if (continuity?.connectRequired) return 'Could not connect to this computer. Make sure it is on and online, then choose Connect again.';
    if (connection === 'denied') return 'This computer did not accept the connection. Make sure this workspace is approved on it, then choose Connect again.';
    if (['disconnected','signed_out'].includes(connection)) return 'Disconnected from this computer. Choose Connect to continue.';
    return 'The connection to this computer could not be confirmed. Choose Check connection to look again.';
  }
  const snapshot = () => Object.freeze({phase, accountUnavailable,
    computers: clients.map(({reference, client, label}) => ({reference_id:reference.reference_id,
      endpoint_origin:reference.endpoint_origin, ...(reference.transport === undefined ? {} : {transport:reference.transport}),
      label:label || computerLabel(reference), ...client.snapshot()}))});
  const invoke = (fn, ...args) => { try { fn?.(...args); } catch { /* consumer callbacks cannot change admission */ } };
  function changed() {
    render();
    for (const listener of listeners) invoke(listener, snapshot());
  }
  function retire(next) {
    phase = next; current = null; sequence++;
    invoke(onRetire);
    clearTimeout(refreshTimer); refreshTimer = null;
    for (const request of requests) request.abort();
    for (const stop of unsubscribes) stop(); unsubscribes = [];
    for (const {client,enrollment,setup} of clients) { setup?.dispose(); enrollment.dispose(); client.dispose(); }
    transport?.dispose(); transport = null;
    for (const stream of streams) stream.detach();
    paused = true; invoke(mount?.onPause);
    if (mount) {
      // A changed account cannot leave private records in a paused old document.
      mount.element.replaceChildren(); mount.controls = null;
      // render below hides the emptied host and leaves the main area as it was; a setup-only view keeps its status line
      mount.setupHost?.replaceChildren(...(mount.setupStatus ? [mount.setupStatus] : []));
      mount.document.querySelector('main')?.replaceChildren(mount.element);
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
        length += part.value.byteLength; if (length > MAX_WORKSPACE_CONTEXT_BYTES) throw new Error(); chunks.push(part.value);
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
    const names = data?.computer_names;
    return keys(data, ['version','user','workspace','context_generation','references',...(names === undefined ? [] : ['computer_names'])]) && data.version === 2
      && keys(data.user, ['id']) && data.user.id === doc.user_id
      && keys(data.workspace, ['id','name']) && data.workspace.id === doc.workspace_id
      && typeof data.workspace.name === 'string' && data.workspace.name.length > 0 && data.workspace.name.length <= 80
      && data.context_generation === doc.context_generation && references(data.references)
      && referenceKey(data.references) === referenceKey(doc.references)
      && (names === undefined || Array.isArray(names) && names.length === data.references.length
        && new Set(names.map(row => row?.reference_id)).size === names.length
        && names.every(row => keys(row, ['reference_id','name'])
          && data.references.some(ref => ref.reference_id === row.reference_id)
          && typeof row.name === 'string' && row.name.trim() && row.name.length <= 80
          && row.name.isWellFormed() && !row.name.includes('\0')));
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
    // Names are presentation only; the carrier receives the identity-only DTO.
    const {computer_names:names, ...identity} = data;
    const context = Object.freeze({...identity, user:Object.freeze({...data.user}), workspace:Object.freeze({...data.workspace}),
      references:Object.freeze(data.references.map(ref => Object.freeze({...ref})))});
    if (clients.length || !doc.references.length) { current = context; return false; }
    const nextClients = [], nextSubscriptions = [];
    let nextTransport = null;
    try {
      for (const reference of doc.references) {
        const row = {reference,label:names?.find(row => row.reference_id === reference.reference_id)?.name,
        key:'c-' + BigInt('0x' + reference.reference_id.replaceAll('-','')).toString(36), client:createClient({reference,
        documentContext:{issuer:doc.shell_origin, operational_origin:doc.operational_origin, subject:doc.user_id,
          workspace_id:doc.workspace_id, context_generation:doc.context_generation},
        currentContext:() => current, obtainSessionAssertion:(input,options) => accountAssertion('/api/plane/session-assertion',input,options),
        onContinuityChange:changed, monotonicNow, documentOrigin, fetch, setTimeout, clearTimeout})};
        nextClients.push(row);
        row.client.activity?.({visible:mount?.document.visibilityState === 'visible'});
        row.setup = createGuidedSetup({client:row.client,setTimeout,clearTimeout,
          visible:() => !mount || mount.document.visibilityState !== 'hidden',
          storageKey:`claudlobby:first-team:${doc.user_id}:${doc.workspace_id}:${doc.context_generation}:${reference.reference_id}:${reference.revision}`});
        row.actions = row.client.createActions({key:row.key,
          onPause:(...args) => invoke(mount?.onActionPause,...args),
          onSourceChange:() => { row.actionRecoveryPending = {}; invoke(mount?.onActionSourceChange,row.key); }});
        row.enrollment = createEnrollment({reference, issuer:doc.shell_origin, operationalOrigin:doc.operational_origin,
          currentContext:() => current, obtainAssertion:(input,options) => accountAssertion('/api/plane/host-assertion',input,options),
          fetch,now,setTimeout,clearTimeout,onChange:changed});
      }
      nextTransport = createTransport({sources:nextClients.map(({client,key,reference,label}) => ({key,
        label:computerLabel(reference), savedLabel:label, reader:client.readHandle, shown:() => shown(reference),
        connectionState:() => client.connectionState?.() || client.snapshot().state,
        readFailure:url => client.readFailure?.(url)}))});
      for (const {client,actions,key,setup} of nextClients) {
        let previous = client.snapshot();
        nextSubscriptions.push(client.subscribe(state => {
          setup.connectionChanged(state);
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
      for (const {client,enrollment,setup} of nextClients) {
        invoke(() => setup?.dispose());
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
        const result = await jsonRequest(doc.shell_origin + '/api/plane/context?workspace_id=' + encodeURIComponent(doc.workspace_id) + '&include=computer_names', {method:'GET'});
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
  async function jget(url, onUpdate) {
    if (disposed || phase === 'denied') return failure('denied');
    if (phase === 'empty') return failure('empty', 'Save a computer address in your website workspace, then open it again.');
    if (phase !== 'ready' || !transport) return failure('unavailable', 'Check your account before connecting a computer.');
    const ticket = sequence, active = transport, recovery = new Map(clients.map(row => [row.key,row.actionRecoveryPending]));
    const value = await active.jget(url, typeof onUpdate === 'function' ? value => {
      if (!disposed && phase === 'ready' && ticket === sequence && active === transport) onUpdate(value);
    } : undefined);
    if (disposed || phase !== 'ready' || ticket !== sequence || active !== transport) return failure('denied');
    let renamed = false;
    for (const source of value?.data?.sources || []) {
      const row = clients.find(row => row.key === source.key);
      if (row && source.state === 'ok' && typeof source.label === 'string'
          && source.label.trim() && source.label.length <= 80 && row.label !== source.label) {
        row.label = source.label; renamed = true;
      }
    }
    if (renamed) changed();
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
  async function actionContext(room,kind,task) {
    const source = actionSource(room); if (!source) return null;
    const ticket = sequence;
    try {
      const value = await source.row.actions[kind](source.fleet,task);
      return disposed || phase !== 'ready' || ticket !== sequence ? null : value;
    } catch (error) {
      if (disposed || phase !== 'ready' || ticket !== sequence) return null;
      throw error;
    }
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
    pending.set(index, operation); changed();
    try {
      if (operation === 'enroll') {
        if (browserTransport) return false;
        if (['ready','setup_only'].includes(row.client.snapshot().state) || !await checkAccount()) return false;
        return await row.enrollment.start();
      }
      const result = await row.client[operation]();
      if (operation === 'connect' && result) {
        row.enrollment.cancel();
        transport?.retryOverview?.(row.key);
      }
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
      element.className = 'owner-session connected-session';
      const heading = document.createElement('div'); heading.className = 'connection-heading'; element.append(heading);
      const note = document.createElement('span'); note.setAttribute('role','status');
      note.setAttribute('aria-live','polite'); heading.append(note);
      const check = document.createElement('button'); check.type = 'button'; check.className = 'pill ghost';
      check.textContent = 'Check account'; check.addEventListener('click', () => { if (!check.disabled && !loading) void checkAccount(); }); heading.append(check);
      mount.controls = {note, check, rows:[]};
    }
    const controls = mount.controls, bound = boundReference();
    controls.note.textContent = phase === 'denied' ? 'Your account or saved computers changed. Close this view and open your workspace again.'
      : phase === 'empty' ? 'No computers saved yet. Add one in your workspace, then reopen this view.'
      : phase === 'checking' ? 'Checking your workspace…'
      : phase === 'unavailable' ? 'Account check unavailable. No computer has been connected.'
      : phase === 'disposed' ? 'Workspace closed.'
      : accountUnavailable ? 'Account check unavailable. Existing computer sessions remain independent.'
      : bound ? 'Connect this computer to view its teams. Active use keeps the connection alive. Unsent drafts stay in this open view.'
      : 'Connect each computer to view its teams. Active use keeps connections alive. Unsent drafts stay in this open view.';
    controls.check.hidden = ['denied','disposed'].includes(phase);
    controls.check.disabled = controls.check.hidden;
    controls.check.setAttribute('aria-disabled', String(!!loading || controls.check.hidden));
    if (phase === 'ready' && !controls.rows.length) clients.forEach(({reference}, index) => {
      const row = document.createElement('div'); row.className = 'connection-card'; row.setAttribute('role','group'); row.tabIndex = -1;
      row.setAttribute('aria-label', new URL(reference.endpoint_origin).hostname);
      const label = document.createElement('strong'); label.className = 'connection-name'; row.append(label);
      const actions = document.createElement('div'); actions.className = 'connection-actions'; row.append(actions);
      const primary = document.createElement('button'); primary.type = 'button'; primary.className = 'pill connection-primary';
      primary.addEventListener('click', () => { if (primary.isConnected && !primary.disabled) void act(index,primary.dataset.operation); });
      const goto = document.createElement('button'); goto.type = 'button'; goto.className = 'pill connection-goto'; goto.textContent = 'Go to setup';
      goto.setAttribute('aria-label','Go to setup for ' + new URL(reference.endpoint_origin).hostname); goto.hidden = true;
      actions.append(primary,goto);
      const detail = document.createElement('p'); detail.className = 'connection-detail'; row.append(detail);
      const help = document.createElement('details'); help.className = 'connection-approval';
      const summary = document.createElement('summary'); summary.textContent = 'Connection options and first-time setup'; help.append(summary); row.append(help);
      const steps = document.createElement('p');
      steps.textContent = browserTransport ? 'Use your workspace’s guided setup to approve this computer and workspace, then finish first-team setup in the setup panel. After the original setup finishes, return to the guide for optional message approval; setup alone does not allow messages.' : 'Already installed? Open computer access first if this computer has not been paired. Prepare local approval for this workspace, confirm its code in the computer’s local terminal, then choose Connect. These steps do not install or restart a team.';
      help.append(steps);
      const access = document.createElement('a'); access.href = reference.endpoint_origin + '/owner';
      access.target = '_blank'; access.rel = 'noopener noreferrer'; access.textContent = 'Set up computer access ↗';
      access.setAttribute('aria-label','Set up access on ' + new URL(reference.endpoint_origin).hostname + ' (opens in a new tab)');
      if (reference.transport === undefined) help.append(access); // An SSH computer has no HTTPS page to open.
      const options = document.createElement('div'); options.className = 'connection-actions'; help.append(options);
      const buttons = [];
      for (const [operation, title] of [['connect','Connect'],['check','Check connection'],['renew','Keep connected'],['logout','Disconnect'],...(!browserTransport ? [['enroll','Prepare local approval']] : [])]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'pill ghost'; button.textContent = title;
        button.dataset.operation = operation;
        button.setAttribute('aria-label', title + ' ' + new URL(reference.endpoint_origin).hostname);
        button.addEventListener('click', () => { if (button.isConnected && !button.disabled) void act(index,operation); });
        options.append(button); buttons.push({button,operation});
      }
      const proposal = document.createElement('div'); proposal.setAttribute('role','group');
      proposal.className = 'connection-proposal';
      proposal.setAttribute('aria-label','Local workspace approval ' + new URL(reference.endpoint_origin).hostname);
      const setupPanel = document.createElement('div'); setupPanel.className = 'setup-focus-panel'; setupPanel.setAttribute('role','group');
      setupPanel.setAttribute('aria-label','First-team setup ' + new URL(reference.endpoint_origin).hostname);
      const setupEyebrow = document.createElement('p'); setupEyebrow.className = 'eyebrow setup-focus-eyebrow'; setupEyebrow.textContent = 'First-team setup';
      const setupHeading = document.createElement('h2'); setupHeading.className = 'setup-focus-title'; setupHeading.tabIndex = -1;
      const setupBody = document.createElement('div'); setupPanel.append(setupEyebrow,setupHeading,setupBody); help.append(setupPanel);
      // In the browser carrier the panel is the whole view, and its first step is this computer's explicit Connect.
      clients[index].setup.mountPanel({document,element:setupBody,...(browserTransport ? {standalone:true,connect:() => {
        const operation = mount?.controls?.rows[index]?.panelOperation;
        if (operation) void act(index,operation);
      }} : {})});
      goto.addEventListener('click', () => {
        if (goto.hidden || !goto.isConnected) return;
        setupPanel.scrollIntoView?.({block:'start'}); setupHeading.focus?.({preventScroll:true});
      });
      if (!browserTransport) {help.append(proposal); clients[index].enrollment.mountPanel({document,element:proposal,
        onForget:() => { if (phase === 'ready') {
          const button = buttons.find(({operation}) => operation === 'enroll').button;
          (button.disabled ? row : button).focus();
        } }}); } element.append(row);
      controls.rows.push({row,label,detail,primary,goto,buttons,help,access,setupPanel,setupHeading,focused:false,approvalState:'idle',setupOnly:false,attentionState:null});
    });
    controls.rows.forEach((controls,index) => {
      const {row,label,detail,primary,goto,buttons,help,access,setupPanel,setupHeading} = controls;
      const {reference,client,enrollment} = clients[index], status = client.snapshot();
      row.hidden = phase !== 'ready' || !shown(reference); // a computer the browser carrier cannot reach is not listed
      const continuity = client.continuity?.();
      enrollment.refresh?.(); const approval = enrollment.snapshot();
      const connection = client.connectionState?.() || status.state;
      const expired = status.state === 'unavailable' && continuity?.reconnectRequired;
      const name = clients[index].label || computerLabel(reference);
      // In the browser carrier the bridged computer's panel is the view in every state, so Connect is its first step.
      const bridged = bound === reference.reference_id;
      const focused = phase === 'ready' && !!mount.setupHost && !row.hidden
        && (browserTransport ? bridged : status.state === 'setup_only');
      if (focused !== controls.focused) { (focused ? mount.setupHost : help).append(setupPanel); controls.focused = focused; }
      setupHeading.textContent = focused ? `Finish setting up ${name}` : 'First-team setup';
      goto.hidden = !focused;
      const labels = {ready:'Connected',setup_only:'First-team setup',not_connected:'Not connected',denied:'Access denied',disconnected:'Disconnected',signed_out:'Disconnected',checking:'Checking connection',unavailable:'Connection unavailable',renewing:'Keeping connected',logout:'Disconnecting'};
      const pendingLabel = {connect:'Connecting',check:'Checking connection'}[pending.get(index)];
      label.textContent = `${name} · ${pendingLabel || (expired ? 'Session expired' : labels[connection] || 'Connection unconfirmed')}`;
      label.title = new URL(reference.endpoint_origin).hostname;
      row.setAttribute('aria-label', name + ' — ' + new URL(reference.endpoint_origin).hostname);
      const toPanel = 'Finish setting up this computer in the setup panel.';
      detail.textContent = continuity?.renewalUnknown && status.state === 'unavailable'
        ? 'Connection renewal was not confirmed. Choose Check connection; if it expired, choose Connect again. Nothing will retry automatically.'
        : continuity?.warning && status.state === 'ready'
        ? 'This connection expires soon. Use Keep connected to continue; active use renews it automatically.'
        : status.state === 'setup_only' ? focused ? toPanel
          : 'Choose Check setup approval below to continue setting up your first team.'
        : connection === 'not_connected' ? focused ? toPanel : 'Choose Connect to read this computer. Its teams and bot states have not been checked.'
        : status.remediation || (status.state === 'denied'
        ? 'Choose Connect if this workspace is already approved. First time here? Set up computer access, then approve this workspace.'
        : '');
      if (['awaiting_local_approval','unknown'].includes(approval.state))
        detail.textContent = approval.state === 'unknown'
          ? 'Local approval may have been prepared. Inspect the retained code on this computer; do not prepare it again. Connect only after local approval.'
          : browserTransport ? 'Return to your workspace’s guided setup to inspect the retained approval, then check this connection.' : 'Waiting for local approval on this computer. Confirm the retained code in its terminal, then choose Connect.';
      access.hidden = browserTransport || ['ready','setup_only'].includes(status.state);
      // Reveal sidebar setup on entry; later paints preserve the chosen disclosure state.
      if (status.state === 'setup_only' && !controls.setupOnly && !focused
          || approval.state !== 'idle' && approval.state !== controls.approvalState) help.open = true;
      controls.approvalState = approval.state;
      controls.setupOnly = status.state === 'setup_only';
      const primaryOperation = expired || continuity?.connectRequired ? 'connect' : status.state === 'unavailable' ? 'check'
        : ['ready','setup_only','logout'].includes(status.state) ? 'logout'
        : status.state === 'renewing' ? 'renew' : 'connect';
      const primaryTitle = {connect:'Connect',check:'Check connection',logout:'Disconnect',renew:'Keeping connected'}[primaryOperation];
      primary.dataset.operation = primaryOperation; primary.textContent = primaryTitle;
      primary.classList.toggle('connection-primary-quiet', focused); // Go to setup leads instead
      primary.setAttribute('aria-label', primaryTitle + ' ' + new URL(reference.endpoint_origin).hostname);
      const disabled = operation => row.hidden || pending.has(index)
        || ['checking','renewing','logout'].includes(status.state)
        || approval.busy
        || ['connect','enroll'].includes(operation) && ['ready','setup_only'].includes(status.state)
        || operation === 'enroll' && ['awaiting_local_approval','unknown'].includes(approval.state)
        || operation === 'renew' && !['ready','setup_only'].includes(status.state)
        || operation === 'logout' && !['ready','setup_only','unavailable'].includes(status.state);
      primary.disabled = disabled(primaryOperation);
      for (const {button,operation} of buttons) {
        button.hidden = operation === primaryOperation
          || ['renew','logout'].includes(operation) && !['ready','setup_only','unavailable','renewing','logout'].includes(status.state);
        button.disabled = disabled(operation);
      }
      if (browserTransport) {
        // The panel's Connect step mirrors this card's own Connect or Check connection; it adds no other operation.
        const operation = primaryOperation === 'check' ? 'check' : 'connect';
        controls.panelOperation = operation;
        clients[index].setup.showConnect?.({operation, disabled:disabled(operation),
          busy:pending.get(index) || (status.state === 'checking' ? 'check' : null),
          hint:connectHint(status, connection, continuity, expired)});
      }
      // A computer that is not listed does not ask for attention.
      if (!row.hidden && connection === 'ready') controls.attentionState = null;
      else if (!row.hidden && ['setup_only','not_connected','denied','unavailable'].includes(connection)
          && controls.attentionState !== connection) {
        controls.attentionState = connection;
        invoke(mount.onConnectionAttention,{source:clients[index].key,state:connection});
      }
    });
    if (mount.setupHost) {
      const focusing = controls.rows.some(row => row.focused), rail = mount.setupHost.parentNode;
      if (mount.setupStatus) {
        // Setup-only view: the host is the whole main area, with a plain line until the panel shows.
        // Retirement keeps the line in its host; a closed view writes nothing more.
        if (phase !== 'disposed') {
          const [text,working] = focusing ? [''] : setupOnlyNote(); mount.setupStatus.hidden = focusing;
          if (mount.setupStatus.textContent !== text) mount.setupStatus.textContent = text; // no repeated announcement
          if (working) mount.setupStatus.dataset.tone = 'working'; else delete mount.setupStatus.dataset.tone;
        }
        mount.setupHost.hidden = false; if (rail) rail.dataset.setupFocus = 'only';
      } else {
        mount.setupHost.hidden = !focusing;
        // Only setup to show: the panel replaces the main area; beside a readable computer it sits above its teams.
        if (focusing) rail.dataset.setupFocus = clients.some(({client,reference}) => shown(reference) && client.snapshot().state === 'ready') ? 'shared' : 'only';
        else delete rail.dataset.setupFocus;
      }
    }
  }
  function mountSessionControls({document, element, onPause = () => {}, onResume = () => {}, onActionPause = () => {}, onActionSourceChange = () => {}, onConnectionAttention = () => {}}) {
    // While a computer is setup-only, its first-team panel takes the main area instead of the sidebar card.
    const rail = document.getElementById('rail-channel'), setupHost = rail ? document.createElement('section') : null;
    if (setupHost) { setupHost.id = 'setup-focus'; setupHost.className = 'setup-focus'; setupHost.hidden = true; rail.prepend(setupHost); }
    // The browser carrier's frame is a setup-only view: workspace.css shows only #setup-focus under this marker.
    let setupStatus = null;
    if (browserTransport) {
      invoke(() => document.documentElement?.setAttribute('data-workspace-view','setup'));
      if (setupHost) { setupStatus = document.createElement('p'); setupStatus.className = 'setup-only-status';
        setupStatus.setAttribute('role','status'); setupStatus.setAttribute('aria-live','polite'); setupHost.prepend(setupStatus); }
    }
    mount = {document,element,setupHost,setupStatus,onPause,onResume,onActionPause,onActionSourceChange,onConnectionAttention}; paused = true; invoke(onPause); changed();
    const unavailable = 'Unavailable in this connected read view. Activity and task records remain available.';
    const tools = document.getElementById('view-nav')?.querySelector?.('details');
    if (tools) {
      const explanation = document.createElement('p'); explanation.className = 'note';
      explanation.textContent = 'Team details, Recorder and activity search are unavailable in this connected view. Use Rooms, Tasks or All agents.';
      tools.append(explanation);
    }
    const disable = () => {
      for (const button of document.querySelectorAll('#view-nav button, [data-bot-inspect]')) {
        if ((['channel','tasks','grid'].includes(button.dataset.view)
            || button.dataset.botInspect && button.dataset.botInspection === 'presence')
            && !['denied','disposed'].includes(phase)) {
          button.disabled = false; button.title = ''; continue;
        }
        button.disabled = true; button.title = unavailable;
      }
      const search = document.getElementById('search'); if (search) { search.disabled = true; search.title = unavailable; search.value = ''; }
    };
    disable();
    const observer = MutationObserver ? new MutationObserver(disable) : null;
    const roster = document.getElementById('fleet'); if (roster) observer?.observe(roster, {childList:true,subtree:true});
    const channel = document.getElementById('channel'); if (channel) observer?.observe(channel, {childList:true,subtree:true});
    const active = () => {
      clearTimeout(refreshTimer); refreshTimer = null;
      for (const {client,setup} of clients) {
        client.activity?.({visible:document.visibilityState === 'visible'});
        if (document.visibilityState !== 'hidden') setup.resumeHandoff();
      }
      if (document.visibilityState !== 'hidden') void checkAccount();
    };
    let lastReadingInput = null;
    const activity = event => {
      if (!event.isTrusted) return;
      if (['wheel','touchmove'].includes(event.type)) {
        const time = monotonicNow();
        if (lastReadingInput !== null && time - lastReadingInput < 1000) return;
        lastReadingInput = time;
      }
      for (const {client} of clients)
        client.activity?.({visible:document.visibilityState === 'visible',userInput:true});
    };
    document.addEventListener('visibilitychange',active);
    const inputs = ['pointerdown','keydown','input','wheel','touchmove'];
    for (const name of inputs) document.addEventListener(name,activity,true);
    mount.cleanup = () => {
      document.removeEventListener('visibilitychange',active);
      for (const name of inputs) document.removeEventListener(name,activity,true);
      observer?.disconnect();
    }; void checkAccount();
    // Acknowledge the rendered interface, independently of host connection or
    // account availability. No account, host, draft or action metadata crosses
    // this boundary; the shell checks the exact origin and frame source.
    const view = document.defaultView;
    if (doc && !disposed && view && view.parent !== view) {
      invoke(() => view.parent.postMessage({type:'claudfather.workspace.ready',version:1}, doc.shell_origin));
      if (browserTransport) reportSize(document, view);
    }
    return {dispose};
  }
  // Setup-only view: the parent sizes the frame to this content. Only the height crosses, to the exact shell
  // origin, and only when it changed. A ResizeObserver on the root element reports at most once per rendering frame.
  function reportSize(document, view) {
    const page = document.documentElement;
    if (!page || typeof view.ResizeObserver !== 'function') return;
    let posted = null, observer = null;
    const measure = () => {
      if (disposed) return;
      let height; try { height = Math.ceil(page.getBoundingClientRect().height); } catch { return; }
      if (!Number.isFinite(height)) return;
      height = Math.min(20000, Math.max(1, height));
      if (height === posted) return;
      posted = height;
      invoke(() => view.parent.postMessage({type:'claudfather.workspace.size',version:1,height}, doc.shell_origin));
    };
    try { observer = new view.ResizeObserver(measure); observer.observe(page); } catch { observer?.disconnect(); return; }
    const previous = mount.cleanup;
    mount.cleanup = () => { previous?.(); observer.disconnect(); };
  }
  function dispose() {
    if (disposed) return; disposed = true;
    mount?.cleanup?.(); retire('disposed');
    for (const stream of [...streams]) stream.close(); listeners.clear();
  }
  return Object.freeze({jget,retryOverview:key => !disposed && phase === 'ready' && !!current && (transport?.retryOverview?.(key) || false),roomLabel:room => transport?.roomLabel(room) || room,createEventSource,mountSessionControls,dispose,checkAccount,snapshot,act,
    interactionContext:room => actionContext(room,'interactionContext'),
    nudgeContext:room => actionContext(room,'nudgeContext'),feedbackContext:room => actionContext(room,'feedbackContext'),
    permissionContext:(room,task) => actionContext(room,'permissionContext',task),
    prepareAction:value => actionOperation('prepareAction',value),sendAction:value => actionOperation('sendAction',value),actionReceipt:value => actionOperation('actionReceipt',value),
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }});
}
