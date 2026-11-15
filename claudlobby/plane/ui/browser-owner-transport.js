// A Fetch facade for one browser-owned target. No host URL or header bag crosses
// the port; the parent and WASM independently admit the same closed operations.
const SHELL = 'https://claudfather.ai', PLANE = 'https://plane.claudfather.ai';
const encoder = new TextEncoder();
const keys = (value, fields) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...fields].sort().join();
const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value);
const secret = value => typeof value === 'string' && /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/.test(value);
const decimal = value => /^(0|[1-9][0-9]*)$/.test(value) && Number.isSafeInteger(Number(value));
const nonce = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const unavailable = () => new Error('Browser owner transport unavailable. Reopen this workspace; inspect the original receipt for uncertain operations.');
const fields = {
  pair:[], 'workspace-challenge':[], 'workspace-stage':['nonce','assertion'],
  'session-challenge':[], 'session-issue':['nonce','assertion'], 'session-status':[], 'session-renew':[], 'session-logout':[],
  'setup-context':[], 'setup-signin-begin':['generation','attempt_id'], 'setup-signin-status':['generation','attempt_id'],
  'setup-signin-cancel':['generation','attempt_id'], 'setup-signin-code':['generation','attempt_id','code'],
  'setup-preview':['generation','goal'], 'setup-launch':['generation','request_id','config_fingerprint','source_revision'],
  'setup-receipt':['generation','request_id','config_fingerprint','source_revision'],
  'read-summary':[], 'read-overview':[], 'read-fleets':[], 'read-trust':[], 'read-channel':['fleet','limit'],
  'read-tasks':['fleet','cursor'], 'read-task':['task_id','fleet'], 'read-identities':['fleet'], 'read-inventory':['fleet'],
  'read-org':['fleet'], 'read-utilization':['fleet'], 'read-presence':['fleet'], 'read-equipment':['alias'],
  'read-search':['q','fleet','limit'], 'read-grid':['fleet','focus'], 'read-stream':['cursor','once']
};
const publicOps = new Set(['pair','workspace-challenge','workspace-stage','session-challenge','session-issue']);
const sameReference = (a,b) => keys(a,['reference_id','endpoint_origin','revision']) && keys(b,['reference_id','endpoint_origin','revision'])
  && a.reference_id === b.reference_id && a.endpoint_origin === b.endpoint_origin && a.revision === b.revision;
