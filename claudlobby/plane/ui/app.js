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
  // the task loop's human acts (chunk M-A, #1481) — both NON-terminal, so
  // both read as live work rather than as an ending
  escalated: { label: "needs a human", cls: "s-pend" },
  nudged: { label: "nudged", cls: "s-open" },
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

// A body past CLAMP_LINES lines is clamped with a "show more" toggle (chunk
// L, #1479): a thirty-line dispatch made everything below it unscannable.
// The toggle is delegated on #channel. The number lives HERE and only here —
// the stylesheet reads it as a custom property, so the clamp height and the
// decision to clamp can never be two different eights.
const CLAMP_LINES = 8, CLAMP_CHARS = 900;
document.documentElement.style.setProperty("--clamp-lines", String(CLAMP_LINES));

// Which bodies the operator opened, by msg_id. A card is rebuilt whenever
// its thread gains a message, and the open state used to live only on that
// DOM node — so the reply that arrived while you were reading re-collapsed
// the message you had expanded (fold F8).
const openBodies = new Set();

function bodyBlock(m) {
  const words = m.body_words || m.body;
  if (words) {
    const long = words.split("\n").length > CLAMP_LINES || words.length > CLAMP_CHARS;
    const open = long && openBodies.has(m.msg_id);
    return `<div class="body${long ? " clamp" : ""}${open ? " open" : ""}">`
      + `${esc(words)}</div>`
      + (long ? `<button class="more" type="button">`
                + `${open ? "show less" : "show more"}</button>` : "");
  }
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
  // U2: a cross-fleet thread carries a visible mark; its names arrive
  // fleet-qualified from the server (`eng/erlich → data/samir`) in every
  // room, while intra-fleet names stay short in their own room
  const xfleet = t.cross_fleet
    ? `<span class="tag xfleet" title="sender and recipient are on`
      + ` different fleets">cross-fleet</span>` : "";
  const msgs = t.messages.map((m) => `
    <div class="msg" data-msg-id="${esc(m.msg_id)}">
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
  el.dataset.room = currentFleet || "all";   // the room this card's names were rendered for
  el.innerHTML = `
    <div class="t-kicker">${kicker}</div>
    <div class="t-head"><span class="t-title">${esc(threadTitle(t))}</span>
      ${xfleet}<span class="t-meta">${esc(attribution)}</span></div>
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
  // A card is reused only when its thread is unchanged AND it was rendered
  // for THIS room: the same thread carries bare names in its room and
  // fleet-qualified names under `all`, so a card kept across a tab switch
  // showed the previous room's names (live on the Mini, U follow-up)
  const room = currentFleet || "all";
  for (const t of env.data.threads) {
    const prev = existing.get(t.key);
    frag.appendChild(prev && prev.dataset.seq === String(t.latest_seq)
                     && prev.dataset.room === room
      ? prev : threadArticle(t));
  }
  el.replaceChildren(frag);
}

// WHY a card needs you, and what clears it — the queue's own arms as the API
// stamps them (attention_reason / attention_since, from ATTENTION_ARMS_SQL:
// the query that SELECTED the row also says which arm holds). Reason and
// remedy are separated by a real dash; every magnitude is the server's
// instant through ago(), never re-derived here — reading it off the deadline
// printed "overdue due in 5m" whenever the two clocks disagreed (fold F7).
// No fallback line: attention IS the disjunction of these arms, so an
// attention row always carries at least one (pinned server-side).
//
// Each arm dates itself from the server instant that arm is ABOUT: the two
// send arms from `attention_since` (the dispatch), the deadline arm from the
// deadline — which is the same field when overdue is the only arm, and the
// right one when it rides behind a failed send — and the two HUMAN arms
// (chunk M-A, #1481) from the instant the human acted, which the server
// already put in `attention_since` for them.
const WHY = {
  escalated: (r) => [
    r.attention_question
      ? `needs you: ${r.attention_question}`
      : "needs you — the question was not recorded",
    `asked by ${r.attention_by || "the manager"} ${ago(r.attention_since)}`],
  send_failed: (r) => [`send failed ${ago(r.attention_since)}`,
                       "re-send with --supersedes, or withdraw it"
                       + " (task-act.sh withdraw <id>)"],
  never_activated: (r) => [`queued ${ago(r.attention_since)}, never delivered`,
                           "re-send with --supersedes, check the bot is up, or"
                           + " withdraw it (task-act.sh withdraw <id>)"],
  // the nudge's own reason, when the operator gave one and the capture kept
  // it: "why did somebody poke this" is the first thing the reader asks, and
  // the server already stamps it for the leading arm (fold F10)
  nudged: (r) => [`nudged ${ago(r.attention_since)} by`
                  + ` ${r.attention_by || "the operator"}, no act yet`
                  + (r.attention_act_reason ? `: ${r.attention_act_reason}` : ""),
                  "the manager owes a chase, supersede, withdraw or escalate"],
  overdue: (r) => [`overdue ${ago(r.expected_by || r.attention_since)}`,
                   "chase the worker, or re-dispatch with a new deadline"],
};

function attentionWhy(r) {
  if (!r.attention) return "";
  const parts = (r.attention_reason || []).filter((k) => WHY[k]).map((k) => {
    const [reason, fix] = WHY[k](r);
    return `${esc(reason)} — <small>${esc(fix)}</small>`;
  });
  if (!parts.length) return "";
  return `<div class="why">⚠ ${parts.join(" · ")}</div>`;
}

// An OUTSTANDING nudge that is not (yet) an alarm — the fold's F11. The
// `nudged` arm fires only once the grace has passed; for the thirty minutes
// before that, "somebody poked this two minutes ago" is the most useful thing
// the card can say about the row, and it is information rather than a
// warning, so it gets its own quiet line. Suppressed once the arm leads, or
// the same fact would print twice in two voices ("nudged 2m ago by chris" and
// "⚠ nudged 40m ago by chris, no act yet").
function nudgeNote(r) {
  if (!r.nudged_at) return "";
  if ((r.attention_reason || [])[0] === "nudged") return "";
  return `<div class="note">nudged ${esc(ago(r.nudged_at))} by`
    + ` ${esc(r.nudged_by || "the operator")}</div>`;
}

// ONE group-by, two callers (fold F6): the attention rail groups the rows the
// API keyed as one broadcast, the roster groups rows by the fleet the API
// stamped. A null key is its OWN group — an unkeyed row is a group of one —
// and order is the input's: a group sits where its first member sat.
function groupBy(rows, keyOf) {
  const out = [], byKey = new Map();
  for (const r of rows) {
    const k = keyOf(r);
    const seen = k == null ? null : byKey.get(k);
    if (seen) { seen.push(r); continue; }
    const group = [r];
    if (k != null) byKey.set(k, group);
    out.push(group);
  }
  return out;
}

// A note dispatched to N bots is N rows, and the rail rendered N identical
// cards (item 6, #1479). WHICH rows are one broadcast is the SERVER's fact
// (`broadcast_key` — one sender, the same stored words, the same state, the
// same arm, one dispatch instant, one row per recipient): the page groups
// what the API keyed and derives no key of its own, so it can never group
// two rows the API considers unrelated.
function groupBroadcasts(rows) {
  return groupBy(rows, (r) => r.broadcast_key);
}

// A card that must be scrolled past has already failed to route attention
// (brief.py's rule), so a long recipient list elides — the COUNT is always
// whole, and the full list is the row's hover title.
const NAMES_SHOWN = 6;

function recipientsLine(g) {
  const names = g.map((r) => r.assignee_short || r.assignee_uid);
  const shown = names.slice(0, NAMES_SHOWN).map(esc).join(", ")
    + (names.length > NAMES_SHOWN ? " …" : "");
  return `→ ${shown} · ${names.length} bots`;
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
  badge.textContent = attn.length;   // ROWS, so the badge and the header agree
  const card = (g) => {
    // A group is dated by its WORST member — the earliest instant the server
    // stamped — so one card for four bots can never under-report the wait it
    // is showing. Selection over server instants, never a magnitude of ours.
    const r = g.length === 1 ? g[0] : g.reduce((a, b) =>
      ((b.attention_since || "") < (a.attention_since || "") ? b : a));
    const st = TASK_STATUS[r.status] || { label: r.status, cls: "" };
    // A finished task has no deadline any more: it reads "completed 1m ago"
    // (the terminal instant the API stamps); the deadline is an OPEN task's
    // fact only (chunk L, #1479 — "completed · overdue 1h" was one card).
    // ENDEDNESS is `terminal_at` itself — the server stamps status and
    // instant from one row, so a copy of its terminal vocabulary here could
    // only ever disagree with it (fold F4).
    const when = r.terminal_at
      ? `${st.label} ${ago(r.terminal_at)}` : dueLabel(r.expected_by);
    const many = g.length > 1;
    const who = many ? recipientsLine(g)
      : esc(r.assignee_short || r.assignee_uid);
    const hover = many
      ? ` title="${esc(g.map((m) => m.assignee_short || m.assignee_uid)
                        .join(", "))}"` : "";
    return `<div class="card ${r.attention ? "attn" : ""}">
      <span class="st ${st.cls}">${esc(st.label)}</span>
      <b>${esc(clip(r.title || "", 160) || r.work_item_id)}</b>
      <div class="sub"${hover}>${who}`
      + `${when ? ` · ${esc(when)}` : ""}</div>`
      + attentionWhy(r) + nudgeNote(r) + `</div>`;
  };
  attEl.innerHTML = attn.length ? groupBroadcasts(attn).map(card).join("")
    : stateBlock("idle", null, null, { label: "nothing needs you" });
  taskEl.innerHTML = rows.filter((r) => !r.attention)
    .map((r) => card([r])).join("");
}

function railRow(a) {
  const live = a.last_seen && (Date.now() - Date.parse(a.last_seen)) < 36e5;
  return `<div class="actor" title="${esc(a.alias)}">
    <span class="dot ${live ? "live" : ""}"></span>
    <span>${esc(a.short)}</span>
    ${a.provisional ? `<span class="prov-badge" title="provisional`
      + ` identity — unconfirmed by the registry">?</span>` : ""}
    <small>${esc(a.kind)} · ${esc(ago(a.last_seen))}</small></div>`;
}

// The roster (item 6, #1479). In a fleet ROOM the read holds one fleet and
// the rail is the flat last-seen list it has always been. Under `all` that
// list interleaved every fleet, so the rail groups under a small header —
// the fleet's alias and its bot count — while KEEPING the recency order the
// operator reads it by: rows arrive last-seen first, so a group's first row
// is its most recent, and the groups sort by that. Names are the server's
// `short` (fleet-qualified under `all`), and no row is dropped: the header
// is added, nothing it names is taken away.
//
// WHICH fleet a row belongs to is the SERVER's stamp (`fleet`, fold F5 — a
// fleet identity is its own fleet, a bot's is read off its alias through
// inventory.fleet_of, a human's is null because it is a participant of every
// room). The page had a third spelling of that rule and no longer parses an
// alias at all. Nothing here asks which room the page thinks it is in —
// whether the READ spans fleets is what decides the grouping, and only the
// `all` read ever does.
function renderFleet(env) {
  const el = $("fleet");
  if (renderState(el, env,
                  { idleWhenEmpty: (d) => !d.identities.length })) return;
  const rows = env.data.identities;
  const fleets = new Set(rows.map((a) => a.fleet).filter(Boolean));
  if (fleets.size < 2) { el.innerHTML = rows.map(railRow).join(""); return; }
  el.innerHTML = groupBy(rows, (a) => a.fleet || "")
    .sort((x, y) => (y[0].last_seen || "").localeCompare(
      x[0].last_seen || ""))
    .map((members) => {
      const f = members[0].fleet;
      // the count is BOTS — the fleet's own identity row is not one of them
      // — and the un-fleeted group counts HUMANS, which is what it holds
      const n = members.filter((m) => m.kind === "actor").length;
      const count = `${n} ${f ? "bot" : "human"}${n === 1 ? "" : "s"}`;
      return `<div class="rail-head"><b>${esc(f || "humans")}</b>`
        + `<small>${esc(count)}</small></div>`
        + members.map(railRow).join("");
    }).join("");
}

// The header in the operator's language (chunk L, #1479): the host's totals
// — bots, working now, needing you, overdue — WITH the disclosures the cards
// carry. The page used to sum the fleet rows itself and dropped both of them
// on the way (fold F5): the unconfirmed share of the bot count, and a live
// poll that was degraded or gone. The totals are the server's now, summed in
// one place; this renders them and adds nothing.
function renderHeader(env) {
  const el = $("fleet-totals");
  if (!el) return;
  if (!env || env.state !== "ok" || !env.data || !env.data.totals) {
    el.innerHTML = `<b>—</b> bots`;   // §16: never a zero for an absent source
    return;
  }
  const t = env.data.totals;
  if (!t.fleets) {
    el.innerHTML = `<span class="dim">no fleet recorded</span>`;
    return;
  }
  el.innerHTML = `<b>${t.bots}</b> bots`
    + (t.provisional ? ` <small title="actors no registry scan has confirmed`
        + ` yet — a mistyped dispatch target mints one">(${t.provisional}`
        + ` unconfirmed)</small>` : "")
    + ` · <b>${t.working}</b> working`
    + ` · <b class="${t.attention ? "warn" : ""}">${t.attention}</b> need you`
    + ` · <b class="${t.overdue ? "warn" : ""}">${t.overdue}</b> overdue`
    + (t.live_poll !== "ok"
       ? ` <small class="warn" title="the pane sampler is not answering — the`
         + ` working count is the recorded half only">live poll`
         + ` ${esc(t.live_poll)}</small>` : "");
}

