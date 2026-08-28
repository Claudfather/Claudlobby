// app.js — the story-first operator plane (Phase-4 walk rulings).
// Acceptance criterion (operator feedback 2026-08-28): someone who has never
// seen the schema can look at the channel and say what the fleet is doing.
// Names not identifiers; conversations not a flat list; plain-language
// delivery states; machinery demoted behind an explicit toggle.

import { esc, ago, renderState } from "/panel-state.js";

const $ = (id) => document.getElementById(id);

// Plain-language transmission states (feedback ruling — jargon stays in the
// machinery rail). class: ok=activated, pend=in-flight, bad=loud.
const TX = {
  pane_submitted: { label: "delivered", cls: "ok" },
  carrier_accepted: { label: "sent to Telegram", cls: "ok" },
  recipient_acknowledged: { label: "acknowledged", cls: "ok" },
  carrier_queued: { label: "queued — bot was mid-turn", cls: "pend" },
  send_attempted: { label: "sending…", cls: "pend" },
  unknown: { label: "delivery unknown", cls: "pend" },
  duplicate_suppressed: { label: "duplicate suppressed", cls: "pend" },
  failed: { label: "FAILED to deliver", cls: "bad" },
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

function latestTx(msg) {
  return msg.tx && msg.tx.length ? msg.tx[msg.tx.length - 1] : null;
}

function deliveryLine(msg) {
  const t = latestTx(msg);
  if (!t) return "";
  const d = TX[t.event] || { label: t.event, cls: "pend" };
  return `<div class="delivery ${d.cls}">${esc(d.label)}</div>`;
}

// "[BOTCOMMAND] erlich | task | <text>" / "[BOTREPORT] x | done | <text>"
// -> "<text>": the prefix is carrier framing (sender/class/status all render
// as chips already), not words (operator feedback: machinery in the prose).
function stripEnvelope(text) {
  return text.replace(/^\[BOT(?:COMMAND|REPORT)\]\s*[^|\n]*\|\s*[^|\n]*\|\s*/,
                      "");
}

function bodyBlock(m) {
  if (m.body) return `<div class="body">${esc(stripEnvelope(m.body))}</div>`;
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
    `emitter ${esc(m.emitter)}`,
  ].filter(Boolean).join(" · ");
  return `<details><summary>machinery</summary>`
       + `<div class="machinery">${parts}</div></details>`;
}

// The task ribbon: the §16 lifecycle of one work item as steps.
function ladder(thread) {
  if (!thread.work_item_id) return "";
  const seen = thread.task_events.map((e) => e.event);
  const terminal = seen.filter((e) =>
    ["completed", "failed", "cancelled", "returned_blocked", "superseded",
     "reassigned", "expired"].includes(e)).pop();
  const steps = [];
  steps.push({ label: "dispatched", on: true });
  const anyTx = thread.messages.some((m) => latestTx(m));
  const delivered = thread.messages.some((m) => {
    const t = latestTx(m);
    return t && ["pane_submitted", "carrier_accepted",
                 "recipient_acknowledged"].includes(t.event);
  });
  steps.push({ label: "delivered", on: delivered, now: anyTx && !delivered });
  if (seen.includes("accepted")) steps.push({ label: "accepted", on: true });
  if (seen.includes("progress")) steps.push({ label: "progress", on: true });
  if (terminal) {
    const d = TASK_STATUS[terminal] || { label: terminal };
    steps.push({ label: d.label, on: true });
  } else {
    steps.push({ label: "working…", on: false, now: delivered });
  }
  return `<div class="t-ladder">` + steps.map((s) =>
    `<span class="step ${s.on ? "done" : ""} ${s.now ? "now" : ""}">`
    + `${esc(s.label)}</span>`).join("") + `</div>`;
}

const clip = (s, n) => (s && s.length > n ? s.slice(0, n) + "…" : s);

function threadTitle(t) {
  // ALWAYS clipped — dispatch titles are whole task texts, and unclipped
  // they render the same paragraph as headline AND first message (operator
  // screenshot, 2026-08-28). The message below carries the full words.
  if (t.title) return clip(stripEnvelope(t.title), 100);
  const first = t.messages[0];
  if (first && first.body) return clip(stripEnvelope(first.body)
                                         .split("\n")[0], 100);
  const cls = first ? first.message_class.replace("_", " ") : "conversation";
  return `${cls}${first ? ` from ${first.sender_short}` : ""}`;
}

