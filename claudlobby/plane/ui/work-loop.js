import { ActionState, rowKey, scopeKey, validContext } from "/action-state.js";
import { esc, ago, stateBlock } from "/panel-state.js";

// One presentation for direct and embedded Plane. No transport is constructed
// here: the default read-only client never supplies a write capability.
export function mountWorkLoop({ api, renderThread, refresh }) {
  const root = document.getElementById("work-loop");
  const dialog = document.getElementById("task-detail");
  let storage;
  try { storage = sessionStorage; } catch { storage = null; }
  const state = new ActionState(storage);
  const messageRecipients = new Map(); // Tab memory only, bound to the full authorized scope.
  let context = null, room = null, epoch = 0, board = null, channel = null;
  let detailEpoch = 0;
  let selected = null, target = null, kind = "message", sending = false, inFlightRequest = null, opener = null, openerIdentity = null;
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
  const row = () => context && target ? { scope: context.scope, kind, target } : null;
  const selectionKey = () => row() ? rowKey(row()) : "";
  const leadId = c => (c.recipients.find(r => r.lead) || c.recipients[0]).id;
  const readDraft = () => row() ? state.draft(row()) : "";
  const recipientLabel = request => (context && scopeKey(request.scope) === scopeKey(context.scope)
    ? context.recipients.find(r => r.id === request.target.recipient)?.label : null) || request.target.recipient;
  function saveDraft() {
    if (!row()) return;
    try { state.draft(row(), $("work-body").value); }
    catch (error) { notice = error.message; }
  }
  function pendingRows() {
    const rows = context ? state.pending.filter(p => scopeKey(p.scope) === scopeKey(context.scope)) : [];
    $("work-pending").innerHTML = rows.map(p => `<div class="pending-action">
      <div><b>Awaiting confirmation</b><p>${esc(p.kind)} · ${esc(recipientLabel(p))}${p.target.task_id ? ` · task ${esc(p.target.task_id)}` : ""}</p>
      <small>Submitted ${esc(ago(p.submitted_at))} · ${esc(p.request_id)}</small></div>
      <button class="pill ghost" type="button" data-request="${esc(p.request_id)}"${p.request_id === inFlightRequest ? " disabled" : ""}>Check receipt</button>
      <button class="pill ghost" type="button" data-discard="${esc(p.request_id)}"${p.request_id === inFlightRequest ? " disabled" : ""}>Discard saved request</button></div>`).join("")
      + [...state.discarded.values()].filter(p => context && scopeKey(p.scope) === scopeKey(context.scope))
        .map(p => `<p class="note">Discarded locally · ${esc(p.kind)} to ${esc(recipientLabel(p))}${p.target.task_id ? ` · task ${esc(p.target.task_id)}` : ""} · ${esc(p.request_id)}. Outcome unknown; this ID is retained only until this tab reloads.</p>`).join("");
  }
  function paint({ restoreDraft = false } = {}) {
    const usable = !!context && !!target;
    $("work-form").hidden = !usable;
    $("work-mode").textContent = context?.simulation ? "EXAMPLE · NO REAL BOT DELIVERY" : "";
    $("work-scope").textContent = context ? context.scope.fleet : (room || "All teams");
    $("work-unavailable").textContent = usable ? "" : "Browser actions are unavailable for this view. Its task and activity records remain readable.";
    if (usable) {
      const options = context.recipients.map(r => `<option value="${esc(r.id)}">${esc(r.label)}${r.lead ? " · lead" : ""}</option>`).join("");
      if ($("work-recipient").innerHTML !== options) $("work-recipient").innerHTML = options;
      $("work-recipient").value = target.recipient;
      $("work-recipient").disabled = kind !== "message";
      $("work-task").textContent = target.task_id ? `Task: ${target.task_id}` : "";
      $("work-reset").hidden = !target.task_id;
      $("work-label").textContent = kind === "feedback" ? "Feedback on this task" : kind === "nudge" ? "Why does this task need attention?" : "Message";
      $("work-send").textContent = sending ? "Sending…" : kind === "nudge" ? "Nudge task" : kind === "feedback" ? "Send feedback" : "Send message";
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
  }
  function closeDetail() {
    ++detailEpoch;
    if (dialog.open) dialog.close();
    selected = null;
  }
  function invalidate(message = "Message access is unavailable. Your draft is kept; task and activity records remain readable.", expectedScope) {
    if (expectedScope && (!context || scopeKey(expectedScope) !== scopeKey(context.scope))) return;
    saveDraft();
    if (context?.actions.includes("message") && kind === "message" && target?.task_id === null)
      messageRecipients.set(scopeKey(context.scope), target.recipient);
    ++epoch;
    context = null; target = null;
    closeDetail();
    $("work-body").value = "";
    notice = message;
    paint();
  }
  function pause() {
    invalidate("Session access is paused. Actions are disabled until your session is checked again; your draft is kept.");
  }
  function setRoom(fleet) {
    saveDraft();
    if (context?.actions.includes("message") && kind === "message" && target?.task_id === null)
      messageRecipients.set(scopeKey(context.scope), target.recipient);
    const token = ++epoch;
    room = fleet || "all";
    context = null; target = null; board = null; channel = null;
    closeDetail();
    // Do not leave private text or rows behind a newly selected team label.
    $("work-body").value = "";
    notice = "Checking available actions…";
    paint();
    Promise.resolve().then(() => api.interactionContext?.(room)).then(value => {
      if (token !== epoch) return;
      // The adapter must resolve the exact selected room, never a previous one.
      if (room !== "all" && validContext(value) && value.room === room && value.scope.fleet === room
          && typeof api.sendAction === "function" && typeof api.actionReceipt === "function") {
        context = value;
        const remembered = value.actions.includes("message") ? messageRecipients.get(scopeKey(value.scope)) : null;
        target = { recipient: value.recipients.some(r => r.id === remembered) ? remembered : leadId(value), task_id: null };
        kind = "message";
        notice = value.simulation ? "Example actions are recorded only by the test service. No real agents receive them." : "";
      } else notice = room === "all" ? "Choose a team to see its available actions."
        : "Browser action access is unavailable for this team. Task and activity records remain readable.";
      if (selected) $("task-detail-refresh").hidden = false;
      paint({ restoreDraft: true });
    }).catch(() => {
      if (token !== epoch) return;
      notice = "Could not check action access. Your draft is kept in this tab.";
      if (selected) $("task-detail-refresh").hidden = false;
      paint();
    });
  }
  function detail() {
    if (!selected) return;
    const token = ++detailEpoch, selection = selected;
    const content = $("task-detail-content");
    if (typeof api.jget !== "function") {
      // Older injected transports can show their board snapshot, explicitly
      // labelled. Never fall back after a real detail request was refused.
      const task = board?.state === "ok" ? board.data.tasks.find(t => t.task_id === selection.id && (t.fleet || "") === selection.fleet) : null;
      if (task) renderDetail(task, true);
      else content.innerHTML = '<h2 id="task-detail-title">Task details are unavailable</h2>' + stateBlock("unavailable");
      return;
    }
    content.innerHTML = '<h2 id="task-detail-title">Task details</h2>' + stateBlock("loading");
    const url = `/api/tasks/${encodeURIComponent(selection.id)}?fleet=${encodeURIComponent(selection.fleet)}`;
    Promise.resolve().then(() => api.jget(url)).then(envelope => {
      if (token !== detailEpoch || selected !== selection) return;
      const task = envelope?.state === "ok" ? envelope.data?.task : null;
      if (!task || task.task_id !== selection.id || task.fleet !== selection.fleet) {
        const previous = board?.state === "ok" ? board.data.tasks.find(t => t.task_id === selection.id && (t.fleet || "") === selection.fleet) : null;
        // Legacy/synthetic read transports may not implement this route yet.
        // A protected transport refusal must never redisplay stale private data.
        const fallback = !task && previous && typeof api.mountSessionControls !== "function"
          && !["denied", "not_found", "invalid", "unknown"].includes(envelope?.state);
        if (fallback) renderDetail(previous, true, envelope || {state:"disconnected"});
        else content.innerHTML = '<h2 id="task-detail-title">Task details are unavailable</h2>'
          + stateBlock(task ? "unknown" : envelope?.state || "disconnected",
            envelope?.provenance, envelope?.remediation);
        return;
      }
      renderDetail(task, false);
    }).catch(() => {
      if (token !== detailEpoch || selected !== selection) return;
      content.innerHTML = '<h2 id="task-detail-title">Task details are unavailable</h2>' + stateBlock("disconnected");
    });
  }
  function historyWindow(label, window) {
    return window?.truncated ? `<p class="note">${esc(label)}: showing ${esc(window.shown)} most recent of ${esc(window.total)} recorded entries.</p>` : "";
  }
  function events(rows) {
    return (rows || []).map(e => `<li><b>${esc(e.event)}</b> · ${esc(ago(e.occurred_at))}
      ${e.actor_alias || e.actor_uid ? ` · ${esc(e.actor_alias || e.actor_uid)}` : ""}
      ${e.detail ? `<pre class="task-record-body">${esc(e.detail)}</pre>` : ""}</li>`).join("");
  }
  function renderDetail(task, boardOnly, unavailable = null) {
    const content = $("task-detail-content");
    const lastEvent = task.last_event || task.history?.at(-1);
    const assignment = task.current_assignment;
    content.innerHTML = `<p class="eyebrow">${esc(task.fleet || room)} · TASK</p>
      <h2 id="task-detail-title">${esc(task.title || task.task_id)}</h2>
      <p class="task-state"><b>${esc(task.state || "State unknown")}</b> · ${esc(task.task_id)}</p>
      ${unavailable ? stateBlock(unavailable.state || "unavailable", unavailable.provenance, unavailable.remediation) : ""}
      <dl class="task-facts"><dt>Assigned to</dt><dd>${esc(assignment?.assignee_short || assignment?.assignee_alias || "No current assignment")}</dd>
      <dt>Last recorded action</dt><dd>${esc(lastEvent?.event || "No action recorded")} · ${esc(ago(lastEvent?.occurred_at))}</dd>
      <dt>Delivery evidence</dt><dd>${esc(task.delivery?.integrity || "No confirmed delivery evidence in this view")}</dd></dl>
      ${task.attention_question ? `<div class="task-question"><b>Needs your input</b><p>${esc(task.attention_question)}</p></div>` : ""}
      ${task.resolved === false ? '<p class="task-question">Task history has unresolved links. Its result cannot be treated as confirmed.</p>' : ""}
      ${boardOnly ? '<p class="note">This transport provides a board snapshot only; full task detail is unavailable.</p>' : `
      <h3>Task description</h3>${task.body === null || task.body === undefined ? '<p class="detail-empty">No task body was recorded.</p>' : `<pre class="task-record-body">${esc(task.body)}</pre>`}
      <h3>Assignments</h3>${historyWindow("Assignments", task.assignments_window)}
      ${(task.assignments || []).map(a => `<details class="task-assignment"><summary>${esc(a.assignee_alias || a.assignee_uid)} · ${esc(a.state)} · ${esc(a.assignment_id)}</summary>
        <p class="note">Assigned by ${esc(a.assigned_by_alias || a.assigned_by_uid)} · expected by ${esc(a.expected_by || "not recorded")}</p>
        ${historyWindow("Assignment history", a.history_window)}<ol class="task-history">${events(a.history)}</ol></details>`).join("") || '<p class="detail-empty">No assignment was recorded.</p>'}
      <h3>Task history</h3>${historyWindow("Task history", task.history_window)}<ol class="task-history">${events(task.history)}</ol>
      ${(task.issues || []).length ? `<h3>History issues</h3><ul>${task.issues.map(i => `<li>${esc(i.code)}${i.blocking ? " · unresolved" : " · historical"}</li>`).join("")}</ul>` : ""}
      ${task.issues_window?.truncated ? '<p class="note">Additional history issues are omitted from this bounded view.</p>' : ""}`}
      <h3>Recent conversation &amp; reports</h3><p class="note">This is the recent channel window, not a complete task history. Completion alone does not mean a result was reviewed.</p>
      <div id="task-reports"></div>
      <div class="task-detail-actions"><button class="pill" type="button" data-kind="feedback">Give feedback</button>
      <button class="pill ghost" type="button" data-kind="nudge">Nudge task</button></div>
      <p class="note" id="task-action-note"></p>`;
    const reports = $("task-reports");
    if (channel?.state !== "ok") reports.innerHTML = stateBlock(channel?.state || "disconnected", channel?.provenance, channel?.remediation);
    else {
      const threads = channel.data.threads.filter(t => t.work_item_id === task.task_id);
      if (threads.length) for (const thread of threads) reports.append(renderThread(thread));
      else reports.innerHTML = '<p class="detail-empty">No linked conversation or result is available in the recent channel window.</p>';
    }
    const sameFleet = context && context.scope.fleet === task.fleet;
    for (const button of content.querySelectorAll("[data-kind]")) {
      button.disabled = !sameFleet || !context.actions.includes(button.dataset.kind);
      button.onclick = () => {
        saveDraft();
        kind = button.dataset.kind;
        target = { recipient: leadId(context), task_id: task.task_id };
        opener = null; openerIdentity = null;
        closeDetail(); notice = context.simulation ? "Example task action. No real bot delivery." : "";
        paint({ restoreDraft: true }); $("work-body").focus();
      };
    }
    $("task-action-note").textContent = sameFleet ? "Task feedback and nudges go to this team’s lead." : "Select this team with an authorized action connection to send feedback or a nudge.";
  }
  function update(tasks, messages) {
    board = tasks; channel = messages;
    // Detail is an explicit snapshot. Never replace its DOM while someone is
    // reading, selecting text or using an action; offer a refresh instead.
    if (selected) $("task-detail-refresh").hidden = false;
  }
  document.getElementById("rail-right").addEventListener("click", event => {
    const button = event.target.closest("[data-task-open]");
    if (!button) return;
    selected = { id: button.dataset.taskOpen, fleet: button.dataset.taskFleet || "" };
    opener = button; openerIdentity = { ...selected, panel: button.closest("#attention, #tasks")?.id };
    detail(); $("task-detail-refresh").hidden = true;
    dialog.showModal(); $("task-detail-close").focus();
  });
  $("task-detail-close").onclick = closeDetail;
  $("task-detail-refresh").onclick = () => { detail(); $("task-detail-refresh").hidden = true; };
  dialog.addEventListener("close", () => {
    selected = null;
    if (!openerIdentity) return;
    const rail = $("rail-right"), buttons = [...rail.querySelectorAll("[data-task-open]")];
    const matches = button => button.dataset.taskOpen === openerIdentity.id
      && (button.dataset.taskFleet || "") === openerIdentity.fleet;
    const panelButtons = openerIdentity.panel
      ? [...($(openerIdentity.panel)?.querySelectorAll("[data-task-open]") || [])] : [];
    const replacement = panelButtons.find(matches) || buttons.find(matches);
    const focusTarget = opener?.isConnected ? opener : replacement || buttons[0] || rail;
    if (focusTarget === rail) rail.tabIndex = -1;
    focusTarget.focus(); opener = null; openerIdentity = null;
  });
  $("work-body").addEventListener("input", saveDraft);
  $("work-recipient").onchange = () => {
    saveDraft(); target = { ...target, recipient: $("work-recipient").value };
    notice = ""; paint({ restoreDraft: true });
  };
  $("work-reset").onclick = () => {
    saveDraft(); target = { ...target, task_id: null }; kind = "message";
    notice = ""; paint({ restoreDraft: true }); $("work-body").focus();
  };
  const requestLabel = request => `${request.kind} to ${recipientLabel(request)}${request.target.task_id ? ` · task ${request.target.task_id}` : ""}`;
  function receiptNotice(status, simulation) {
    const prefix = simulation ? "Example receipt: " : "";
    return prefix + ({ delivered: simulation ? "simulated delivery confirmed. No real bot received this." : "delivery confirmed by the host.",
      recorded: "recorded; delivery is not yet confirmed. Check this receipt again.",
      rejected: "request refused. Nothing was delivered. You may edit a new request." })[status];
  }
  $("work-form").onsubmit = async event => {
    event.preventDefault();
    if (!context || !target || sending) return;
    saveDraft(); const originContext = context, token = epoch, selection = selectionKey();
    let request, restoreDraft = false;
    try {
      if (typeof globalThis.crypto?.randomUUID !== "function")
        throw new Error("A safe request ID is unavailable. Nothing was sent.");
      let requestId;
      try { requestId = globalThis.crypto.randomUUID(); }
      catch { throw new Error("A safe request ID is unavailable. Nothing was sent."); }
      if (state.pending.some(p => p.request_id === requestId) || state.discarded.has(requestId))
        throw new Error("This request ID was already used. Nothing was sent; check its original receipt.");
      request = state.begin(context, kind, target, $("work-body").value, requestId);
      sending = true; inFlightRequest = request.request_id; notice = "Sending…"; paint();
      const receipt = await api.sendAction(request);
      const status = state.accept(request, receipt);
      if (token === epoch) {
        notice = `${requestLabel(request)}: ${receiptNotice(status, originContext.simulation)}`;
        restoreDraft = status === "delivered" && selection === selectionKey();
      }
      refresh();
    } catch (error) {
      if (request && error.effect === "not_started") {
        // Only this fresh, owned send row is resolved. Receipt lookups never
        // clear older IDs merely because a new invocation did not start.
        try {
          state.accept(request, { ...state.metadata(request), version: 1, status: "rejected" });
          if (room === originContext.room)
            notice = "This submission was refused before delivery; your draft is kept.";
        } catch {
          if (room === originContext.room)
            notice = "This submission was refused before delivery, but its saved row could not be updated. Your draft and original request ID are kept.";
        }
      } else if (token === epoch) notice = request
        ? `${requestLabel(request)}: Outcome unknown. Your draft is kept. Check the original receipt before sending again.`
        : error.message;
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
    if (!button || !context) return;
    const request = state.pending.find(p => p.request_id === (discardButton ? button.dataset.discard : button.dataset.request) && scopeKey(p.scope) === scopeKey(context.scope));
    if (!request || request.request_id === inFlightRequest) return;
    if (discardButton) {
      if (!globalThis.confirm(`Request ${request.request_id} may already have been delivered or may still be delivered. Copy this ID before discarding: it is retained only until this tab reloads. Discarding stops saving its receipt and lets you send a new request, which could duplicate the original. Nothing will be cancelled or resent. Discard this saved request?`)) return;
      try { state.discard(request); notice = `Discarded saved request ${request.request_id}. Its outcome remains unknown; nothing was cancelled or resent.`; }
      catch (error) { notice = error.message; }
      paint(); return;
    }
    const token = epoch, simulation = context.simulation, selection = selectionKey();
    const label = requestLabel(request);
    button.disabled = true;
    try {
      const status = state.accept(request, await api.actionReceipt(request));
      if (token === epoch) notice = `${label}: ${receiptNotice(status, simulation)}`;
      refresh();
    } catch {
      if (token === epoch) notice = `${label}: Receipt unavailable or mismatched. Outcome remains unknown; nothing was resent.`;
    } finally { if (token === epoch) paint({ restoreDraft: selection === selectionKey() }); }
  };
  paint();
  return { setRoom, update, invalidate, pause };
}
