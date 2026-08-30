// app.js — the story-first operator plane (Phase-4 walk + its gauntlet).
// Acceptance criterion (operator feedback 2026-08-28): someone who has never
// seen the schema can look at the channel and say what the fleet is doing.
// Names not identifiers; conversations not a flat list; plain-language
// delivery states; machinery demoted behind a toggle.
//
// SEMANTICS COME FROM THE SERVER (gauntlet): threads arrive stamped with
// `delivered`/`terminal` and each tx with `activated`, derived from
// queries.py's one-definition constants — this file renders facts and owns
// only presentation labels. The first version's copied vocabularies already
// disagreed live with the SQL reducer.

import { esc, ago, renderState, stateBlock } from "/panel-state.js";

const $ = (id) => document.getElementById(id);

// Plain-language transmission states — PRESENTATION ONLY (the ok/pend/bad
// coloring of a tx rides its server-stamped `activated` + failed check).
const TX = {
  pane_submitted: "delivered",
  carrier_accepted: "sent to Telegram",
  recipient_acknowledged: "acknowledged",
  carrier_queued: "queued — bot was mid-turn",
  send_attempted: "sending…",
  unknown: "delivery unknown",
  duplicate_suppressed: "duplicate suppressed",
  failed: "FAILED to deliver",
};

const TASK_STATUS = {
  open: { label: "in flight", cls: "s-open" },
  pending_unacknowledged: { label: "pending delivery", cls: "s-pend" },
  dispatch_failed: { label: "dispatch FAILED", cls: "s-bad" },
  created_not_sent: { label: "not sent", cls: "s-pend" },
  completed: { label: "completed", cls: "s-done" },
  failed: { label: "failed", cls: "s-bad" },
  cancelled: { label: "cancelled", cls: "s-done" },
  returned_blocked: { label: "returned blocked", cls: "s-bad" },
  superseded: { label: "superseded", cls: "s-done" },
  reassigned: { label: "reassigned", cls: "s-done" },
  expired: { label: "expired", cls: "s-bad" },
  progress: { label: "in progress", cls: "s-open" },
  accepted: { label: "accepted", cls: "s-open" },
  blocked_waiting: { label: "blocked (waiting)", cls: "s-pend" },
  resumed: { label: "resumed", cls: "s-open" },
};

const CLASS_TAGS = new Set(["task_request", "report", "question", "answer",
  "alert", "notice", "briefing", "nudge", "acknowledgement", "chat",
  "config_change", "raw_control"]);

async function jget(url) {
  try {
    const r = await fetch(url);
    return await r.json();
  } catch {
    return null; // renderState(null) => disconnected
  }
}

// Signed deadline label (gauntlet, 3 reviewers: the old ago()+replace made
// every NOT-yet-due task read "due 0s past").
function dueLabel(iso) {
  if (!iso) return "";
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (Number.isNaN(s)) return "";
  const mag = Math.abs(s);
  const unit = mag < 60 ? `${mag | 0}s` : mag < 3600 ? `${(mag / 60) | 0}m`
    : mag < 86400 ? `${(mag / 3600) | 0}h` : `${(mag / 86400) | 0}d`;
  return s >= 0 ? `due in ${unit}` : `overdue ${unit}`;
}

const clip = (s, n) => (s && s.length > n ? s.slice(0, n) + "…" : s);

function latestTx(msg) {
  return msg.tx && msg.tx.length ? msg.tx[msg.tx.length - 1] : null;
}

function deliveryLine(msg) {
  const t = latestTx(msg);
  if (!t) return "";
  const cls = t.event === "failed" ? "bad" : t.activated ? "ok" : "pend";
  return `<div class="delivery ${cls}">${esc(TX[t.event] || t.event)}</div>`;
}

function bodyBlock(m) {
  const words = m.body_words || m.body;
  if (words) return `<div class="body">${esc(words)}</div>`;
  return `<div class="body redacted">message captured as metadata only`
       + ` (${m.body_bytes} bytes${m.truncated ? ", truncated" : ""})</div>`;
}

