// This page holds a one-time pairing challenge only in memory. Session cookies
// are HttpOnly; neither identity claims nor credentials come from browser input.
export function mountOwnerEntry({ document, fetch, now = () => Date.now(),
  setInterval = globalThis.setInterval, clearInterval = globalThis.clearInterval,
  setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout }) {
  const ids = ['status', 'pairing', 'namespace', 'subject', 'expires', 'challenge',
    'pair', 'check', 'login', 'open', 'logout'];
  const ui = Object.fromEntries(ids.map(id => [id, document.getElementById(`owner-${id}`)]));
  let state = 'checking';
  let challenge = null;
  let busy = false;
  let disposed = false;
  let request = null;
  const messages = {
    checking: 'Checking owner access…',
    needs_pairing: 'Local owner approval is needed. Start pairing to request approval for your verified identity.',
    awaiting_local_approval: 'Waiting for local approval. Check approval after confirming on this host.',
    sign_in_required: 'Sign in to open a browser session. Local owner approval is required before sign-in succeeds.',
    ready: 'Signed in. You can open this host’s Plane.',
    signed_out: 'Signed out of this browser session. Local owner pairing remains. Sign in when you want to return.',
    expired: 'The pairing code expired. Check status if you already approved it, or start pairing again.',
    denied: 'This request was refused. Check status or ask this host’s operator to verify your access.',
    unavailable: 'Owner access could not be checked. This does not mean you are unpaired. Retry Check status.',
  };
  function forgetChallenge() {
    challenge = null;
    for (const id of ['namespace', 'subject', 'expires', 'challenge']) ui[id].textContent = '';
  }
  function render() {
    ui.status.textContent = messages[state];
    ui.pairing.hidden = !challenge;
    ui.pair.hidden = !['needs_pairing', 'expired'].includes(state);
    ui.login.hidden = !['sign_in_required', 'signed_out'].includes(state);
    ui.logout.hidden = state !== 'ready';
    ui.open.hidden = state !== 'ready' || busy;
    ui.check.textContent = challenge ? 'Check approval' : 'Check status';
    for (const id of ['pair', 'check', 'login', 'logout']) ui[id].disabled = busy;
  }
  function expire() {
    if (challenge && challenge.expires_at * 1000 <= now()) {
      forgetChallenge();
      state = 'expired';
      render();
    }
  }
  function accept(action, data, status) {
    const allowed = action === 'pair' ? ['awaiting_local_approval']
      : action === 'logout' ? ['signed_out']
      : action === 'login' ? ['ready'] : ['needs_pairing', 'sign_in_required', 'ready'];
    if (status === 503 && data.state === 'unavailable') { state = 'unavailable'; return; }
    if (status === 403 && ['denied', 'sign_in_required'].includes(data.state)) {
      forgetChallenge(); state = data.state; return;
    }
    if (!allowed.includes(data.state) || status !== (action === 'pair' ? 202 : 200)) {
      throw new Error('Unexpected owner response');
    }
    if (action === 'pair') {
      if (typeof data.challenge !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(data.challenge)
        || typeof data.principal?.namespace !== 'string' || !data.principal.namespace
        || typeof data.principal?.subject !== 'string' || !data.principal.subject
        || !Number.isFinite(data.expires_at) || data.expires_at * 1000 <= now()) {
        throw new Error('Invalid pairing response');
      }
      forgetChallenge();
      challenge = { expires_at: data.expires_at };
      ui.namespace.textContent = data.principal.namespace;
      ui.subject.textContent = data.principal.subject;
      ui.challenge.textContent = data.challenge;
      ui.expires.textContent = new Date(data.expires_at * 1000).toLocaleString();
    } else if (data.state !== 'needs_pairing') {
      forgetChallenge();
    }
    // A pending challenge is not an approved owner grant. Status deliberately
    // reports needs_pairing until local confirmation; retain its instructions.
    state = data.state === 'needs_pairing' && challenge ? 'awaiting_local_approval' : data.state;
  }
  async function act(action) {
    if (busy || disposed) return;
    expire();
    busy = true;
    render();
    request = new AbortController();
    const timeout = setTimeout(() => request?.abort(), 8000);
    try {
      const response = await fetch(`/api/owner/${action}`, {
        method: action === 'status' ? 'GET' : 'POST',
        credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        signal: request.signal,
        ...(action === 'status' ? {} : {
          headers: { 'Content-Type': 'application/json', 'X-Claudlobby-Owner': '1' }, body: '{}',
        }),
      });
      const data = await response.json();
      if (disposed) return;
      accept(action, data, response.status);
    } catch {
      if (!disposed) state = 'unavailable';
    } finally {
      clearTimeout(timeout);
      request = null;
      busy = false;
      if (!disposed) { expire(); render(); }
    }
  }
  for (const [id, action] of [['pair', 'pair'], ['check', 'status'], ['login', 'login'], ['logout', 'logout']]) {
    ui[id].addEventListener('click', () => { void act(action); });
  }
  const timer = setInterval(expire, 1000);
  render();
  const ready = act('status');
  return {
    ready,
    dispose() {
      disposed = true;
      request?.abort();
      clearInterval(timer);
      forgetChallenge();
    },
  };
}

if (typeof document !== 'undefined') {
  const entry = mountOwnerEntry({ document, fetch: globalThis.fetch.bind(globalThis) });
  // Drop the displayed challenge before a page can enter the back/forward cache.
  globalThis.addEventListener('pagehide', () => entry.dispose(), { once: true });
  globalThis.addEventListener('pageshow', event => {
    if (event.persisted) globalThis.location.reload();
  });
}