// The machinery rail's host line — from the OVERVIEW's host block, the same
// envelope the host card renders (fold F6: the rail read /api/summary and the
// card /api/overview, so the two could disagree by an instant, and the value
// also sat in a module variable nothing else read). `null` blanks it, for the
// stream's source-failure path: a stale number is worse than none (§16).
function renderHostFacts(env) {
  const el = $("host-facts");
  if (!el) return;
  const h = env && env.state === "ok" && env.data ? env.data.host : null;
  el.textContent = h
    ? `host ingest ${h.last_ingest_at ? ago(h.last_ingest_at) : "never"}`
      + ` · rows ${h.rows} · spool ${h.spool_files}`
    : "host ingest — · rows — · spool —";
}

function renderSummary(env) {
  const beat = $("beat"), label = $("beat-label");
  if (!env || env.state !== "ok") {
    beat.className = "dot";
    label.textContent = env ? `source ${env.state}` : "view daemon unreachable";
    return;
  }
  const d = env.data, prov = env.provenance;
  // the recorder's numbers moved off the header (chunk L, #1479): the header
  // answers "how is my fleet", not "how is the recorder" — the host card and
  // the machinery rail keep the latter (the rail's line is renderHostFacts,
  // off the same overview envelope as the card)
  const fresh = prov.last_ingest_at
    && (Date.now() - Date.parse(prov.last_ingest_at)) < 12e4;
  beat.className = "dot " + (d.daemon_serving ? (fresh ? "ok" : "warn") : "");
  label.textContent = d.daemon_serving
    ? (fresh ? "recording" : "recorder up, quiet") : "recorder DOWN";
}