function machineryBlock(m) {
  const t = latestTx(m);
  const parts = [
    `msg ${esc(m.msg_id)}`,
    m.work_item_id ? `work item ${esc(m.work_item_id)}` : "",
    m.assignment_id ? `assignment ${esc(m.assignment_id)}` : "",
    `seq ${m.ingest_seq}`,
    t ? `tx ${esc(t.event)} via ${esc(t.carrier)}#${t.attempt_no}` : "",
    `class ${esc(m.message_class)}`,
    `sender ${esc(m.sender_alias)}`,
    `emitter ${esc(m.emitter)}`,
  ].filter(Boolean).join(" · ");
  // The verbatim wire body (framing included) lives here — the prose above
  // renders body_words; this expand is the raw-text view of truth.
  const raw = m.body && m.body !== (m.body_words || "")
    ? `<div class="machinery raw">${esc(m.body)}</div>` : "";
  return `<details><summary>machinery</summary>`
       + `<div class="machinery">${parts}</div>${raw}</details>`;
}

// The task ribbon renders SERVER facts: thread.delivered / thread.terminal.
function ladder(thread) {
  if (!thread.work_item_id) return "";
  const events = thread.task_events.map((e) => e.event);
  const steps = [{ label: "dispatched", on: true }];
  const anyTx = thread.messages.some((m) => latestTx(m));
  steps.push({ label: "delivered", on: thread.delivered,
               now: anyTx && !thread.delivered });
  if (events.includes("accepted")) steps.push({ label: "accepted", on: true });
  if (events.includes("progress")) steps.push({ label: "progress", on: true });
  if (thread.terminal) {
    const d = TASK_STATUS[thread.terminal] || { label: thread.terminal };
    steps.push({ label: d.label, on: true });
  } else {
    steps.push({ label: "working…", on: false, now: thread.delivered });
  }
  return `<div class="t-ladder">` + steps.map((s) =>
    `<span class="step ${s.on ? "done" : ""} ${s.now ? "now" : ""}">`
    + `${esc(s.label)}</span>`).join("") + `</div>`;
}

function threadTitle(t) {
  if (t.title) return clip(t.title, 100);
  const first = t.messages[0];
  if (first && (first.body_words || first.body)) {
    return clip((first.body_words || first.body).split("\n")[0], 100);
  }
  const cls = first ? first.message_class.replace("_", " ") : "conversation";
  return `${cls}${first ? ` from ${first.sender_short}` : ""}`;
}

function threadArticle(t) {
  const first = t.messages[0];
  const attribution = first
    ? `${first.sender_short} → ${first.recipient_short || "—"}`
      + ` · ${ago(first.occurred_at)}`
    : "";
  const kicker = t.work_item_id
    ? `work item${t.repo ? ` · ${esc(t.repo)}` : ""}` : "conversation";
  const msgs = t.messages.map((m) => `
    <div class="msg">
      <div class="who"><b>${esc(m.sender_short)}</b>
        <span class="to">→ ${esc(m.recipient_short || "—")}</span>
        ${CLASS_TAGS.has(m.message_class)
          ? `<span class="tag ${esc(m.message_class)}">`
            + `${esc(m.message_class.replace("_", " "))}</span>` : ""}
        <time>${esc(ago(m.occurred_at))}</time></div>
      ${bodyBlock(m)}
      ${deliveryLine(m)}
      ${machineryBlock(m)}
    </div>`).join("");
  const el = document.createElement("article");
  el.className = "thread";
  el.dataset.key = t.key;
  el.dataset.seq = String(t.latest_seq);
  el.innerHTML = `
    <div class="t-kicker">${kicker}</div>
    <div class="t-head"><span class="t-title">${esc(threadTitle(t))}</span>
      <span class="t-meta">${esc(attribution)}</span></div>
    ${ladder(t)}
    ${msgs}`;
  return el;
}

// Keyed render: unchanged threads keep their existing DOM node (open
// machinery <details>, text selection, layout) — a full innerHTML rebuild
// collapsed all of it every refresh (gauntlet; §16 preserved-focus).
function renderChannel(env) {
  const el = $("channel");
  if (renderState(el, env, { idleWhenEmpty: (d) => !d.threads.length })) return;
  const existing = new Map(
    [...el.querySelectorAll("article.thread")].map((n) => [n.dataset.key, n]));
  const frag = document.createDocumentFragment();
  for (const t of env.data.threads) {
    const prev = existing.get(t.key);
    frag.appendChild(prev && prev.dataset.seq === String(t.latest_seq)
      ? prev : threadArticle(t));
  }
  el.replaceChildren(frag);
}