function hostOrigin(value) {
  try { const url = new URL(value); return url.origin === value && url.protocol === 'https:' && !url.port
    && url.hostname.endsWith('.ts.net') && !url.username && !url.password; } catch { return false; }
}
function validInput(operation, input) {
  const allowed = fields[operation], read = operation.startsWith('read-');
  if (!allowed || !input || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).some(k => !allowed.includes(k)) || (!read && !keys(input,allowed))
    || operation === 'read-task' && !Object.hasOwn(input,'task_id')) return false;
  for (const [key,value] of Object.entries(input)) {
    if (typeof value !== 'string' || !value.isWellFormed() || value.includes('\0') || encoder.encode(value).length > 32768) return false;
    if (['generation','attempt_id','request_id'].includes(key) && !uuid(value)) return false;
    if (key === 'nonce' && !secret(value)) return false;
    if (key === 'assertion' && !/^[\x20-\x7e]{1,8192}$/.test(value)) return false;
    if (key === 'config_fingerprint' && !/^[a-f0-9]{64}$/.test(value)) return false;
    if (key === 'source_revision' && !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(value)) return false;
    if (key === 'goal' && [...value].length > 2000) return false;
    if (key === 'code' && ([...value].length < 1 || [...value].length > 2305)) return false;
    if (key === 'task_id' && !/^wi_[a-f0-9]{32}$/.test(value)) return false;
    if ((key === 'cursor' && operation === 'read-stream' || key === 'limit') && !decimal(value)) return false;
    if (key === 'once' && !['0','1'].includes(value)) return false;
    if (read && value.length > 8192) return false;
  }
  return true;
}
function translate(url, options, target) {
  if (Object.keys(options).some(k => !['method','credentials','cache','redirect','signal','headers','body'].includes(k))
    || options.credentials !== undefined && options.credentials !== 'omit'
    || options.redirect !== undefined && options.redirect !== 'error'
    || options.cache !== undefined && options.cache !== 'no-store') throw unavailable();
  const method = options.method || 'GET', headers = new Headers(options.headers);
  const allowedHeaders = ['authorization','content-type','x-claudlobby-owner','last-event-id'];
  if ([...headers.keys()].some(k => !allowedHeaders.includes(k))) throw unavailable();
  let operation, input = {};
  if (url.pathname === '/api/owner/bootstrap/pair') operation = 'pair';
  else if (/^\/api\/owner\/workspace\/(challenge|stage)$/.test(url.pathname)) operation = 'workspace-' + url.pathname.split('/').at(-1);
  else if (/^\/api\/owner\/connected\/(challenge|issue|status|renew|logout)$/.test(url.pathname)) operation = 'session-' + url.pathname.split('/').at(-1);
  else if (url.pathname.startsWith('/api/owner/connected/setup/')) operation = 'setup-' + url.pathname.slice('/api/owner/connected/setup/'.length);
  else if (url.pathname.startsWith('/api/owner/connected/read/')) {
    const suffix = url.pathname.slice('/api/owner/connected/read/'.length);
    if (/^tasks\/wi_[a-f0-9]{32}$/.test(suffix)) { operation = 'read-task'; input.task_id = suffix.slice(6); }
    else operation = 'read-' + suffix;
    const names = [...url.searchParams.keys()];
    if (names.length !== new Set(names).size || names.some(k => k === 'task_id')) throw unavailable();
    for (const [key,value] of url.searchParams) input[key] = value;
  }
  if (!Object.hasOwn(fields,operation)) throw unavailable();
  const get = operation.startsWith('read-') || operation === 'session-status';
  if (method !== (get ? 'GET' : 'POST') || (get && options.body !== undefined) || (!operation.startsWith('read-') && url.search)) throw unavailable();
  if (get) {
    if (headers.has('content-type') || headers.has('x-claudlobby-owner')) throw unavailable();
  } else {
    if (headers.get('content-type') !== 'application/json' || headers.get('x-claudlobby-owner') !== '1'
      || typeof options.body !== 'string' || encoder.encode(options.body).length > 32768) throw unavailable();
    input = JSON.parse(options.body);
  }
  if (!validInput(operation,input)) throw unavailable();
  const authorization = headers.get('authorization'), lastEventId = headers.get('last-event-id');
  const token = authorization?.startsWith('Bearer ') ? authorization.slice(7) : null;
  if (publicOps.has(operation) ? authorization !== null : !secret(token)) throw unavailable();
  if (lastEventId !== null && (operation !== 'read-stream' || !decimal(lastEventId))) throw unavailable();
  return {targetHandle:target.targetHandle,operation,input,...(token ? {token} : {}),...(lastEventId !== null ? {lastEventId} : {})};
}