// The two-fleet overview strip (U3): one card per fleet, one host card.
// Every figure is a server fact through the door that defines it; a
// figure the server could not source arrives null + a reason and renders
// as "unknown (reason)", never as a zero (§16). A fleet card is the tab
// switch when the host has a fleet dimension.
const OV_PRESENCE_ORDER = ["working", "idle", "stale", "unknown", "sampling",
                           "down"];
function ovPresence(p) {
  const c = p.counts || {};
  const parts = OV_PRESENCE_ORDER.filter((s) => c[s] > 0)
    .map((s) => `<span class="pres pres-${s}">${c[s]} ${s}</span>`);
  if (p.live_poll !== "ok") {
    parts.push(`<span class="ov-warn">live poll ${esc(p.live_poll)}</span>`);
  }
  return parts.length ? parts.join(" ") : `<span>no presence recorded</span>`;
}
function ovNum(n, word, bad = true) {
  if (n === null || n === undefined) return `<span class="ov-warn">${esc(word)} unknown</span>`;
  return `<span class="${n > 0 && bad ? "ov-bad" : ""}">${n} ${esc(word)}</span>`;
}
function renderOverview(env) {
  const el = $("overview");
  if (!el) return;
  if (!env || env.state !== "ok") {
    el.innerHTML = stateBlock(env ? env.state : "disconnected",
                              env && env.provenance, env && env.remediation);
    return;
  }
  const d = env.data;
  const dimension = fleets.length >= 2;
  const cards = d.fleets.map((f) => `
    <div class="ov-card ${dimension ? "pick" : ""} ${
        dimension && f.alias === currentFleet ? "on" : ""}"
         data-fleet="${esc(f.alias)}" ${dimension ? 'role="button" tabindex="0"' : ""}
         title="${esc(f.alias)}${dimension ? " — open this room" : ""}">
      <div class="ov-head"><b>${esc(f.alias)}</b>
        <span>${f.bots} bot${f.bots === 1 ? "" : "s"}${
          f.provisional ? ` <small title="actors no registry scan has confirmed yet — a mistyped dispatch target mints one">(${f.provisional} unconfirmed)</small>` : ""}</span>
        <small>${esc(f.capture)} capture</small></div>
      <div class="ov-line">${ovPresence(f.presence)}</div>
      <div class="ov-line">${ovNum(f.open, "open", false)} ·
        ${ovNum(f.attention, "need you")} · ${ovNum(f.overdue, "overdue")} ·
        <span title="${esc(f.orphaned_reason || "")}">${
          ovNum(f.orphaned, "orphaned")}</span></div>
      <div class="ov-line">
        <span>${f.newest_report_at
          ? `last report ${esc(ago(f.newest_report_at))}` : "no reports"}${
          f.reports_24h ? ` · ${f.reports_24h} in 24h` : ""}${
          f.unacked === null || f.unacked === undefined
            ? ` · <span class="ov-warn" title="${esc(f.unacked_reason || "")}">no ack recorded</span>`
            : ` · ${ovNum(f.unacked, "unacked")} <small title="${esc(f.acked_at || "")}">acked by ${
                esc(f.acked_by)} ${esc(ago(f.acked_at))}</small>`}</span> ·
        <span>active ${esc(ago(f.last_activity_at))}</span></div>
    </div>`).join("");
  const h = d.host;
  // the lag STATE is the API's (ingest_lag_state) — the page only renders it
  const lag = h.ingest_lag_state === "none"
    ? `<span class="ov-warn">nothing ingested yet</span>`
    : `<span class="${h.ingest_lag_state === "warn" ? "ov-warn" : ""}">ingest lag ${
        h.ingest_lag_s | 0}s</span>`;
  const sm = h.samples;
  const facet = (k, label, fmt) => sm && sm[k] ? `${label} ${esc(fmt(sm[k].value))}` : null;
  const facets = sm ? [
    // the probe records the load TRIPLET as an object ({one, five, fifteen});
    // a scalar (an older or a foreign emitter) renders as itself
    facet("host.load", "load", (v) => v && typeof v === "object"
      ? [v.one, v.five, v.fifteen].filter((x) => x !== undefined).join(" ")
      : String(v)),
    facet("host.mem_available_mb", "free RAM", (v) => `${Math.round(v / 1024 * 10) / 10} GB`),
    facet("host.disk_free_gb", "free disk", (v) => `${v} GB`),
    facet("host.undervoltage", "undervoltage", (v) => v ? "YES" : "no"),
    facet("host.thermal_flags", "thermal", (v) => String(v)),
  ].filter(Boolean).join(" · ") : `<span class="ov-warn" title="arm the plane-host-probe host timer (PLANE_EMIT_ENABLED=1) to record load, RAM, disk and thermal facets">host probe not armed</span>`;
  const spool = h.spool_state === "unreadable"
    ? `<span class="ov-bad">spool unreadable</span>`
    : ovNum(h.spool_files, "spooled");
  const host = `
    <div class="ov-card ov-host" title="the host's recorder — every fleet on this host writes here">
      <div class="ov-head"><b>host</b>
        <span class="${h.daemon_serving ? "ov-ok" : "ov-bad"}">${
          h.daemon_serving ? "recorder up" : "recorder DOWN"}</span>
        <small>${h.rows} rows</small></div>
      <div class="ov-line">${spool} · ${lag}</div>
      <div class="ov-line">${facets}</div>
      ${d.capture_config === "malformed"
        ? `<div class="ov-line ov-bad">capture.json malformed — policies shown are defaults</div>` : ""}
    </div>`;
  el.innerHTML = cards + host;
}