function renderTasks(env) {
  const attEl = $("attention"), taskEl = $("tasks"), badge = $("attn-count");
  if (renderState(taskEl, env,
                  { idleWhenEmpty: (d) => !d.assignments.length })) {
    renderState(attEl, env, { idleWhenEmpty: () => true });
    badge.hidden = true;
    return;
  }
  const rows = env.data.assignments;
  const attn = rows.filter((r) => r.attention);
  badge.hidden = !attn.length;
  badge.textContent = attn.length;
  const card = (r) => {
    const st = TASK_STATUS[r.status] || { label: r.status, cls: "" };
    const due = dueLabel(r.expected_by);
    return `<div class="card ${r.attention ? "attn" : ""}">
      <span class="st ${st.cls}">${esc(st.label)}</span>
      <b>${esc(clip(r.title || "", 160) || r.work_item_id)}</b>
      <div class="sub">${esc(r.assignee_short || r.assignee_uid)}`
      + `${due ? ` · ${esc(due)}` : ""}`
      + `${r.attention ? " · ⚠ needs you" : ""}</div></div>`;
  };
  attEl.innerHTML = attn.length ? attn.map(card).join("")
    : stateBlock("idle", null, null, { label: "nothing needs you" });
  taskEl.innerHTML = rows.filter((r) => !r.attention).map(card).join("");
}