export function createBrowserOwnerTransport({documentContext, window = globalThis.window,
  nativeFetch = globalThis.fetch.bind(globalThis), selection = ['browser-v1'], now = () => Date.now()/1000,
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout} = {}) {
  let doc = null, channelId = null, sessionId = null, target = null, port = null;
  let closed = false, outgoing = 0, incoming = 0, nextID = 0, offerTimer;
  const requests = new Map();
  let resolveReady, rejectReady;
  const ready = new Promise((resolve,reject) => {resolveReady=resolve;rejectReady=reject;});
  void ready.catch(() => {}); // Readiness may fail before the first host request.
  function post(type, fields = {}) {
    if (closed || !port || outgoing >= 1000000) throw unavailable();
    port.postMessage({version:1,channelId,sessionId,seq:++outgoing,type,...fields});
  }
  function finish(request) {
    clearTimeout(request.timer);request.signal?.removeEventListener('abort',request.abort);requests.delete(request.id);
    request.wait?.();request.wait=null;
  }
  function fail(request, cancel = false) {
    if (cancel && !closed) { try {post('cancel',{id:request.id});} catch {dispose();} }
    request.chunks=[];request.reject(unavailable());
    try {request.controller?.error(unavailable());} catch {}
    finish(request);
  }
  function dispose() {
    if (closed) return;
    try {if (port) post('retire');} catch {}
    closed=true;clearTimeout(offerTimer);
    window?.removeEventListener('message',offer);window?.removeEventListener('pagehide',dispose);
    rejectReady(unavailable());
    for (const request of [...requests.values()]) fail(request);
    if (port) {port.onmessage=null;port.onmessageerror=null;port.close();} port=null;target=null;
  }
  function consume(request, controller) {
    if (request.chunks.length) {
      controller.enqueue(request.chunks.shift());
      if (!request.ended) {request.credit++;post('pull',{id:request.id,credit:1});}
    }
    if (request.ended && !request.chunks.length) {controller.close();finish(request);}
  }
  function receive(event) {
    if (closed) return;
    try {
      const frame=event.data;
      if (!frame || encoder.encode(JSON.stringify(frame)).length > 65536 || frame.version !== 1
        || frame.channelId !== channelId || frame.sessionId !== sessionId || frame.seq !== incoming+1 || frame.seq > 1000000) throw unavailable();
      incoming=frame.seq;
      const envelope=['version','channelId','sessionId','seq','type'];
      if (frame.type === 'retire' && keys(frame,envelope)) {dispose();return;}
      const extras={headers:['id','status','headers'],chunk:['id','data'],end:['id'],error:['id','outcome']}[frame.type];
      if (!extras || !keys(frame,[...envelope,...extras]) || !Number.isSafeInteger(frame.id) || frame.id < 1 || frame.id > nextID) throw unavailable();
      const request=requests.get(frame.id);
      if (!request) return; // Issued and retired requests cannot be revived by late replies.
      if (frame.type === 'error') {
        if (!['unavailable','unknown','cancelled'].includes(frame.outcome)) throw unavailable();
        fail(request);return;
      }
      if (frame.type === 'headers') {
        if (request.controller || !Number.isInteger(frame.status) || frame.status < 200 || frame.status > 599
          || frame.status >= 300 && frame.status < 400 || !frame.headers || typeof frame.headers !== 'object' || Array.isArray(frame.headers)
          || encoder.encode(JSON.stringify(frame.headers)).length > 2048
          || Object.entries(frame.headers).some(([k,v]) => !['content-type','cache-control','retry-after'].includes(k)
            || typeof v !== 'string' || /[\r\n\0]/.test(v))) throw unavailable();
        if (request.stream) clearTimeout(request.timer);
        request.noBody=[204,205].includes(frame.status);
        const body=new ReadableStream({start(controller){request.controller=controller;},
          pull(controller) {
            if (request.chunks.length || request.ended) {consume(request,controller);return;}
            return new Promise(resolve => {request.wait=resolve;});
          },cancel(){if(requests.has(request.id)) fail(request,true);}}, {highWaterMark:0});
        request.resolve(new Response(request.noBody ? null : body,{status:frame.status,headers:frame.headers}));
        return;
      }
      if (!request.controller || request.ended) throw unavailable();
      if (frame.type === 'end') {
        request.ended=true;
        if (!request.chunks.length) {request.controller.close();finish(request);}
        else {request.wait?.();request.wait=null;}
        return;
      }
      if (request.noBody || typeof frame.data !== 'string' || frame.data.length > 21848 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(frame.data)) throw unavailable();
      const decoded=atob(frame.data);
      if (!decoded.length || decoded.length > 16384 || btoa(decoded) !== frame.data || request.credit < 1
        || request.chunks.length >= 4 || request.total+decoded.length > request.limit) throw unavailable();
      request.credit--;request.total+=decoded.length;request.chunks.push(Uint8Array.from(decoded,c=>c.charCodeAt(0)));
      if (request.wait) {consume(request,request.controller);request.wait?.();request.wait=null;}
    } catch {dispose();}
  }
  function offer(event) {
    if (closed || port || event.source !== window.parent || event.origin !== SHELL) return;
    const value=event.data;
    try { if (encoder.encode(JSON.stringify(value)).length > 65536) return; } catch { return; }
    if (!keys(value,['type','version','channelId','document','sessionId','reference','targetHandle'])
      || value.type !== 'claudfather.owner-transport.offer' || value.version !== 1 || value.channelId !== channelId
      || !nonce(value.sessionId) || JSON.stringify(value.document) !== JSON.stringify(doc)
      || !doc.references.some(ref=>sameReference(ref,value.reference)) || !hostOrigin(value.reference.endpoint_origin)
      || typeof value.targetHandle !== 'string' || value.targetHandle.length > 128 || !/^target-[a-f0-9]{32}-[1-9][0-9]*$/.test(value.targetHandle)
      || event.ports?.length !== 1) return;
    sessionId=value.sessionId;target={...value.reference,targetHandle:value.targetHandle};port=event.ports[0];
    port.onmessage=receive;port.onmessageerror=dispose;port.start();clearTimeout(offerTimer);
    try {post('ready');resolveReady();} catch {dispose();}
  }
  try {
    const d=documentContext;
    if (!keys(d,['version','user_id','workspace_id','context_generation','shell_origin','operational_origin','references','expires_at'])
      || encoder.encode(JSON.stringify(d)).length > 24576
      || d.version !== 1 || d.shell_origin !== SHELL || d.operational_origin !== PLANE || window?.location?.origin !== PLANE
      || !window.parent || window.parent === window || ![d.user_id,d.workspace_id,d.context_generation].every(uuid)
      || !Number.isSafeInteger(d.expires_at) || d.expires_at <= 0 || !Array.isArray(d.references) || d.references.length > 8
      || d.references.some(r=>!keys(r,['reference_id','endpoint_origin','revision']) || !uuid(r.reference_id)
        || !Number.isSafeInteger(r.revision) || r.revision < 1 || typeof r.endpoint_origin !== 'string')
      || new Set(d.references.map(r=>r.reference_id)).size !== d.references.length
      || new Set(d.references.map(r=>r.endpoint_origin)).size !== d.references.length
      || selection.length !== 1 || selection[0] !== 'browser-v1') throw unavailable();
    // The server already redeemed the one-use ticket before emitting this
    // document. Its exact expiry remains bound metadata, not a live workspace
    // lifetime; canonical account/context and native sessions own retirement.
    doc=JSON.parse(JSON.stringify({user_id:d.user_id,workspace_id:d.workspace_id,context_generation:d.context_generation,references:d.references,expires_at:d.expires_at}));
    channelId=Array.from(globalThis.crypto.getRandomValues(new Uint8Array(32)),b=>b.toString(16).padStart(2,'0')).join('');
    window.addEventListener('message',offer);window.addEventListener('pagehide',dispose);
    offerTimer=setTimeout(dispose,8000);
    window.parent.postMessage({type:'claudfather.owner-transport.request',version:1,channelId,document:JSON.parse(JSON.stringify(doc))},SHELL);
  } catch {dispose();}
  async function fetch(url, options = {}) {
    if (closed || !doc || typeof url !== 'string') {dispose();throw unavailable();}
    const parsed=new URL(url);
    if (parsed.hash || parsed.username || parsed.password || url !== parsed.origin+parsed.pathname+parsed.search) throw unavailable();
    if (parsed.origin === SHELL) {
      if (!['/api/plane/context','/api/plane/host-assertion','/api/plane/session-assertion'].includes(parsed.pathname)) throw unavailable();
      return nativeFetch(url,options);
    }
    await ready;
    if (closed || !target || parsed.origin !== target.endpoint_origin || options.signal?.aborted || requests.size >= 16 || nextID >= 65536) throw unavailable();
    const request=translate(parsed,options,target);
    if (encoder.encode(JSON.stringify(request)).length > 64000) throw unavailable();
    const id=++nextID;
    return new Promise((resolve,reject) => {
      const stream=request.operation === 'read-stream';
      const active={id,resolve,reject,stream,signal:options.signal,controller:null,chunks:[],credit:4,total:0,ended:false,
        limit:stream?Infinity:request.operation.startsWith('read-')?8*1024*1024:request.operation.startsWith('setup-')?131072:16384};
      active.abort=()=>{if(requests.has(id)) fail(active,true);};requests.set(id,active);
      active.signal?.addEventListener('abort',active.abort,{once:true});
      active.timer=setTimeout(()=>fail(active,true),request.operation === 'setup-launch'?45000:request.operation === 'pair'||request.operation.startsWith('workspace-')?35000:15000);
      try {post('request',{id,request,credit:4});} catch {dispose();}
    });
  }
  return Object.freeze({fetch,dispose,ready});
}