$("channel").addEventListener("click", (e) => {
  const b = e.target.closest("button.more");
  if (!b) return;
  // by STRUCTURE, not by sibling order: anything rendered between a body and
  // its button (a delivery line, a tag) breaks a walk to the button's
  // previous sibling, and breaks it SILENTLY — the toggle just stops working
  const msg = b.closest(".msg");
  const body = msg && msg.querySelector(".body");
  if (!body) return;
  const open = body.classList.toggle("open");
  b.textContent = open ? "show less" : "show more";
  if (msg.dataset.msgId) {
    if (open) openBodies.add(msg.dataset.msgId);
    else openBodies.delete(msg.dataset.msgId);
  }
});

// ONE delegated listener for the strip's cards (re-rendered on every refresh;
// per-card listeners were re-attached each time — simplify lens)
$("overview").addEventListener("click", (e) => {
  const c = e.target.closest(".ov-card.pick");
  if (c) pickFleet(c.dataset.fleet);
});
$("overview").addEventListener("keydown", (e) => {
  const c = e.target.closest(".ov-card.pick");
  if (c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pickFleet(c.dataset.fleet); }
});

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
  if (!fleetsSeen) {
    // The FIRST paint learns the fleet dimension before it fetches a board:
    // every per-fleet board below is fetched IN the room, so the room must
    // be known first — the old flow fetched the firehose, discovered the
    // fleets from the roster, and refetched (one wasted round trip and one
    // flash of the wrong room on every load).
    adoptFleets(await jget("/api/fleets"));
    if (gen !== generation) return;
  }
  const q = fleetQuery();
  const [ch, tk, fl, sm, ov] = await Promise.all([
    jget(channelUrl()), jget("/api/tasks" + q),
    jget("/api/identities" + q), jget("/api/summary"),
    jget("/api/overview"),
  ]);
  if (gen !== generation) return;
  adoptFleets(ov);   // the overview carries the fleet list + default: one door
  renderHeader(ov); renderHostFacts(ov);
  renderChannel(ch); renderTasks(tk); renderFleet(fl); renderSummary(sm);
  renderFleetTabs();
  renderOverview(ov);   // after the tabs: the strip highlights the pick
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
    // silence (the first version pushed these frames to no listener). The
    // header and the host line are CLEARED, not left standing: the numbers
    // they hold came from a source that has stopped answering (§16).
    try {
      const env = JSON.parse(ev.data);
      renderSummary(env);
      renderHeader(null);
      renderHostFacts(null);
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
    .replace(/\x1b\[[0-9;?]*[@-ln-~]/g, "");   // CSI non-SGR (keeps m)
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
let currentFleet = null;   // null = auto (single fleet, or no pick yet)
let gridTimer = null;
let focusTimer = null;

// The fleet DIMENSION (U1): the host's fleets come from /api/fleets — the
// registry's fleet identities — never from the roster rail, whose LIMIT-200
// last-seen window silently dropped a quiet fleet's tab on a two-fleet host.
let fleets = [];          // [{alias, bots, last_comm_at, ...}], alphabetical
let fleetsSeen = false;   // the first /api/fleets answer has landed
const PICK_KEY = "plane.fleet";

function loadPick() {   // per-viewer convenience — may be absent or throw
  try { return localStorage.getItem(PICK_KEY); } catch { return null; }
}
function savePick(f) {
  try { localStorage.setItem(PICK_KEY, f); } catch { /* not persisted */ }
}

// Adopt a /api/fleets answer: keep the list, and settle `currentFleet` —
// the viewer's remembered pick when it still names a fleet (or "all"),
// else the server's default (the room that moved most recently), else the
// first fleet. One fleet = no dimension at all (null, no tabs).
function adoptFleets(env) {
  if (!env || env.state !== "ok" || !env.data) return;
  fleetsSeen = true;
  fleets = env.data.fleets;   // the server's order (ORDER BY alias) is the tab order
  const names = fleets.map((f) => f.alias);
  if (names.length < 2) { currentFleet = null; return; }
  if (currentFleet && (currentFleet === "all" || names.includes(currentFleet))) return;
  const stored = loadPick();
  currentFleet = stored && (stored === "all" || names.includes(stored))
    ? stored : (env.data.default || names[0]);
}

function fleetQuery(sep = "?") {   // the per-fleet routes' `fleet=` axis
  return currentFleet && currentFleet !== "all"
    ? `${sep}fleet=${encodeURIComponent(currentFleet)}` : "";
}

function channelUrl() {
  return `/api/channel?limit=120${fleetQuery("&")}`;
}

function renderFleetTabs() {
  const el = $("fleet-tabs");
  if (fleets.length < 2) { el.innerHTML = ""; return; }
  // Per-team rooms are the DEFAULT (operator ruling): a fleet tab is always
  // selected; the merged host view is the explicit last resort.
  el.innerHTML = [...fleets.map((f) => f.alias), "all"].map((f) => {
    const meta = fleets.find((x) => x.alias === f);
    const n = meta ? `<small>${esc(meta.bots)}</small>` : "<small>host</small>";
    return `<button class="pill ghost ${f === currentFleet ? "on" : ""}"`
      + ` data-fleet="${esc(f)}" type="button">${esc(f)} ${n}</button>`;
  }).join("");
  el.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => pickFleet(b.dataset.fleet)));
}

// ONE pick path — the tab row and the overview strip's cards (U3) both
// land here, so a card click can never drift from a tab click.
function pickFleet(f) {
  currentFleet = f;
  savePick(currentFleet);
  renderFleetTabs();             // instant highlight (the strip re-renders with the boards)
  if (currentView === "fleet") pollFleet();   // the tab follows the pick
  if (currentView === "grid") pollGrid();     // so does the grid
  refreshBoards();               // guarded path (generation stale-guard)
  if (!$("search-results").hidden) {
    // active search: re-fire in the NEW room — otherwise the visible
    // hits stay scoped to the old room under the new tab's highlight
    $("search").dispatchEvent(new Event("input"));
  }
}