function renderFleet(env) {
  const el = $("fleet");
  if (renderState(el, env,
                  { idleWhenEmpty: (d) => !d.identities.length })) return;
  el.innerHTML = env.data.identities.map((a) => {
    const live = a.last_seen && (Date.now() - Date.parse(a.last_seen)) < 36e5;
    return `<div class="actor" title="${esc(a.alias)}">
      <span class="dot ${live ? "live" : ""}"></span>
      <span>${esc(a.short)}</span>
      ${a.provisional ? `<span class="prov-badge" title="provisional`
        + ` identity — unconfirmed by the registry">?</span>` : ""}
      <small>${esc(a.kind)} · ${esc(ago(a.last_seen))}</small></div>`;
  }).join("");
}

function renderSummary(env) {
  const beat = $("beat"), label = $("beat-label");
  if (!env || env.state !== "ok") {
    beat.className = "dot";
    label.textContent = env ? `source ${env.state}` : "view daemon unreachable";
    $("age").textContent = "—";
    return;
  }
  const d = env.data, prov = env.provenance;
  $("total").textContent = d.counts.ingest_ledger;
  $("spool").textContent = d.spool_files;
  $("age").textContent = prov.last_ingest_at ? ago(prov.last_ingest_at)
                                             : "never";
  const fresh = prov.last_ingest_at
    && (Date.now() - Date.parse(prov.last_ingest_at)) < 12e4;
  beat.className = "dot " + (d.daemon_serving ? (fresh ? "ok" : "warn") : "");
  label.textContent = d.daemon_serving
    ? (fresh ? "recording" : "recorder up, quiet") : "recorder DOWN";
}

// Debug rail: a ring buffer rendered ONLY while visible (the hidden rail
// used to receive DOM churn for every ledger row regardless — gauntlet).
const debugRing = [];
function pushDebugRow(row) {
  debugRing.push(row);
  if (debugRing.length > 120) debugRing.shift();
  if (!$("rail-debug").hidden) renderDebugRail();
}
function renderDebugRail() {
  $("debug").innerHTML = [...debugRing].reverse().map((row) =>
    `<div class="evt"><span>${esc(ago(row.ingested_at))}</span>`
    + `<b>${esc(row.family)}</b><span>seq ${row.ingest_seq}</span></div>`
  ).join("");
}

// ---------------------------------------------------------------------------
let refreshTimer = null;
let safetyTimer = null;
let generation = 0;

async function refreshBoards() {
  const gen = ++generation;   // stale responses never paint over newer ones
  const [ch, tk, fl, sm] = await Promise.all([
    jget(channelUrl()), jget("/api/tasks"),
    jget("/api/identities"), jget("/api/summary"),
  ]);
  if (gen !== generation) return;
  renderChannel(ch); renderTasks(tk); renderFleet(fl); renderSummary(sm);
  if (fl && fl.state === "ok") renderFleetTabs(fl.data.identities);
  restartSafety();
}

function scheduleRefresh() { // coalesce bursts into one refetch
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refreshBoards(); },
                            400);
}

function restartSafety() {  // relative times re-render; missed pushes heal
  clearTimeout(safetyTimer);
  safetyTimer = setTimeout(refreshBoards, 60000);
}

function openStream() {
  const es = new EventSource("/api/stream");  // server starts at HEAD;
  es.onmessage = (ev) => {                    // reconnects ride Last-Event-ID
    try {
      const payload = JSON.parse(ev.data);
      for (const row of payload.rows) pushDebugRow(row);
      if (payload.rows.length) scheduleRefresh();
    } catch { /* malformed push — the next board refresh corrects */ }
  };
  es.addEventListener("source", (ev) => {
    // The RECORDER's source went absent/unreadable mid-stream — typed, not
    // silence (the first version pushed these frames to no listener).
    try {
      const env = JSON.parse(ev.data);
      renderSummary(env);
      renderState($("channel"), env);
    } catch { /* next refresh corrects */ }
  });
  es.onerror = () => {
    // The page cannot reach its own API — say so; never fake health.
    $("beat").className = "dot";
    $("beat-label").textContent = "reconnecting…";
    // EventSource retries itself (server sent retry: 3000)
  };
}

$("debug-toggle").addEventListener("click", () => {
  const rail = $("rail-debug");
  rail.hidden = !rail.hidden;
  if (!rail.hidden) renderDebugRail();
  $("debug-toggle").setAttribute("aria-pressed", String(!rail.hidden));
});


// ---------------------------------------------------------------------------
// Grid view + fleet tabs (Phase-4 chunk 2)
// ---------------------------------------------------------------------------

// Minimal ANSI SGR -> HTML. Every TEXT run routes through esc(); only
// class names of our own minting enter markup. Non-SGR escapes (cursor,
// OSC titles) are stripped first.
function ansiToHtml(text) {
  const cleaned = (text || "")
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")   // OSC
    .replace(/\x1b\[[0-9;?]*[@-ln-~]/g, "");      // CSI non-SGR (keep m)        // CSI non-SGR
  let out = "", cls = [];
  for (const part of cleaned.split(/(\x1b\[[0-9;]*m)/)) {
    const m = part.match(/^\x1b\[([0-9;]*)m$/);
    if (!m) { if (part) out += span(cls, part); continue; }
    const codes = (m[1] || "0").split(";").map(Number);
    for (let i = 0; i < codes.length; i++) {
      const code = codes[i];
      if (code === 0) cls = [];
      else if (code === 1) cls.push("a-b");
      else if (code === 39) cls = cls.filter((c) => !/^a-\d+$/.test(c));
      else if (code === 38 || code === 48) {
        // Extended color: 38;5;N (256) or 38;2;R;G;B (truecolor) — Claude
        // panes are truecolor-heavy, so CONSUME the args (do not re-read
        // them as basic SGR codes) rather than render them. i advances past
        // the argument sequence; a nearest-basic mapping is a later polish.
        i += (codes[i + 1] === 5) ? 2 : (codes[i + 1] === 2) ? 4 : 1;
      } else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
        cls = cls.filter((c) => !/^a-\d+$/.test(c));
        cls.push(`a-${code}`);
      }
    }
  }
  return out;
  function span(c, t) {
    return c.length ? `<span class="${c.join(" ")}">${esc(t)}</span>` : esc(t);
  }
}

let currentView = "channel";
let currentFleet = null;   // null = auto (single fleet or "all" not yet chosen)
let gridTimer = null;
let focusTimer = null;

function channelUrl() {
  const f = currentFleet && currentFleet !== "all"
    ? `&fleet=${encodeURIComponent(currentFleet)}` : "";
  return `/api/channel?limit=120${f}`;
}

function renderFleetTabs(identities) {
  const fleets = identities.filter((i) => i.kind === "fleet")
    .map((i) => i.alias).sort();
  const el = $("fleet-tabs");
  if (fleets.length < 2) { el.innerHTML = ""; currentFleet = null; return; }
  // Per-team rooms are the DEFAULT (operator ruling): a fleet tab is always
  // selected; the merged firehose is the explicit last resort.
  if (!currentFleet) {
    // Rooms are the default: on first paint with >1 fleet the channel was
    // fetched as the firehose (currentFleet null); adopt the room AND
    // refetch so a room tab never labels firehose content.
    currentFleet = fleets[0];
    jget(channelUrl()).then(renderChannel);
  }
  el.innerHTML = [...fleets, "all"].map((f) =>
    `<button class="pill ghost ${f === currentFleet ? "on" : ""}"`
    + ` data-fleet="${esc(f)}" type="button">${esc(f)}</button>`).join("");
  el.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      currentFleet = b.dataset.fleet;
      renderFleetTabs(identities);   // instant highlight
      refreshBoards();               // guarded path (generation stale-guard)
    }));
}

function renderGrid(env) {
  const el = $("grid");
  if (!env || env.state !== "ok") {
    el.innerHTML = stateBlock(env ? env.state : "disconnected",
                              env && env.provenance,
                              env && env.remediation);
    return;
  }
  const panes = env.data.panes;
  if (!panes.length) {
    el.innerHTML = stateBlock("idle", env.provenance,
                              "no bots discovered on this host");
    return;
  }
  el.innerHTML = panes.map((p) => `
    <div class="pane-card ${p.alive ? "" : "dead"}" data-bot="${esc(p.bot)}"
         data-fleet="${esc(p.fleet)}">
      <div class="p-head"><span class="dot ${p.alive ? "live" : ""}"></span>
        <b>${esc(p.bot)}</b><span class="tag">${esc(p.fleet)}</span>
        <small>${p.captured_ago_s != null ? esc(p.captured_ago_s) + "s" : "—"}</small>
      </div>
      <pre>${ansiToHtml(p.lines)}</pre>
      ${p.alive ? "" : `<div class="dead-note">session down</div>`}
    </div>`).join("");
  el.querySelectorAll(".pane-card").forEach((c) =>
    c.addEventListener("click",
                       () => openFocus(c.dataset.bot, c.dataset.fleet)));
}

async function pollGrid() {
  if (currentView !== "grid") return;
  renderGrid(await jget("/api/grid"));
}

function setView(view) {
  currentView = view;
  $("channel").hidden = view !== "channel";
  $("grid").hidden = view !== "grid";
  document.querySelectorAll("#view-nav button").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === view));
  clearInterval(gridTimer);
  if (view === "grid") {
    renderState($("grid"), { state: "loading" });
    pollGrid();
    gridTimer = setInterval(pollGrid, 5000);
  }
}
document.querySelectorAll("#view-nav button").forEach((b) =>
  b.addEventListener("click", () => setView(b.dataset.view)));

function openFocus(bot, fleet) {
  clearInterval(focusTimer);   // no prior timer bleeds into this overlay
  $("focus-overlay").hidden = false;
  $("focus-title").textContent = bot;
  $("focus-title").dataset.fleet = fleet || "";
  $("focus-pane").innerHTML = "";
  const q = `/api/grid?focus=${encodeURIComponent(bot)}`
    + (fleet ? `&fleet=${encodeURIComponent(fleet)}` : "");
  const tick = async () => {
    const env = await jget(q);
    if (!env || env.state !== "ok") return;
    if ($("focus-title").textContent !== bot) return;  // overlay moved on
    const pane = env.data.panes.find(
      (p) => p.bot === bot && (!fleet || p.fleet === fleet));
    if (pane) $("focus-pane").innerHTML = ansiToHtml(pane.lines);
  };
  tick();
  focusTimer = setInterval(tick, 1000);
}
function closeFocus() {
  $("focus-overlay").hidden = true;
  clearInterval(focusTimer);
}
$("focus-close").addEventListener("click", closeFocus);
$("focus-overlay").addEventListener("click", (e) => {
  if (e.target.id === "focus-overlay") closeFocus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("focus-overlay").hidden) closeFocus();
});

// Bootstrap LAST — after every top-level `let` (currentFleet, currentView…)
// has initialized. Placed mid-file it read those bindings in their temporal
// dead zone and threw on first load, freezing the page at its loading markup.
["channel", "tasks", "attention", "fleet"].forEach((id) =>
  renderState($(id), { state: "loading" }));
refreshBoards();
openStream();
