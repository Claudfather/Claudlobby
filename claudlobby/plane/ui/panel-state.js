// panel-state.js — the §16 panel-state machinery, built FIRST because the ten
// non-happy states are the framework every surface hangs from. The server
// decides absent/unreadable (the envelope's `state`); this module owns the
// client-side ones (loading, daemon-disconnected, stale) and the LAW that a
// panel never renders zero when its source is absent — an empty list renders
// only when the server said ok.

export const esc = (s) => (s ?? "").toString()
  .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                                '"': "&quot;" }[c]));

export function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (Number.isNaN(s)) return iso;
  if (s < 60) return `${Math.max(0, s | 0)}s ago`;
  if (s < 3600) return `${(s / 60) | 0}m ago`;
  if (s < 86400) return `${(s / 3600) | 0}h ago`;
  return `${(s / 86400) | 0}d ago`;
}

const STATE_COPY = {
  loading: { label: "loading…", detail: "first load in progress" },
  absent: { label: "no recorder data yet", detail: "" },
  unreadable: { label: "source unreadable", detail: "" },
  disconnected: { label: "view daemon unreachable",
                  detail: "the page cannot reach its own API — the fleet may"
                          + " be fine; this is the window, not the world" },
  idle: { label: "quiet", detail: "the source is healthy and has nothing"
                                  + " matching — legitimately idle" },
};

// Render a non-ok state INTO the panel. Returns true when it rendered a
// state (caller must not render data); false when envelope.state === "ok".
export function renderState(el, envelope, { idleWhenEmpty = null } = {}) {
  if (envelope && envelope.state === "ok") {
    const empty = idleWhenEmpty !== null && idleWhenEmpty(envelope.data);
    if (!empty) return false;
    el.innerHTML = stateBlock("idle", envelope.provenance, null);
    return true;
  }
  const st = envelope ? envelope.state : "disconnected";
  el.innerHTML = stateBlock(st, envelope && envelope.provenance,
                            envelope && envelope.remediation);
  return true;
}

export function stateBlock(state, provenance, remediation) {
  const copy = STATE_COPY[state] || { label: state, detail: "" };
  const prov = provenance
    ? `<div class="prov">source: ${esc(provenance.db || "api")}`
      + (provenance.last_ingest_at
         ? ` · last ingest ${esc(ago(provenance.last_ingest_at))}` : "")
      + ` · checked ${esc(ago(provenance.checked_at))}</div>`
    : "";
  const rem = remediation
    ? `<div class="remedy">${esc(remediation)}</div>` : "";
  return `<div class="panel-state st-${esc(state)}">
    <div class="label">${esc(copy.label)}</div>
    ${copy.detail ? `<div class="detail">${esc(copy.detail)}</div>` : ""}
    ${rem}${prov}</div>`;
}