const STATUS_DOT = { up: "live", down: "", sampling: "warn" };
const STATUS_NOTE = { down: "session down", sampling: "sampling…" };

// Presence is the DERIVED verdict (working/idle/down/stale/unknown/
// sampling) the /api/presence join computes — the plain-language word
// #1361 is about, not the raw liveness poll. The card badges it; the raw
// status still drives the terminal-frame styling below.
const PRESENCE_WORD = {
  working: "working", idle: "idle", down: "down", stale: "stale",
  unknown: "unknown", sampling: "sampling…",
};

function renderPaneCard(el, p) {
  el.className = "pane-card" + (p.status === "up" ? "" : ` s-${p.status}`);
  el._lines = p.lines;   // JS property, not a multi-KB DOM attribute
  const pr = p.presence;
  const badge = pr
    ? `<span class="pres pres-${pr}">${esc(PRESENCE_WORD[pr] || pr)}` +
      (pr === "working" && p.marker_age_s != null
        ? ` ${esc(p.marker_age_s)}s` : "") + `</span>`
    : "";
  el.innerHTML = `
    <div class="p-head"><span class="dot ${STATUS_DOT[p.status] || ""}"></span>
      ${badge}
      <b>${esc(p.bot)}</b><span class="tag">${esc(p.fleet)}</span>
      <small class="age">${p.captured_ago_s != null
        ? esc(p.captured_ago_s) + "s" : "—"}</small></div>
    <pre>${ansiToHtml(p.lines)}</pre>
    ${STATUS_NOTE[p.status]
      ? `<div class="dead-note">${esc(STATUS_NOTE[p.status])}</div>` : ""}`;
}

function paneCard(p) {
  const el = document.createElement("div");
  el.dataset.bot = p.bot;
  el.dataset.fleet = p.fleet;
  el.tabIndex = 0;                       // §16: keyboard-navigable
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", `focus ${p.fleet}/${p.bot}`);
  const open = () => openFocus(p.bot, p.fleet);
  el.addEventListener("click", open);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
  });
  renderPaneCard(el, p);
  return el;
}

// Keyed render (§16 preserved-focus + text selection — the class the channel
// was pinned against): unchanged frames keep their DOM node so an operator
// can actually copy a bot's terminal out.
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
  const existing = new Map(
    [...el.querySelectorAll(".pane-card")].map(
      (n) => [`${n.dataset.fleet}/${n.dataset.bot}`, n]));
  const frag = document.createDocumentFragment();
  if (env.data.sampler_running === false) {
    // The one signal separating "sampler crashed, frames aging" from
    // "all well" — surfaced, never silent (§16).
    const warn = document.createElement("div");
    warn.className = "panel-state st-unreadable";
    warn.innerHTML = `<div class="label">sampler stopped — frames are` +
      ` aging</div><div class="remedy">restart the view daemon</div>`;
    frag.appendChild(warn);
  }
  for (const p of panes) {
    const prev = existing.get(`${p.fleet}/${p.bot}`);
    if (prev && prev._lines === p.lines) {
      const age = prev.querySelector(".age");
      if (age) age.textContent = p.captured_ago_s != null
        ? p.captured_ago_s + "s" : "—";
      frag.appendChild(prev);
    } else if (prev) {
      renderPaneCard(prev, p);
      frag.appendChild(prev);
    } else {
      frag.appendChild(paneCard(p));
    }
  }
  el.replaceChildren(frag);
}

// Presence rides alongside the frames: /api/grid is the terminal thumbnails
// (liveness + lines), /api/presence is the derived verdict per bot. Merge by
// alias so each card carries its working/idle/down word, and roll the counts
// into the header strip. A presence-fetch failure never blanks the grid —
// the frames still render; the badges just go absent (the recorded half is
// the degradable one).
async function pollGrid() {
  if (currentView !== "grid") return;
  const q = fleetQuery();   // the tab's panes and verdicts (U4/U1)
  const [gridEnv, presEnv] = await Promise.all([
    jget("/api/grid" + q), jget("/api/presence" + q).catch(() => null),
  ]);
  const byAlias = {};
  if (presEnv && presEnv.data) {
    for (const b of presEnv.data.bots) byAlias[b.alias] = b;
    // the recorded half can fail while the live half still answers — the
    // server discloses it (state !== ok / recorded_unavailable); surface
    // that, never swallow it into a badge-less grid with no hint
    renderPresenceStrip(presEnv.data.counts,
                        presEnv.state !== "ok" || presEnv.data.recorded_unavailable);
  }
  if (gridEnv && gridEnv.data && gridEnv.data.panes) {
    for (const p of gridEnv.data.panes) {
      const pr = byAlias[`bot:${p.fleet}/${p.bot}`];
      if (pr) { p.presence = pr.presence; p.marker_age_s = pr.marker_age_s; }
    }
  }
  renderGrid(gridEnv);
}

const PRESENCE_ORDER = ["working", "idle", "stale", "unknown", "sampling",
                        "down"];

function renderPresenceStrip(counts, recordedDown) {
  const el = $("presence-strip");
  if (!el || !counts) return;
  // only the states actually present, in a stable order — a zero is real
  // (presence_counts always carries every key) but a header of six zeros is
  // noise; the trust view is where the full breakdown lives
  const parts = PRESENCE_ORDER
    .filter((s) => counts[s] > 0)
    .map((s) => `<span class="pres pres-${s}">${counts[s]} ${s}</span>`);
  if (parts.length) {
    // whose counts these are: the room's under a tab, the host's under
    // "all" — the header is otherwise host-level, so say which
    parts.unshift(`<span>${esc(currentFleet && currentFleet !== "all"
      ? currentFleet : "host")}</span>`);
  }
  if (recordedDown) {
    // the activity half is dark — say so, never a badge-less grid with no
    // hint (the disclosure the server sent must reach the operator)
    parts.push(`<span class="pres pres-stale">activity half unavailable</span>`);
  }
  el.innerHTML = parts.join(" ");
  el.hidden = parts.length === 0;
}

function setView(view) {
  currentView = view;
  $("channel").hidden = view !== "channel";
  $("search-results").hidden = true;   // any view switch closes results
  if (view !== "channel") $("search").value = "";
  $("grid").hidden = view !== "grid";
  $("trust").hidden = view !== "trust";
  $("fleet-room").hidden = view !== "fleet";
  if (view === "fleet") pollFleet();
  document.querySelectorAll("#view-nav button").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === view));
  clearInterval(gridTimer);
  if (view === "grid") {
    renderState($("grid"), { state: "loading" });
    pollGrid();
    gridTimer = setInterval(pollGrid, 5000);
  }
  clearInterval(trustTimer);
  if (view === "trust") {
    renderState($("trust"), { state: "loading" });
    pollTrust();
    // A one-shot snapshot froze green while events quarantined behind it
    // (external review): filesystem-only changes add no ledger row, so SSE
    // cannot carry them — a bounded poll while the tab is visible is the
    // floor. 15s: trust is a slow surface; ages re-render each pass.
    trustTimer = setInterval(pollTrust, 15000);
  }
}

