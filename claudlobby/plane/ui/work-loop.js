import { ActionState, scopeKey, targetKey, validContext } from "/action-state.js";
import { esc, ago, stateBlock } from "/panel-state.js";

// One presentation for direct and embedded Plane. No transport is constructed
// here: the default read-only client never supplies a write capability.
export function mountWorkLoop({ api, renderThread, refresh }) {
  const root = document.getElementById("work-loop");
  const dialog = document.getElementById("task-detail");
  let storage;
  try { storage = sessionStorage; } catch { storage = null; }
  const state = new ActionState(storage);
  let context = null, room = null, epoch = 0, board = null, channel = null;
  let selected = null, target = null, kind = "message", sending = false, opener = null;
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
    <div id="work-pending"></div>`;
  const $ = id => document.getElementById(id);
  const row = () => context && target ? { scope: context.scope, kind, target } : null;
  const selectionKey = () => row() ? JSON.stringify([scopeKey(context.scope), kind, targetKey(target)]) : "";
  const readDraft = () => row() ? state.draft(row()) : "";
  function saveDraft() {
    if (!row()) return;
    try { state.draft(row(), $("work-body").value); }
    catch (error) { notice = error.message; }
  }
  function pendingRows() {
    const rows = context ? state.pending.filter(p => scopeKey(p.scope) === scopeKey(context.scope)) : [];
    $("work-pending").innerHTML = rows.map(p => `<div class="pending-action">
      <div><b>Awaiting confirmation</b><p>${esc(p.kind)} · ${esc(p.target.recipient)}${p.target.task_id ? ` · task ${esc(p.target.task_id)}` : ""}</p>
      <small>Submitted ${esc(ago(p.submitted_at))} · ${esc(p.request_id)}</small></div>
      <button class="pill ghost" type="button" data-request="${esc(p.request_id)}">Check receipt</button></div>`).join("");
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
    }
    $("work-notice").textContent = usable && state.storageError
      ? "This tab cannot safely save or read pending receipts. Sending is disabled; existing requests were not resent."
      : notice;
    pendingRows();
  }
  function closeDetail() {
    if (dialog.open) dialog.close();
    selected = null;
  }
  function setRoom(fleet) {
    saveDraft();
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
        target = { recipient: (value.recipients.find(r => r.lead) || value.recipients[0]).id, task_id: null };
        kind = "message";
        notice = value.simulation ? "Example actions are recorded only by the test service. No real agents receive them." : "";
      } else notice = "This host has not enabled an authorized browser action service.";
      paint({ restoreDraft: true });
    }).catch(() => {
      if (token !== epoch) return;
      notice = "Could not check action access. Your draft is kept in this tab.";
      paint();
    });
  }
  function detail() {
    if (!selected) return;
    const task = board?.state === "ok" ? board.data.tasks.find(t => t.task_id === selected.id && (t.fleet || "") === selected.fleet) : null;
    const content = $("task-detail-content");
    if (!task) {
      content.innerHTML = '<h2 id="task-detail-title">Task details are unavailable</h2>' + stateBlock("unavailable", null, null, { label: "No current task record", detail: "The selected task is not in the latest board response. Refresh the team view to check again." });
      return;
    }
    const assignment = task.current_assignment;
    content.innerHTML = `<p class="eyebrow">${esc(task.fleet || room)} · TASK</p>
      <h2 id="task-detail-title">${esc(task.title || task.task_id)}</h2>
      <p class="task-state"><b>${esc(task.state || "State unknown")}</b> · ${esc(task.task_id)}</p>
      <dl class="task-facts"><dt>Assigned to</dt><dd>${esc(assignment?.assignee_short || assignment?.assignee_alias || "No current assignment")}</dd>
      <dt>Last recorded action</dt><dd>${esc(task.last_event?.event || "No action recorded")} · ${esc(ago(task.last_event?.occurred_at))}</dd>
      <dt>Delivery evidence</dt><dd>${esc(task.delivery?.integrity || "No confirmed delivery evidence in this view")}</dd></dl>
      ${task.attention_question ? `<div class="task-question"><b>Needs your input</b><p>${esc(task.attention_question)}</p></div>` : ""}
      ${task.resolved === false ? '<p class="task-question">Task history has unresolved links. Its result cannot be treated as confirmed.</p>' : ""}
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
        target = { recipient: (context.recipients.find(r => r.lead) || context.recipients[0]).id, task_id: task.task_id };
        opener = null;
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
    opener = button;
    detail(); $("task-detail-refresh").hidden = true;
    dialog.showModal(); $("task-detail-close").focus();
  });
  $("task-detail-close").onclick = closeDetail;
  $("task-detail-refresh").onclick = () => { detail(); $("task-detail-refresh").hidden = true; };
  dialog.addEventListener("close", () => { selected = null; if (opener?.isConnected) opener.focus(); });
  $("work-body").addEventListener("input", saveDraft);
  $("work-recipient").onchange = () => {
    saveDraft(); target = { ...target, recipient: $("work-recipient").value };
    notice = ""; paint({ restoreDraft: true });
  };
  $("work-reset").onclick = () => {
    saveDraft(); target = { ...target, task_id: null }; kind = "message";
    notice = ""; paint({ restoreDraft: true }); $("work-body").focus();
  };
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
    let request;
    try {
      request = state.begin(context, kind, target, $("work-body").value, crypto.randomUUID());
      sending = true; notice = "Sending…"; paint();
      const receipt = await api.sendAction(request);
      const status = state.accept(request, receipt);
      if (token === epoch && selection === selectionKey()) {
        notice = receiptNotice(status, originContext.simulation);
        paint({ restoreDraft: status === "delivered" });
      }
      refresh();
    } catch (error) {
      if (token === epoch && selection === selectionKey()) notice = request
        ? "Outcome unknown. Your draft is kept. Check the original receipt before sending again."
        : error.message;
    } finally {
      sending = false;
      paint();
    }
  };
  $("work-pending").onclick = async event => {
    const button = event.target.closest("[data-request]");
    if (!button || !context) return;
    const request = state.pending.find(p => p.request_id === button.dataset.request && scopeKey(p.scope) === scopeKey(context.scope));
    if (!request) return;
    const token = epoch, simulation = context.simulation;
    button.disabled = true;
    try {
      const status = state.accept(request, await api.actionReceipt(request));
      if (token === epoch) notice = receiptNotice(status, simulation);
      refresh();
    } catch {
      if (token === epoch) notice = "Receipt unavailable or mismatched. Outcome remains unknown; nothing was resent.";
    } finally { if (token === epoch) paint({ restoreDraft: true }); }
  };
  paint();
  return { setRoom, update };
}
