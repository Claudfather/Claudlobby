import { ActionState, rowKey, scopeKey, validContext, validTaskActionContext, validTaskActionTarget } from "/action-state.js";
import { esc, ago, stateBlock } from "/panel-state.js";

// One presentation for direct and embedded Plane. No transport is constructed
// here: the default read-only client never supplies a write capability.
// Additive channel navigation metadata, not action authority or an actor alias.
// Direct-host reads use bare IDs; injected demo transports use one shared
// task qualifier; host/fleet UIDs may be bare or share that same qualifier.
// The injected transport must serve that exact qualified detail route.
const LINK_TASK = /^(?:([a-z][a-z0-9-]*)::)?wi_[0-9a-f]{32}$/;
const LINK_FLEET = /^(?:([a-z][a-z0-9-]*)::)?fleet_[0-9a-f]{32}$/;
const LINK_HOST = /^(?:([a-z][a-z0-9-]*)::)?host_[0-9a-f]{32}$/;
export function conversationTaskLink(thread) {
  const link = thread?.task_link;
  if (!link || typeof thread.key !== "string" || !thread.key || thread.key.length > 240
      || typeof link.fleet !== "string" || !link.fleet || link.fleet === "all" || link.fleet.length > 240) return null;
  const task = typeof link.task_id === "string" && link.task_id.match(LINK_TASK);
  const fleet = typeof link.fleet_uid === "string" && link.fleet_uid.match(LINK_FLEET);
  const host = typeof link.host_uid === "string" && link.host_uid.match(LINK_HOST);
  if (!task || !fleet || !host || link.task_id !== thread.work_item_id
      || (fleet[1] && task[1] !== fleet[1]) || (host[1] && task[1] !== host[1])) return null;
  return link;
}