let trustTimer = null;
let trustGen = 0;   // monotonic request generation — the view check alone
                    // loses the ABA race (leave trust, return, and an OLD
                    // in-flight response lands while the view is trust
                    // again, overwriting the newer state — external round
                    // 2, probed with deferred responses)
async function pollTrust() {
  if (currentView !== "trust" || document.hidden) return;
  const gen = ++trustGen;
  const env = await jget("/api/trust");
  if (gen !== trustGen || currentView !== "trust" || document.hidden) return;
  renderTrust(env);
}
document.querySelectorAll("#view-nav button").forEach((b) =>
  b.addEventListener("click", () => setView(b.dataset.view)));

function openFocus(bot, fleet) {
  if (!bot) { closeFocus(); return; }   // never a titleless empty void
  clearInterval(focusTimer);   // no prior timer bleeds into this overlay
  clearInterval(gridTimer);    // pause the full-grid poll behind the overlay
  $("focus-overlay").hidden = false;
  $("focus-title").textContent = fleet ? `${bot} · ${fleet}` : bot;
  $("focus-title").dataset.bot = bot;
  $("focus-title").dataset.fleet = fleet || "";
  $("focus-pane").innerHTML = stateBlock("loading", null, null,
    { label: `opening ${bot}…`, detail: "" });   // never blank while fetching
  const q = `/api/grid?focus=${encodeURIComponent(bot)}`
    + (fleet ? `&fleet=${encodeURIComponent(fleet)}` : "");
  let lastSig = null;
  const tick = async () => {
    const env = await jget(q);   // server ships ONLY this pane
    if ($("focus-title").dataset.bot !== bot) return;  // overlay moved on
    if (!env || env.state !== "ok") {
      $("focus-pane").innerHTML = stateBlock(
        env ? env.state : "disconnected", env && env.provenance,
        env && env.remediation);
      lastSig = null;
      return;
    }
    const pane = (env.data.panes || [])[0];
    if (!pane) {   // ghost focus: a state, never "opening…" forever
      $("focus-pane").innerHTML = stateBlock("idle", null, null,
        { label: `no such pane: ${bot}`, detail: "" });
      lastSig = null;
      return;
    }
    // ONE signature over status+frame: an unchanged frame never re-renders
    // (preserved selection — including on a DOWN pane, where copying the
    // last screen matters most), and a status flip always does.
    const sig = pane.status + "\u0000" + pane.lines;
    if (sig === lastSig) return;
    lastSig = sig;
    $("focus-pane").innerHTML = (pane.status === "up" ? "" : stateBlock(
      "idle", null, null, { label: STATUS_NOTE[pane.status] || pane.status,
                            detail: "" }))
      + ansiToHtml(pane.lines);
  };
  tick();
  focusTimer = setInterval(tick, 1000);
}
function closeFocus() {
  $("focus-overlay").hidden = true;
  clearInterval(focusTimer);
  if (currentView === "grid") {   // resume the grid poll
    gridTimer = setInterval(pollGrid, 5000);
    pollGrid();
  }
}
$("focus-close").addEventListener("click", closeFocus);
$("focus-overlay").addEventListener("click", (e) => {
  if (e.target.id === "focus-overlay") closeFocus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("focus-overlay").hidden) closeFocus();
});
document.addEventListener("visibilitychange", () => {
  // A hidden tab's open overlay must not pin the server at 1s focus cadence
  // (each tick renews the 30s TTL) — §14 honesty (reviewer finding).
  if (document.hidden) {
    clearInterval(focusTimer); clearInterval(gridTimer);
    clearInterval(trustTimer);
  } else if (!$("focus-overlay").hidden && $("focus-title").dataset.bot) {
    openFocus($("focus-title").dataset.bot,
              $("focus-title").dataset.fleet || null);
  } else if (currentView === "grid") {
    gridTimer = setInterval(pollGrid, 5000); pollGrid();
  } else if (currentView === "trust") {
    trustTimer = setInterval(pollTrust, 15000); pollTrust();
  }
});

// ---------------------------------------------------------------------------
// Trust/gaps surface + channel search (Phase-4 final chunk)
// ---------------------------------------------------------------------------

function trustNum(n, nonzero = "trust-bad") {
  return `<span class="trust-num ${n === 0 ? "trust-ok" : nonzero}">${n}</span>`;
}

function renderTrust(env) {
  const el = $("trust");
  if (renderState(el, env)) return;
  const d = env.data;
  const emitters = d.emitters.map((e) => `
    <div class="trust-row"><b>${esc(e.emitter)}</b>
      <span>${esc(ago(e.last_at))}</span>
      <small>${e.events} events</small></div>`).join("")
    || `<div class="trust-row"><small>no emitters have fired yet</small></div>`;
  const fleets = d.fleets.map((f) => `
    <div class="trust-row"><b>${esc(f.fleet)}</b>
      <span class="tag">${esc(f.capture)} capture</span>
      <span>${f.last_comm_at ? esc(ago(f.last_comm_at)) : "no comms ever"}</span>
      <small>${f.comms} messages${f.note ? " · " + esc(f.note) : ""}</small>
    </div>`).join("");
  const reasons = d.quarantine_reasons.map((r) =>
    `<div class="reason">${esc(r.event)} — ${esc(r.reason)}</div>`).join("");
  el.innerHTML = `
    <div class="trust-block"><h3>gaps — what the recorder refused</h3>
      ${d.quarantine_state === "unreadable"
        ? `<div class="trust-row trust-bad"><b>quarantine dir unreadable</b>
             <small>cannot count refusals — this is a gap in the gap
             counter, not a zero</small></div>` : ""}
      <div class="trust-row">${trustNum(d.quarantined)}
        <b>quarantined events</b>
        <small>arrived and were refused — each is a recording gap</small></div>
      ${reasons}
      ${d.spool_state === "unreadable"
        ? `<div class="trust-row trust-bad"><b>spool unreadable</b>
             <small>cannot count pending — a gap, not a zero</small></div>`
        : `<div class="trust-row">${trustNum(d.spool_pending)}
             <b>spooled, not yet ingested</b>
             <small>${d.spool_oldest_at
               ? "oldest " + esc(ago(d.spool_oldest_at)) : ""}</small></div>`}
    </div>
    <div class="trust-block"><h3>doors — per-emitter freshness</h3>
      ${emitters}
    </div>
    <div class="trust-block"><h3>fleets — capture policy + liveness</h3>
      ${d.capture_config === "malformed"
        ? `<div class="trust-row trust-bad"><b>capture.json is malformed</b>
             <small>policy unreadable — modes shown are defaults</small></div>`
        : ""}
      ${fleets}
    </div>
    <div class="trust-block"><h3>identities</h3>
      <div class="trust-row">${trustNum(d.provisional_identities, "trust-warn")}
        <b>provisional identities</b>
        <small>lazily minted, unconfirmed by the registry — Phase 2b
        confirms these</small></div>
    </div>`;
}