function renderChannel(env) {
  const el = $("channel");
  if (renderState(el, env, { idleWhenEmpty: (d) => !d.threads.length })) return;
  el.innerHTML = env.data.threads.map((t) => {
    const participants = [...new Set(
      t.messages.flatMap((m) => [m.sender_short, m.recipient_short])
        .filter(Boolean))].join(", ");
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
    const kicker = t.work_item_id
      ? `work item${t.repo ? ` · ${esc(t.repo)}` : ""}` : "conversation";
    return `<article class="thread">
      <div class="t-kicker">${kicker}</div>
      <div class="t-head"><span class="t-title">${esc(threadTitle(t))}</span>
        <span class="t-meta">${esc(participants)}</span></div>
      ${ladder(t)}
      ${msgs}
    </article>`;
  }).join("");
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
    return `<div class="card ${r.attention ? "attn" : ""}">
      <span class="st ${st.cls}">${esc(st.label)}</span>
      <b>${esc(clip(stripEnvelope(r.title || ""), 160)
             || r.work_item_id)}</b>
      <div class="sub">${esc(r.assignee_short || r.assignee_uid)}`
      + `${r.expected_by ? ` · due ${esc(ago(r.expected_by))
            .replace(" ago", " past")}` : ""}`
      + `${r.attention ? " · ⚠ needs you" : ""}</div></div>`;
  };
  attEl.innerHTML = attn.length ? attn.map(card).join("")
    : `<div class="panel-state st-idle"><div class="label">nothing needs`
      + ` you</div></div>`;
  taskEl.innerHTML = rows.filter((r) => !r.attention).map(card).join("");
}

function renderFleet(env) {
  const el = $("fleet");
  if (renderState(el, env,
                  { idleWhenEmpty: (d) => !d.identities.length })) return;
  el.innerHTML = env.data.identities.map((a) => {
    const live = a.last_seen && (Date.now() - Date.parse(a.last_seen)) < 36e5;
    return `<div class="actor">
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
    label.textContent = env ? env.state : "view daemon unreachable";
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
  beat.className = "dot " + (d.ingest_socket_present
    ? (fresh ? "ok" : "warn") : "");
  label.textContent = d.ingest_socket_present
    ? (fresh ? "recording" : "recorder up, quiet") : "recorder DOWN";
}

function renderDebugRow(row) {
  const el = $("debug");
  const div = document.createElement("div");
  div.className = "evt";
  div.innerHTML = `<span>${esc(ago(row.ingested_at))}</span>`
    + `<b>${esc(row.family)}</b><span>seq ${row.ingest_seq}</span>`;
  el.prepend(div);
  while (el.children.length > 120) el.lastChild.remove();
}

// ---------------------------------------------------------------------------
const BOARD_FAMILIES = new Set(["communication", "transmission", "task",
  "work_item", "assignment", "workstream", "workstream_event"]);

let refreshTimer = null;
async function refreshBoards() {
  renderChannel(await jget("/api/channel"));
  renderTasks(await jget("/api/tasks"));
  renderFleet(await jget("/api/identities"));
  renderSummary(await jget("/api/summary"));
}

function scheduleRefresh() { // coalesce bursts into one refetch
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refreshBoards(); },
                            400);
}

function openStream() {
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    try {
      const payload = JSON.parse(ev.data);
      let boardHit = false;
      for (const row of payload.rows) {
        renderDebugRow(row);
        if (BOARD_FAMILIES.has(row.family)) boardHit = true;
      }
      if (boardHit) scheduleRefresh();
      else jget("/api/summary").then(renderSummary);
    } catch { /* malformed push — the next board refresh corrects */ }
  };
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
  $("debug-toggle").setAttribute("aria-pressed", String(!rail.hidden));
});

["channel", "tasks", "attention", "fleet"].forEach((id) =>
  renderState($(id), { state: "loading" }));
refreshBoards();
openStream();
// Slow safety refresh: relative times re-render and any missed push heals.
setInterval(refreshBoards, 60000);