export function mountWorkLoop({ api, renderThread, refresh, onActionsChange = () => {} }) {
  const root = document.getElementById("work-loop");
  const dialog = document.getElementById("task-detail");
  let storage;
  try { storage = sessionStorage; } catch { storage = null; }
  const state = new ActionState(storage);
  const contextsByKind = new Map(), checkingReceipts = new Set();
  const actionRefreshEpochs = new Map();
  const taskContextReader = action => action === "nudge" ? api.nudgeContext : action === "feedback" ? api.feedbackContext : null;
  const retainContext = value => Object.freeze({ ...value, scope: Object.freeze({ ...value.scope }),
    recipients: Object.freeze(value.recipients.map(recipient => Object.freeze({ ...recipient }))), actions: Object.freeze([...value.actions]) });
  const messageRecipients = new Map(); // Tab memory only, bound to the full authorized scope.
  let context = null, room = null, epoch = 0, board = null, channel = null;
  let detailEpoch = 0, pendingDetail = null, compositionEpoch = 0, detailSnapshot = null, targetTitle = null;
  let conversation = null;
  let selected = null, target = null, kind = "message", sending = false, inFlightRequest = null, opener = null, openerIdentity = null, readsPaused = false;
  let notice = "Choose a team to see its available actions.";
  root.innerHTML = `<div class="work-loop-head"><div><h2>Talk to your team</h2>
    <p id="work-scope"></p></div><span id="work-mode" class="tag"></span></div>
    <p id="work-unavailable" class="note"></p>
    <form id="work-form" hidden>
      <div class="work-target"><label for="work-recipient">To</label>
        <select id="work-recipient"></select><span id="work-task"></span>
        <button id="work-reset" class="pill ghost" type="button" hidden>New message</button></div>
      <label id="work-label" for="work-body">Message</label>
      <textarea id="work-body" maxlength="2000" rows="3" placeholder="What should the team focus on next?"></textarea>
      <div class="work-controls"><span class="note">Drafts stay in this tab. Refreshing clears unsent text.</span>
        <button id="work-send" type="submit" class="pill">Send message</button></div>
    </form>
    <p id="work-notice" role="status" aria-live="polite"></p>
    <div id="work-pending"></div>
    <div id="work-recoverable"></div>
    <button id="work-clear-saved" class="pill ghost" type="button" hidden>Clear unreadable saved requests</button>`;
  const $ = id => document.getElementById(id);
  const row = () => context && target ? { ...(context.version === 2 ? { version: 2 } : {}), scope: context.scope, kind, target } : null;
  const requestContext = request => {
    const value = contextsByKind.get(request.kind);
    return value && (request.version === 2 ? value.version === 2 : value.version === 1)
      && scopeKey(value.scope) === scopeKey(request.scope) ? value : null;
  };
  const visibleRequest = request => requestContext(request) || (request.version === 2 && [...contextsByKind.values()].some(value =>
    ["workspace", "host", "fleet"].every(field => value.scope[field] === request.scope[field])));
  const selectionKey = () => row() ? rowKey(row()) : "";
  const leadId = c => (c.recipients.find(r => r.lead) || c.recipients[0]).id;
  const readDraft = () => row() ? state.draft(row()) : "";
  const recipientLabel = request => requestContext(request)?.recipients.find(r => r.id === request.target.recipient)?.label || request.target.recipient;
  function saveDraft() {
    if (!row()) return;
    try { state.draft(row(), $("work-body").value); }
    catch (error) { notice = error.message; }
  }
  // Selection only: authority must already be admitted for this exact room.
  // No context probe, task preparation, submission or receipt lookup lives here.
  function canSelectMessageRecipient(identity) {
    const message = contextsByKind.get("message");
    return !!message && message.version === 1 && message.actions.includes("message")
      && identity?.current === true && identity.provisional === false
      && /^actor_[0-9a-f]{32}$/.test(identity.uid)
      && /^fleet_[0-9a-f]{32}$/.test(identity.fleet_uid)
      && typeof identity.alias === "string" && identity.alias.length > 0
      && identity.fleet === room && message.room === room && message.scope.fleet === room
      && message.recipients.some(recipient => recipient.id === identity.uid);
  }
  function selectMessageRecipient(identity) {
    if (!canSelectMessageRecipient(identity)) return false;
    saveDraft(); ++compositionEpoch;
    context = contextsByKind.get("message"); kind = "message"; targetTitle = null;
    target = { recipient: identity.uid, task_id: null };
    messageRecipients.set(scopeKey(context.scope), identity.uid);
    opener = null; openerIdentity = null;
    closeDetail(); notice = ""; paint({ restoreDraft: true }); $("work-body").focus();
    return true;
  }
  function pendingRows() {
    const rows = state.pending.filter(visibleRequest);
    $("work-pending").innerHTML = rows.map(p => `<div class="pending-action">
      <div><b>Awaiting confirmation</b><p>${esc(p.kind)} · ${esc(recipientLabel(p))}${p.target.task_id ? ` · task ${esc(p.target.task_id)}` : ""}</p>
      ${!requestContext(p) ? '<p class="note">Retained request from prior access. Its receipt cannot be checked under the current grant.</p>' : ""}<small>Submitted ${esc(ago(p.submitted_at))} · ${esc(p.request_id)}</small></div>
      <button class="pill ghost" type="button" data-request="${esc(p.request_id)}"${!requestContext(p) || p.request_id === inFlightRequest || checkingReceipts.has(p.request_id) ? " disabled" : ""}>Check receipt</button>
      <button class="pill ghost" type="button" data-discard="${esc(p.request_id)}"${p.request_id === inFlightRequest || checkingReceipts.has(p.request_id) ? " disabled" : ""}>Discard saved request</button></div>`).join("")
      + [...state.discarded.values()].filter(p => requestContext(p))
        .map(p => `<p class="note">Discarded locally · ${esc(p.kind)} to ${esc(recipientLabel(p))}${p.target.task_id ? ` · task ${esc(p.target.task_id)}` : ""} · ${esc(p.request_id)}. Outcome unknown; this ID is retained only until this tab reloads.</p>`).join("");
  }
  function paint({ restoreDraft = false } = {}) {
    const usable = !!context && !!target;
    $("work-form").hidden = !usable;
    $("work-mode").textContent = context?.simulation ? "EXAMPLE · NO REAL BOT DELIVERY" : "";
    $("work-scope").textContent = context ? context.scope.fleet : (room || "All teams");
    $("work-unavailable").textContent = usable ? "" : contextsByKind.has("feedback")
      ? "Open a task to give feedback to the lead. This does not approve or change the task."
      : contextsByKind.has("nudge") ? "Open a task to ask its team lead about it. This does not approve or complete work."
      : "Browser actions are unavailable for this view. Its task and activity records remain readable.";
    if (usable) {
      const options = context.recipients.map(r => `<option value="${esc(r.id)}">${esc(r.label)}${r.lead ? " · lead" : ""}</option>`).join("");
      if ($("work-recipient").innerHTML !== options) $("work-recipient").innerHTML = options;
      $("work-recipient").value = target.recipient;
      $("work-recipient").disabled = kind !== "message";
      $("work-task").textContent = target.task_id ? `Task: ${targetTitle || target.task_id}` : "";
      $("work-reset").hidden = !target.task_id || !contextsByKind.has("message");
      $("work-label").textContent = kind === "feedback" ? "Feedback on this task" : kind === "nudge" ? "Ask the team lead about this task (not approval)" : "Message";
      $("work-send").textContent = sending ? "Sending…" : kind === "nudge" ? "Ask team lead" : kind === "feedback" ? "Send feedback" : "Send message";
      $("work-send").disabled = sending || !context.actions.includes(kind) || !!state.unresolved(row()) || state.storageError;
      if (restoreDraft) $("work-body").value = readDraft();
    } else $("work-send").disabled = true;
    $("work-notice").textContent = usable && state.storageError
      ? "This tab cannot safely save or read pending receipts. Sending is disabled; existing requests were not resent."
      : notice;
    $("work-recoverable").innerHTML = state.corruptStorage && state.recoverable.length
      ? '<p class="note">Readable saved request IDs from this tab. Copy any needed IDs before clearing the damaged records.</p>'
        + state.recoverable.map(p => `<p class="note">${esc(p.scope.fleet)} · ${esc(p.kind)} to ${esc(recipientLabel(p))}${p.target.task_id ? ` · task ${esc(p.target.task_id)}` : ""} · ${esc(p.request_id)}</p>`).join("")
      : "";
    $("work-clear-saved").hidden = !state.corruptStorage;
    pendingRows();
    onActionsChange();
  }
  function closeDetail() {
    ++detailEpoch; pendingDetail = null; detailSnapshot = null; conversation = null;
    $("task-detail-content").innerHTML = "";
    if (dialog.open) dialog.close();
    selected = null;
  }
  function invalidate(message = "Message access is unavailable. Your draft is kept; task and activity records remain readable.", expectedScope, affectedKind, expectedRecipient) {
    if (affectedKind) {
      const affected = contextsByKind.get(affectedKind);
      if (!affected || (expectedScope && scopeKey(expectedScope) !== scopeKey(affected.scope))
        || (expectedRecipient && !affected.recipients.some(recipient => recipient.id === expectedRecipient))) return;
      if (kind === affectedKind) {
        saveDraft(); ++compositionEpoch;
        if (kind === "message" && target?.task_id === null) messageRecipients.set(scopeKey(affected.scope), target.recipient);
        context = null; target = null; targetTitle = null; $("work-body").value = "";
        // Restore the independent message draft without retargeting a task action.
        if (["nudge", "feedback"].includes(affectedKind)) {
          kind = "message";
          const messageContext = contextsByKind.get("message");
          if (messageContext) {
            context = messageContext;
            const remembered = messageRecipients.get(scopeKey(context.scope));
            target = { recipient: context.recipients.some(r => r.id === remembered) ? remembered : leadId(context), task_id: null };
            $("work-body").value = readDraft();
          }
        }
      }
      contextsByKind.delete(affectedKind);
      notice = affectedKind === "feedback" ? "Task feedback access is unavailable. Your comment is kept; task records remain readable." : affectedKind === "nudge" ? "Task nudge access is unavailable. Your reason is kept; task records remain readable." : message || "Message access is unavailable. Your draft is kept; task records remain readable.";
      updateDetailActions(); paint();
      const readContext = taskContextReader(affectedKind);
      if (typeof readContext === "function") {
        const token = epoch, retry = (actionRefreshEpochs.get(affectedKind) || 0) + 1, selectedRoom = room;
        actionRefreshEpochs.set(affectedKind, retry);
        // One read-only recovery per scoped refusal. Context refusals do not
        // call invalidate, and this result never restores a task target or sends.
        Promise.resolve().then(() => token === epoch && retry === actionRefreshEpochs.get(affectedKind)
          ? readContext(selectedRoom) : null).then(value => {
          if (token !== epoch || retry !== actionRefreshEpochs.get(affectedKind) || contextsByKind.has(affectedKind)) return;
          if (!validTaskActionContext(value, affectedKind) || value.room !== selectedRoom || value.scope.fleet !== selectedRoom
              || typeof api.prepareAction !== "function" || typeof api.sendAction !== "function"
              || typeof api.actionReceipt !== "function") return;
          contextsByKind.set(affectedKind, retainContext(value));
          updateDetailActions();
          if (selected) $("task-detail-refresh").hidden = false;
          paint(); // Do not overwrite a message composed while recovery was pending.
        }).catch(() => {});
      }
      return;
    }
    if (expectedScope && (!context || scopeKey(expectedScope) !== scopeKey(context.scope))) return;
    saveDraft();
    if (context?.actions.includes("message") && kind === "message" && target?.task_id === null)
      messageRecipients.set(scopeKey(context.scope), target.recipient);
    ++epoch;
    contextsByKind.clear(); ++compositionEpoch;
    context = null; target = null; targetTitle = null;
    closeDetail();
    $("work-body").value = "";
    notice = message;
    paint();
  }
  function pause() {
    readsPaused = true;
    invalidate("Session access is paused. Actions are disabled until your session is checked again; your draft is kept.");
  }
  function setRoom(fleet) {
    saveDraft();
    if (context?.actions.includes("message") && kind === "message" && target?.task_id === null)
      messageRecipients.set(scopeKey(context.scope), target.recipient);
    const token = ++epoch, selectedRoom = fleet || "all";
    ++compositionEpoch; room = selectedRoom; readsPaused = false;
    contextsByKind.clear(); context = null; target = null; targetTitle = null; kind = "message"; board = null; channel = null;
    closeDetail(); $("work-body").value = ""; $("work-recipient").innerHTML = ""; $("work-recipient").value = "";
    notice = "Checking available actions…"; paint();
    function accept(value, taskKind) {
      if (token !== epoch) return;
      const valid = selectedRoom !== "all" && (taskKind ? validTaskActionContext(value, taskKind) : validContext(value))
        && value.room === selectedRoom && value.scope.fleet === selectedRoom
        && typeof api.sendAction === "function" && typeof api.actionReceipt === "function"
        && (!taskKind || typeof api.prepareAction === "function");
      if (valid) {
        value = retainContext(value);
        for (const action of value.actions) if (taskKind || value.simulation || action === "message") contextsByKind.set(action, value);
        if (!taskKind && kind === "message" && !target && !contextsByKind.has("message")) $("work-recipient").value = leadId(value);
        if (kind === "message" && contextsByKind.has("message") && !target) {
          context = contextsByKind.get("message");
          const remembered = messageRecipients.get(scopeKey(context.scope));
          target = {recipient:context.recipients.some(r => r.id === remembered) ? remembered : leadId(context),task_id:null};
          paint({ restoreDraft: true });
        }
        notice = value.simulation ? "Example actions are recorded only by the test service. No real agents receive them." : "";
      } else if (!contextsByKind.size) notice = selectedRoom === "all" ? "Choose a team to see its available actions."
        : "Browser action access is unavailable for this team. Task and activity records remain readable.";
      if (selected) $("task-detail-refresh").hidden = false;
      paint(); // An unrelated context result must not replace an authored draft.
    }
    Promise.resolve().then(() => token === epoch ? api.interactionContext?.(selectedRoom) : null)
      .then(value => accept(value, null)).catch(() => accept(null, null));
    // Keep one task-context read in flight per room alongside the message read.
    Promise.resolve().then(async () => {
      for (const action of ["nudge", "feedback"]) {
        if (token !== epoch) return;
        const readContext = taskContextReader(action);
        if (typeof readContext !== "function") continue;
        try { accept(await readContext(selectedRoom), action); }
        catch { accept(null, action); }
      }
    });
  }
  function showDetailUnavailable(...block) {
    $("task-detail-content").innerHTML = '<h2 id="task-detail-title">Task details are unavailable</h2>' + stateBlock(...block);
  }
  function detail() {
    if (!selected) return;
    const token = ++detailEpoch, selection = selected; pendingDetail = null; detailSnapshot = null; conversation = null;
    const content = $("task-detail-content");
    // Read at call time: a late reply must see the board as it is then.
    const boardTask = () => board?.state === "ok" ? board.data.tasks.find(t => t.task_id === selection.id && (t.fleet || "") === selection.fleet) : null;
    if (!selection.fleet) {
      const task = boardTask();
      if (task && typeof api.mountSessionControls !== "function") renderDetail(task, true);
      else showDetailUnavailable("unknown", null, "This task has no recorded team. Full detail cannot be authorized in this view.");
      return;
    }
    if (typeof api.jget !== "function") {
      // Older injected transports can show their board snapshot, explicitly
      // labelled. Never fall back after a real detail request was refused.
      const task = boardTask();
      if (task && !selection.fleetUid) renderDetail(task, true);
      else showDetailUnavailable("unavailable");
      return;
    }
    content.innerHTML = '<h2 id="task-detail-title">Task details</h2>' + stateBlock("loading");
    const url = `/api/tasks/${encodeURIComponent(selection.id)}?fleet=${encodeURIComponent(selection.fleet)}`;
    pendingDetail = token;
    Promise.resolve().then(() => token === detailEpoch && selected === selection ? api.jget(url) : null).then(envelope => {
      if (token !== detailEpoch || selected !== selection) return;
      pendingDetail = null;
      const task = envelope?.state === "ok" ? envelope.data?.task : null;
      if (!task || task.task_id !== selection.id || task.fleet !== selection.fleet
          || (selection.fleetUid && task.fleet_uid !== selection.fleetUid)) {
        const previous = boardTask();
        // Legacy/synthetic read transports may not implement this route yet.
        // A protected transport refusal must never redisplay stale private data.
        const fallback = !selection.fleetUid && !task && previous && typeof api.mountSessionControls !== "function"
          && !["denied", "not_found", "invalid", "unknown"].includes(envelope?.state);
        if (fallback) renderDetail(previous, true, envelope || {state:"disconnected"});
        else showDetailUnavailable(task ? "unknown" : envelope?.state || "disconnected",
          envelope?.provenance, envelope?.remediation);
        return;
      }
      renderDetail(task, false);
    }).catch(() => {
      if (token !== detailEpoch || selected !== selection) return;
      pendingDetail = null;
      showDetailUnavailable("disconnected");
    });
  }
  function historyWindow(label, window) {
    return window?.truncated ? `<p class="note">${esc(label)}: showing ${esc(window.shown)} most recent of ${esc(window.total)} recorded entries.</p>` : "";
  }
  function recordIds(entries) {
    const visible = entries.filter(([, value]) => value);
    if (!visible.length) return "";
    return `<details class="task-record-details"><summary>Record identifiers</summary><dl class="task-facts">${visible
      .map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join("")}</dl></details>`;
  }
  function eventDetails(event) {
    if (typeof event.detail !== "string") return "";
    let fields = null;
    try { const value = JSON.parse(event.detail); if (value && typeof value === "object" && !Array.isArray(value)) fields = value; } catch {}
    const prose = ["summary", "reason", "question"].filter(key => typeof fields?.[key] === "string")
      .map(key => `<p class="note">${esc(key[0].toUpperCase() + key.slice(1))}</p><pre class="task-record-body">${esc(fields[key])}</pre>`).join("");
    return prose + `<details class="task-record-details"><summary>Recorded event details</summary><pre class="task-record-body">${esc(event.detail)}</pre></details>`;
  }
  function events(rows) {
    return (rows || []).map(e => {
      const actor = e.actor_short && e.actor_short !== e.actor_uid ? e.actor_short : e.actor_alias || (e.actor_uid ? "Actor not resolved" : "");
      return `<li><b>${esc((e.event || "Recorded event").replaceAll("_", " "))}</b> · ${esc(ago(e.occurred_at))}
        ${actor ? ` · ${esc(actor)}` : ""}${eventDetails(e)}
        ${recordIds([["Event", e.event_id], ["Actor", e.actor_uid], ["Assignment", e.assignment_id]])}</li>`;
    }).join("");
  }
  function assigneeLabel(assignment) {
    if (!assignment) return "No current assignment";
    return assignment.assignee_short && assignment.assignee_short !== assignment.assignee_uid
      ? assignment.assignee_short : assignment.assignee_alias || "Assignee not resolved";
  }
  function renderDetail(task, boardOnly, unavailable = null) {
    const content = $("task-detail-content");
    const lastEvent = task.last_event || task.history?.at(-1);
    const assignment = task.current_assignment;
    content.innerHTML = `<p class="eyebrow">${esc(task.fleet || "Team not recorded")} · TASK</p>
      <h2 id="task-detail-title">${esc(task.title || "Recorded task")}</h2>
      <p class="task-state"><b>${esc(task.state || "State unknown")}</b></p>
      ${recordIds([["Task", task.task_id], ["Team", task.fleet_uid], ["Created by", task.created_by_alias || task.created_by_uid]])}
      ${unavailable ? stateBlock(unavailable.state || "unavailable", unavailable.provenance, unavailable.remediation) : ""}
      <dl class="task-facts"><dt>Assigned to</dt><dd>${esc(assigneeLabel(assignment))}</dd>
      <dt>Last recorded action</dt><dd>${esc(lastEvent?.event || "No action recorded")} · ${esc(ago(lastEvent?.occurred_at))}</dd>
      <dt>Delivery evidence</dt><dd>${esc(task.delivery?.integrity || "No confirmed delivery evidence in this view")}</dd></dl>
      ${task.attention_question || task.attention_reason?.includes("escalated") ? `<div class="task-question"><b>Needs your input</b><pre class="task-record-body">${esc(task.attention_question || "The question was not recorded.")}</pre></div>` : ""}
      ${task.resolved === false ? '<p class="task-question">Task history has unresolved links. Its result cannot be treated as confirmed.</p>' : ""}
      ${boardOnly ? '<p class="note">Showing a limited board snapshot. Full task detail is unavailable in this view.</p>' : `
      <h3>Task description</h3>${task.body === null || task.body === undefined ? '<p class="detail-empty">No task body was recorded.</p>' : `<pre class="task-record-body">${esc(task.body)}</pre>`}
      <h3>Assignments</h3>${historyWindow("Assignments", task.assignments_window)}
      ${(task.assignments || []).map(a => `<details class="task-assignment"><summary>${esc(assigneeLabel(a))} · ${esc(a.state)}</summary>
        ${recordIds([["Assignment", a.assignment_id], ["Assignee", a.assignee_uid], ["Assigned by", a.assigned_by_uid], ["Dispatch message", a.dispatch_message_id]])}
        <p class="note">Assigned by ${esc(a.assigned_by_short && a.assigned_by_short !== a.assigned_by_uid ? a.assigned_by_short : a.assigned_by_alias || "an unresolved actor")} · expected by ${esc(a.expected_by || "not recorded")}</p>
        ${historyWindow("Assignment history", a.history_window)}<ol class="task-history">${events(a.history)}</ol></details>`).join("") || '<p class="detail-empty">No assignment was recorded.</p>'}
      <h3>Task history</h3>${historyWindow("Task history", task.history_window)}<ol class="task-history">${events(task.history)}</ol>
      ${(task.issues || []).length ? `<h3>History issues</h3><ul>${task.issues.map(i => `<li>${esc(i.code)}${i.blocking ? " · unresolved" : " · historical"}</li>`).join("")}</ul>` : ""}
      ${task.issues_window?.truncated ? '<p class="note">Additional history issues are omitted from this bounded view.</p>' : ""}`}
      <p class="note">Task state, history and action selection are a snapshot. Use Refresh to read them again.</p>
      <h3>Recent conversation &amp; reports</h3><p class="note">This is the recent channel window, not a complete task history. Completion alone does not mean a result was reviewed.</p>
      <button id="task-conversation-update" class="pill ghost" type="button" hidden>New conversation available</button>
      <p id="task-lineage-note" class="note" hidden></p>
      <div id="task-reports"></div>
      <div class="task-detail-actions"><button class="pill" type="button" data-kind="feedback">Give feedback</button>
      <button class="pill ghost" type="button" data-kind="nudge">Nudge task</button></div>
      <p class="note" id="task-action-note"></p>`;
    detailSnapshot = { task: Object.freeze({ ...task, ...(Object.hasOwn(task, "current_assignment")
      ? { current_assignment: task.current_assignment && Object.freeze({ ...task.current_assignment }) } : {}) }), boardOnly };
    const selection = selected, token = detailEpoch;
    $("task-conversation-update").onclick = () => {
      if (selected !== selection || token !== detailEpoch) return;
      updateConversation(true);
    };
    updateConversation();
    updateDetailActions();
  }
  function updateConversation(explicit = false) {
    if (!selected || !detailSnapshot || detailSnapshot.task.task_id !== selected.id
        || (detailSnapshot.task.fleet || "") !== selected.fleet) return;
    const reports = $("task-reports"), button = $("task-conversation-update"), lineageNote = $("task-lineage-note");
    // The channel is the admitted current-room read. Task IDs are opaque,
    // shared across that room's cross-fleet threads; never infer from aliases.
    const ok = channel?.state === "ok";
    const threads = ok ? channel.data.threads.filter(t => t.work_item_id === selected.id) : [];
    // These diagnostics describe the entire admitted room window. They do not
    // identify an affected task, and numeric detail/reasons do not change copy.
    const unresolved = ok && Number.isSafeInteger(channel.data.lineage?.unresolved_threads)
      && channel.data.lineage.unresolved_threads > 0;
    const signature = JSON.stringify(ok ? [threads, unresolved] : [channel?.state, channel?.provenance, channel?.remediation]);
    const active = document.activeElement, held = active === button || reports.contains(active) || lineageNote.contains(active);
    // Hiding the focused button or clearing a focused message would drop focus
    // to the page. Keep it on the region; never take it from elsewhere.
    const settle = keepFocus => {
      button.hidden = true;
      if (keepFocus && document.activeElement !== reports) { reports.tabIndex = -1; reports.focus(); }
    };
    if (conversation?.signature === signature) { settle(active === button); return; }
    // Focus on the region itself (left there by an explicit update) survives
    // replacement; only focus or selected text inside it would be lost.
    const reading = (active !== reports && (reports.contains(active) || lineageNote.contains(active)))
      || selectionTouches(reports) || selectionTouches(lineageNote);
    // Source loss clears stale text even during interaction. Healthy updates
    // wait for explicit consent when focus or a text selection would be lost.
    if (!explicit && ok && reading) { button.hidden = false; return; }
    const previous = conversation?.threads || new Map(), next = new Map();
    if (!ok) reports.innerHTML = stateBlock(channel?.state || "disconnected", channel?.provenance, channel?.remediation);
    else if (!threads.length) reports.innerHTML = '<p class="detail-empty">No linked conversation or result is available in the recent channel window.</p>';
    else {
      const articles = threads.map(thread => {
        const stamp = JSON.stringify(thread), old = previous.get(thread.key);
        const article = old?.stamp === stamp ? old.article : renderThread(thread);
        if (old && article !== old.article) {
          const expanded = new Map([...old.article.querySelectorAll(".msg[data-msg-id]")].map(message =>
            [message.dataset.msgId, [...message.querySelectorAll("details")].map(detail => detail.open)]));
          for (const message of article.querySelectorAll(".msg[data-msg-id]")) {
            const open = expanded.get(message.dataset.msgId);
            if (open) [...message.querySelectorAll("details")].forEach((detail, index) => { detail.open = !!open[index]; });
          }
        }
        next.set(thread.key, { stamp, article });
        return article;
      });
      reports.replaceChildren(...articles);
    }
    lineageNote.textContent = unresolved
      ? `Some recent replies ${room === "all" ? "across teams" : "in this team"} couldn’t be linked. Task conversations may be incomplete.` : "";
    lineageNote.hidden = !unresolved;
    conversation = { signature, threads: next };
    settle(explicit || held);
  }
  // Endpoints inside the node, or a range reaching it from outside (select all).
  function selectionTouches(node) {
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed) return false;
    if (node.contains(selection.anchorNode) || node.contains(selection.focusNode)) return true;
    for (let index = 0; index < (selection.rangeCount || 0); index++) {
      const range = selection.getRangeAt(index);
      if (!range.collapsed && range.intersectsNode(node)) return true;
    }
    return false;
  }
  function frozenTaskTarget(task, value, boardOnly, action) {
    if (boardOnly || task.resolved !== true || !Object.hasOwn(task, "body")
        || !Object.hasOwn(task, "current_assignment")) return null;
    const assignment = task.current_assignment, live = ["assigned", "active", "blocked"];
    // Only feedback may select terminal work, and only with no current assignment.
    const unassigned = action === "feedback" ? ["queued", "completed", "failed", "cancelled"] : ["queued"];
    if (assignment === null ? !unassigned.includes(task.state) : !live.includes(task.state) || !assignment || assignment.task_id !== task.task_id
        || !live.includes(assignment.state) || assignment.terminal_event !== null) return null;
    const result = { recipient: leadId(value), task_id: task.task_id,
      assignment_id: assignment === null ? null : assignment.assignment_id, release_id: value.release_id };
    return validTaskActionTarget(result) ? result : null;
  }
  function updateDetailActions() {
    if (!detailSnapshot) return;
    const { task, boardOnly } = detailSnapshot, content = $("task-detail-content");
    const capability = action => {
      const value = contextsByKind.get(action);
      if (!value || value.scope.fleet !== task.fleet) return null;
      if (value.version === 2 && !frozenTaskTarget(task, value, boardOnly, action)) return null;
      return value;
    };
    for (const button of content.querySelectorAll("[data-kind]")) {
      button.disabled = !capability(button.dataset.kind);
      button.onclick = () => {
        const nextKind = button.dataset.kind, nextContext = capability(nextKind);
        if (!nextContext) return;
        saveDraft(); ++compositionEpoch;
        if (kind === "message" && target?.task_id === null) messageRecipients.set(scopeKey(context.scope), target.recipient);
        kind = nextKind; context = nextContext; targetTitle = task.title;
        target = context.version === 2 ? frozenTaskTarget(task, context, boardOnly, kind) : { recipient: leadId(context), task_id: task.task_id };
        opener = null; openerIdentity = null;
        closeDetail(); notice = context.simulation ? "Example task action. No real bot delivery."
          : kind === "feedback" ? "Send a comment about this task to the lead. This does not approve or change the task." : "This asks the team lead about the selected task. It does not approve or complete work. Refresh and select again if its assignment or release changes.";
        paint({ restoreDraft: true }); $("work-body").focus();
      };
    }
    const feedback = capability("feedback"), nudge = capability("nudge");
    const taskActions = feedback && nudge ? "Task feedback and nudges go to this team’s lead."
      : feedback ? "Task feedback goes to this team’s lead. Nudges are unavailable with this connection."
      : nudge ? "Task nudges go to this team’s lead. Feedback is unavailable with this connection."
      : "Task feedback and nudges are unavailable with this connection.";
    $("task-action-note").textContent = taskActions + (feedback?.version === 2 ? " Feedback is a linked comment, not approval or a task change." : "") + (nudge?.version === 2 ? " A nudge asks about this task; it is not approval." : "")
      + (capability("message") ? " Close this task and use Talk to your team to send an ordinary message." : "");
  }
  function update(tasks, messages) {
    board = tasks; updateChannel(messages);
  }
  // A channel-only read (stream source loss) leaves the board snapshot as read.
  function updateChannel(messages) {
    channel = messages;
    // Source loss also retires an unfinished detail read. Keep an already
    // displayed lifecycle snapshot, but never admit a held old response.
    if (selected && pendingDetail !== null && channel?.state !== "ok") {
      ++detailEpoch; pendingDetail = null;
      showDetailUnavailable(channel?.state || "disconnected", channel?.provenance, channel?.remediation);
    }
    // Lifecycle and action preconditions stay frozen until explicit Refresh.
    // Only the admitted recent conversation read may update in place.
    if (selected) { $("task-detail-refresh").hidden = false; updateConversation(); }
  }
  function openTask(event, mainChannel = false) {
    const button = event.target.closest("[data-task-open]");
    if (!button) return;
    let link = null;
    if (mainChannel) {
      if (readsPaused || channel?.state !== "ok") return;
      const thread = channel.data.threads.find(t => t.key === button.dataset.taskThread);
      link = conversationTaskLink(thread);
      if (!link || link.task_id !== button.dataset.taskOpen || link.fleet !== button.dataset.taskFleet
          || link.host_uid !== button.dataset.taskHost || link.fleet_uid !== button.dataset.taskFleetUid) return;
    }
    ++compositionEpoch; selected = { id: button.dataset.taskOpen, fleet: button.dataset.taskFleet || "",
      ...(link ? {fleetUid:link.fleet_uid,hostUid:link.host_uid} : {}) };
    opener = button; openerIdentity = { ...selected, panel: mainChannel ? "channel" : button.closest("#attention, #tasks")?.id,
      ...(mainChannel ? {thread:button.dataset.taskThread} : {}) };
    detail(); $("task-detail-refresh").hidden = true;
    dialog.showModal(); $("task-detail-close").focus();
  }
  document.getElementById("rail-right").addEventListener("click", event => openTask(event));
  $("channel").addEventListener("click", event => openTask(event, true));
  $("task-detail-close").onclick = closeDetail;
  $("task-detail-refresh").onclick = () => { ++compositionEpoch; detail(); $("task-detail-refresh").hidden = true; };
  dialog.addEventListener("close", () => {
    ++detailEpoch; pendingDetail = null;
    $("task-detail-content").innerHTML = "";
    selected = null; detailSnapshot = null; conversation = null;
    if (!openerIdentity) return;
    const rail = $("rail-right"), buttons = [...rail.querySelectorAll("[data-task-open]")];
    const matches = button => button.dataset.taskOpen === openerIdentity.id
      && (button.dataset.taskFleet || "") === openerIdentity.fleet
      && (!openerIdentity.thread || button.dataset.taskThread === openerIdentity.thread
        && button.dataset.taskHost === openerIdentity.hostUid && button.dataset.taskFleetUid === openerIdentity.fleetUid);
    const panelButtons = openerIdentity.panel
      ? [...($(openerIdentity.panel)?.querySelectorAll("[data-task-open]") || [])] : [];
    const replacement = panelButtons.find(matches) || buttons.find(matches);
    const fallback = openerIdentity.panel === "channel" ? $("channel") : buttons[0] || rail;
    const focusTarget = opener?.isConnected ? opener : replacement || fallback;
    if (focusTarget === rail || focusTarget === $("channel")) focusTarget.tabIndex = -1;
    focusTarget.focus(); opener = null; openerIdentity = null;
  });
  $("work-body").addEventListener("input", () => { ++compositionEpoch; saveDraft(); });
  $("work-recipient").onchange = () => {
    saveDraft(); ++compositionEpoch; target = { ...target, recipient: $("work-recipient").value };
    notice = ""; paint({ restoreDraft: true });
  };
  $("work-reset").onclick = () => {
    const message = contextsByKind.get("message"); if (!message) return;
    saveDraft(); ++compositionEpoch; context = message; kind = "message"; targetTitle = null;
    const remembered = messageRecipients.get(scopeKey(context.scope));
    target = {recipient:context.recipients.some(r => r.id === remembered) ? remembered : leadId(context),task_id:null};
    notice = ""; paint({ restoreDraft: true }); $("work-body").focus();
  };
  const requestLabel = request => `${request.kind} to ${recipientLabel(request)}${request.target.task_id ? ` · task ${request.target.task_id}` : ""}`;
  function receiptNotice(status, simulation, actionKind) {
    const prefix = simulation ? "Example receipt: " : "";
    return prefix + ({ delivered: simulation ? "simulated delivery confirmed. No real bot received this." : actionKind === "feedback" ? "The lead received this feedback." : actionKind === "nudge" ? "nudge received by the team lead; no task result or approval is implied." : "delivery confirmed by the host.",
      recorded: actionKind === "feedback" && !simulation ? "Feedback is recorded; delivery to the lead is unconfirmed. Check this request’s receipt." : actionKind === "nudge" && !simulation ? "task nudge recorded; delivery to the lead is unconfirmed. Check this original receipt again."
        : "recorded; delivery is not yet confirmed. Check this receipt again.",
      rejected: "request refused. Nothing was delivered. You may edit a new request." })[status];
  }
  $("work-form").onsubmit = async event => {
    event.preventDefault();
    if (!context || !target || sending) return;
    saveDraft(); const originContext = context, token = epoch, composition = compositionEpoch, selection = selectionKey(), body = $("work-body").value;
    let request, restoreDraft = false;
    try {
      if (typeof globalThis.crypto?.randomUUID !== "function")
        throw new Error("A safe request ID is unavailable. Nothing was sent.");
      let requestId;
      try { requestId = globalThis.crypto.randomUUID(); }
      catch { throw new Error("A safe request ID is unavailable. Nothing was sent."); }
      if (state.pending.some(p => p.request_id === requestId) || state.discarded.has(requestId))
        throw new Error("This request ID was already used. Nothing was sent; check its original receipt.");
      sending = true; inFlightRequest = requestId;
      if (originContext.version === 2) {
        const preparation = state.prepare(originContext, target, body, requestId);
        notice = `Checking the selected task ${preparation.kind}…`; paint();
        const prepared = await api.prepareAction(preparation);
        if (token !== epoch || composition !== compositionEpoch || selection !== selectionKey() || $("work-body").value !== body
            || requestContext(preparation) !== originContext) throw new Error("Task action selection changed. Nothing was sent; select it again.");
        request = state.beginPrepared(originContext, preparation, prepared);
      } else request = state.begin(originContext, kind, target, body, requestId);
      notice = "Sending…"; paint();
      const receipt = await api.sendAction(request);
      const status = state.accept(request, receipt);
      if (token === epoch) {
        notice = `${requestLabel(request)}: ${receiptNotice(status, originContext.simulation, request.kind)}`;
        restoreDraft = status === "delivered" && selection === selectionKey();
      }
      refresh();
    } catch (error) {
      if (request && error.effect === "not_started") {
        // Only this fresh, owned send row is resolved. Receipt lookups never
        // clear older IDs merely because a new invocation did not start.
        try {
          state.accept(request, { ...state.metadata(request), version: request.version || 1, status: "rejected" });
          if (room === originContext.room)
            notice = "This submission was refused before delivery; your draft is kept.";
        } catch {
          if (room === originContext.room)
            notice = "This submission was refused before delivery, but its saved row could not be updated. Your draft and original request ID are kept.";
        }
      } else if (token === epoch) notice = request
        ? `${requestLabel(request)}: Outcome unknown. Your draft is kept. Check the original receipt before sending again.`
        : originContext.version === 2 ? originContext.actions[0] === "feedback" ? "Task feedback was not sent. Your comment is kept; refresh the task and select feedback again." : "Task nudge was not sent. Your reason is kept; refresh the task and select its nudge again." : error.message;
    } finally {
      sending = false; inFlightRequest = null;
      paint({ restoreDraft });
    }
  };
  $("work-clear-saved").onclick = () => {
    if (!state.corruptStorage || !globalThis.confirm("Saved request records cannot be read. They may include requests already delivered or still awaiting delivery. Copy any readable IDs shown above first. Clearing them removes their saved receipt IDs and allows new requests; it does not cancel or resend anything. Clear these unreadable records?")) return;
    try { state.clearCorruptStorage(); notice = "Unreadable saved requests cleared. Nothing was sent or cancelled."; }
    catch (error) { notice = error.message; }
    paint();
  };
  $("work-pending").onclick = async event => {
    const discardButton = event.target.closest("[data-discard]");
    const button = discardButton || event.target.closest("[data-request]");
    if (!button) return;
    const request = state.pending.find(p => p.request_id === (discardButton ? button.dataset.discard : button.dataset.request) && visibleRequest(p));
    if (!discardButton && request && !requestContext(request)) return;
    if (!request || request.request_id === inFlightRequest || checkingReceipts.has(request.request_id)) return;
    if (discardButton) {
      if (!globalThis.confirm(`Request ${request.request_id} may already have been delivered or may still be delivered. Copy this ID before discarding: it is retained only until this tab reloads. Discarding stops saving its receipt and lets you send a new request, which could duplicate the original. Nothing will be cancelled or resent. Discard this saved request?`)) return;
      try { state.discard(request); notice = `Discarded saved request ${request.request_id}. Its outcome remains unknown; nothing was cancelled or resent.`; }
      catch (error) { notice = error.message; }
      paint(); return;
    }
    const token = epoch, simulation = requestContext(request).simulation, selection = selectionKey();
    const label = requestLabel(request);
    checkingReceipts.add(request.request_id); button.disabled = true;
    try {
      const status = state.accept(request, await api.actionReceipt(request));
      if (token === epoch) notice = `${label}: ${receiptNotice(status, simulation, request.kind)}`;
      refresh();
    } catch {
      if (token === epoch) notice = `${label}: Receipt unavailable or mismatched. Outcome remains unknown; nothing was resent.`;
    } finally { checkingReceipts.delete(request.request_id); if (token === epoch) paint({ restoreDraft: selection === selectionKey() }); }
  };
  paint();
  return { setRoom, update, updateChannel, invalidate, pause, canSelectMessageRecipient, selectMessageRecipient };
}