function renderSearch(env) {
  const el = $("search-results");
  if (renderState(el, env)) return;
  const hits = env.data.results;
  // §11 completeness: say what the search CANNOT see — a metadata-capture
  // room answering "no matches" alone is a false idle.
  const un = Number(env.data.unsearchable);   // server-typed ints; belt
  const part = Number(env.data.partially_indexed);
  const unsearchable = (un > 0 || part > 0)
    ? `<div class="panel-state st-idle"><div class="detail">`
      + [un > 0 ? `${un} message${un === 1 ? " has" : "s have"} no recorded`
                  + ` words here (metadata capture, or sent body-less)` : "",
         part > 0 ? `${part} message${part === 1 ? " is" : "s are"} only`
                    + ` partially indexed (body truncated at capture — a`
                    + ` term past the cap is unfindable)` : ""]
        .filter(Boolean).join(" · ")
      + `</div></div>` : "";
  if (!hits.length) {
    el.innerHTML = stateBlock("idle", null, null,
      { label: `no matches for “${env.data.query}”`, detail: "" })
      + unsearchable;  // stateBlock escapes its label
    return;
  }
  el.innerHTML = hits.map((h) => {
    // esc() the WHOLE snippet, then swap the server's PER-REQUEST random
    // markers for <mark> — markup never rides in from bot-authored text,
    // and a body carrying literal marker bytes cannot forge a highlight
    // (it cannot predict the token).
    const snip = esc(h.snip)
      .replaceAll(env.data.marker_open, "<mark>")
      .replaceAll(env.data.marker_close, "</mark>");
    return `<div class="hit">
      <b>${esc(h.sender_short)}</b>
      <span class="to">→ ${esc(h.recipient_short || "—")}</span>
      <small> · ${esc(ago(h.occurred_at))}${h.work_item_id
        ? " · work item" : ""}</small>
      <div class="snip">${snip}</div></div>`;
  }).join("") + unsearchable;
}

let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("search").value.trim();
  if (!q) {
    $("search-results").hidden = true;
    $("channel").hidden = false;
    return;
  }
  searchTimer = setTimeout(async () => {
    const fleetAtFire = currentFleet;   // room captured at fetch time —
    const f = fleetAtFire && fleetAtFire !== "all"
      ? `&fleet=${encodeURIComponent(fleetAtFire)}` : "";
    const env = await jget(`/api/search?q=${encodeURIComponent(q)}${f}`);
    // stale if the QUERY or the ROOM moved on (two rapid tab clicks raced
    // the old room's hits under the new tab — round 2, probed)
    if ($("search").value.trim() !== q || currentFleet !== fleetAtFire) return;
    $("channel").hidden = true;
    $("search-results").hidden = false;
    renderSearch(env);
  }, 250);
});

// Bootstrap LAST — after every top-level `let` (currentFleet, currentView…)
// has initialized. Placed mid-file it read those bindings in their temporal
// dead zone and threw on first load, freezing the page at its loading markup.
$("focus-overlay").hidden = true;   // a restored-open modal never survives a load
["channel", "tasks", "attention", "fleet"].forEach((id) =>
  renderState($(id), { state: "loading" }));
refreshBoards();
openStream();


// --- fleet inventory + per-bot equipment (#1405) ---------------------------
// The content the v1 rail demoted (208 library items flooding the participant
// list), given its own room: what is active on the fleet, and what each bot
// is composed of — "a viz over the bot directory / composed config". Pure
// read over the registry doors; alias-first; every value esc()'d.
const EQUIP_ORDER = ["expertise", "skills", "mcp", "integrations", "guardrails",
  "protocols", "resources", "lessons", "principles", "post_actions", "tools",
  "plugins", "voice"];

async function pollFleet() {
  if (currentView !== "fleet") return;
  renderState($("fleet-room"), { state: "loading" });
  // honor the fleet picker (the same fleet= the channel/search use); with
  // "all", every fleet the host records — cross-fleet twins come back
  // fleet-qualified from the server (gauntlet). The org tree FOLLOWS the
  // tab (U4): with a tab picked the server never falls back to its
  // first-alphabetical default.
  const f = fleetQuery();
  // org + utilization ride alongside; either failing never blanks the
  // inventory (the same opposite-failure-modes rule as grid + presence)
  const [inv, org, util] = await Promise.all([
    jget("/api/inventory" + f),
    jget("/api/org" + f).catch(() => null),
    jget("/api/utilization" + f).catch(() => null),
  ]);
  if (inv && inv.data && util && util.data) {
    const by = {};
    for (const u of util.data) by[u.alias] = u;
    for (const b of inv.data.bots) { const u = by[b.alias]; if (u) b.util = u; }
  }
  renderInventory(inv, org);
}

function orgNode(n) {
  return `<li${n.cycle ? ' class="cycle"' : ""}>${esc(n.bot)}${n.cycle ? " <small>(cycle)</small>" : ""}` +
    (n.reports.length ? `<ul>${n.reports.map(orgNode).join("")}</ul>` : "") + `</li>`;
}

function countsLine(counts) {
  return EQUIP_ORDER.filter((k) => counts[k] > 0)
    .map((k) => `${counts[k]} ${k.replace("_", " ")}`).join(" · ");
}

function renderInventory(env, orgEnv) {
  const el = $("fleet-room");
  if (!env || env.state !== "ok") {
    el.innerHTML = stateBlock(env ? env.state : "disconnected",
                              env && env.provenance, env && env.remediation);
    return;
  }
  const d = env.data;
  if (!d.bots.length) {
    el.innerHTML = stateBlock("idle", env.provenance,
      "no bots keyframed yet — run `claudlobby --fleet <name> generate`");
    return;
  }
  const bots = d.bots.map((b) => `
    <div class="bot-card" data-alias="${esc(b.alias)}" tabindex="0" role="button"
         aria-label="equipment of ${esc(b.short)}">
      <div class="bc-head"><b>${esc(b.short)}</b>
        <small>${esc(b.model || "")}${b.permissions_mode
          ? ` · ${esc(b.permissions_mode)}` : ""}</small></div>
      <div class="bc-counts">${esc(countsLine(b.counts)) || "no equipment recorded"}</div>
      ${b.util ? `<div class="bc-util">busy ${esc(b.util.busy_pct_24h)}% today · ${esc(b.util.busy_pct_7d)}% 7d</div>` : ""}
      ${b.reports_to ? `<div class="bc-org">reports to ${esc(b.reports_to)}</div>` : ""}
      ${b.manages && b.manages.length
        ? `<div class="bc-org">manages ${esc(b.manages.join(", "))}</div>` : ""}
    </div>`).join("");
  const projects = d.projects.length ? d.projects.map((p) => `
    <div class="proj-row"><b>${esc(p.title || p.key)}</b>
      <small>${esc(p.tier || "")}${p.repos.length
        ? ` · ${esc(p.repos.length)} repo${p.repos.length === 1 ? "" : "s"}` : ""}</small></div>`).join("")
    : `<div class="dim">no projects keyframed</div>`;
  // library grouped by category, collapsed — 208 rows is a wall, not a story
  const byCat = {};
  for (const it of d.library) (byCat[it.category] = byCat[it.category] || []).push(it);
  const library = Object.keys(byCat).sort().map((cat) => {
    const items = byCat[cat];
    const inUse = items.filter((i) => i.used_by.length).length;
    return `<details class="lib-group"><summary>${esc(cat)}
        <small>${items.length} · ${inUse} in use</small></summary>
      ${items.map((i) => `<div class="lib-row${i.used_by.length ? "" : " unused"}">
        <span>${esc(i.name)}${i.tier && i.tier !== "shared" ? ` <small>${esc(i.tier)}</small>` : ""}</span>
        <small>${i.shadowed ? "shadowed by an overlay copy"
          : i.used_by.length ? esc(i.used_by.join(", ")) : "unused"}</small>
      </div>`).join("")}</details>`;
  }).join("");
  const orgHtml = orgEnv && orgEnv.state === "ok" && orgEnv.data
    ? `<details class="org" open><summary>org · ${esc(orgEnv.data.fleet)}${orgEnv.data.manager ? ` · manager ${esc(orgEnv.data.manager)}` : ""}${
        orgEnv.data.cycles.length ? ` · <b>${orgEnv.data.cycles.length} reporting cycle(s)</b>` : ""}${
        orgEnv.data.malformed_edges ? ` · <b>${esc(orgEnv.data.malformed_edges)} malformed edge(s) skipped${
          (orgEnv.data.malformed_bots || []).length ? `: ${esc(orgEnv.data.malformed_bots.join(", "))}` : ""}</b>` : ""}${
        // the org tree is ONE fleet's; under the "all" pick on a multi-fleet
        // host the inventory shows every fleet — say which tree this is
        (orgEnv.data.available || []).length > 1 && (!currentFleet || currentFleet === "all")
          ? ` · <small>1 of ${orgEnv.data.available.length} fleets — pick a fleet for its tree</small>` : ""}</summary>
        <ul class="org-tree">${orgEnv.data.roots.map(orgNode).join("")}</ul></details>`
    : "";
  el.innerHTML = `
    <div id="equip-detail" hidden></div>
    <div class="inv-head">${d.counts.bots} bots · ${d.counts.projects} projects ·
      ${d.counts.library_in_use}/${d.counts.library} library items in use</div>
    ${orgHtml}
    <div class="bot-grid">${bots}</div>
    <h3>projects</h3>${projects}
    <h3>library</h3>${library}`;
  el.querySelectorAll(".bot-card").forEach((c) => {
    const open = () => openEquipment(c.dataset.alias);
    c.addEventListener("click", open);
    c.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });
}

async function openEquipment(alias) {
  const box = $("equip-detail");
  if (!box) return;
  box.hidden = false;
  renderState(box, { state: "loading" });
  const env = await jget(`/api/equipment?alias=${encodeURIComponent(alias)}`);
  if (!env || env.state !== "ok") {
    box.innerHTML = stateBlock(env ? env.state : "disconnected",
                               env && env.provenance, env && env.remediation);
    return;
  }
  const b = env.data;
  const chips = (arr) => (arr || []).map((x) => `<span class="chip">${esc(String(x))}</span>`).join("");
  const eq = EQUIP_ORDER.filter((k) => b.equipment[k] && (Array.isArray(b.equipment[k])
      ? b.equipment[k].length : true))
    .map((k) => `<div class="eq-row"><b>${esc(k.replace("_", " "))}</b>
      ${Array.isArray(b.equipment[k]) ? chips(b.equipment[k]) : chips([b.equipment[k]])}</div>`)
    .join("") || `<div class="dim">no equipment recorded</div>`;
  const po = b.posture || {};
  const changes = b.changes.length ? b.changes.slice(0, 20).map((c) => `
    <div class="chg-row"><small>${esc(ago(c.occurred_at) || c.occurred_at || "")}</small>
      ${c.kind && c.kind !== "updated" ? `<b>${esc(c.kind)}</b> ` : ""}${esc((c.fields || []).join(", "))}</div>`).join("")
    : `<div class="dim">no changes across ${esc(b.versions)} keyframe${b.versions === 1 ? "" : "s"}</div>`;
  box.innerHTML = `
    <div class="ed-head"><b>${esc(b.short)}</b>
      <small>${esc(b.model || "")}${b.account ? ` · ${esc(b.account)}` : ""}</small>
      <button class="pill ghost ed-close" type="button">close</button></div>
    ${b.org && b.org.mission ? `<div class="ed-mission">${esc(b.org.mission)}</div>` : ""}
    <div class="ed-cols">
      <div><h4>equipment</h4>${eq}</div>
      <div><h4>posture</h4>
        <div class="eq-row"><b>permissions</b>${chips([po.permissions_mode || "—"])}</div>
        <div class="eq-row"><b>tool allow</b>${chips(po.tool_allow || [])}</div>
        <div class="eq-row"><b>tool deny</b>${chips(po.tool_deny || [])}</div>
        ${po.sandbox ? `<div class="eq-row"><b>sandbox</b>${chips([
          po.sandbox.enabled ? "enabled" : "off"])}</div>` : ""}
        <h4>org</h4>
        ${b.org && b.org.reports_to ? `<div class="eq-row"><b>reports to</b>${chips([b.org.reports_to])}</div>` : ""}
        ${b.org && b.org.manages && b.org.manages.length
          ? `<div class="eq-row"><b>manages</b>${chips(b.org.manages)}</div>` : ""}
        <h4>changes</h4>${changes}
        <details class="machinery"><summary>composed hashes</summary>
          ${Object.entries(b.composed_hashes || {}).map(([k, v]) =>
            `<div class="hash-row"><span>${esc(k)}</span><code>${esc(String(v).slice(0, 12))}</code></div>`).join("")}
        </details>
      </div>
    </div>`;
  box.querySelector(".ed-close").addEventListener("click", () => { box.hidden = true; });
  box.scrollIntoView({ block: "nearest" });
}
