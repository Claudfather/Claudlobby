import { ActionState, scopeKey, validPermissionContext, samePermissionReceipt } from "./action-state.js";
import { esc } from "./panel-state.js";

// Presentation and pending UUIDs only; all authority and decisions stay on host.
export function createPermissionPanel({api, storage}) {
  const state = new ActionState(storage, 'plane.pending-permissions.v1');
  let element = null, task = null, context = null, epoch = 0, busy = false, form = null, note = '';
  const labels = {approve:'Approve', decline:'Decline', revoke:'Revoke'};
  const target = p => ({permission_id:p.permission_id, task_id:p.task_id,
    scope_sha256:p.scope_sha256, release_id:context.release_id});
  const draftRow = (p, decision) => ({version:2, kind:'permission', scope:context.scope, target:target(p), decision});
  const visible = row => context && row.target.task_id === task.task_id
    && ['workspace','host','fleet'].every(k => row.scope[k] === context.scope[k]);
  const pending = p => state.pending.some(row => visible(row) && row.target.permission_id === p.permission_id
    && (p.state !== 'approved' || row.decision === 'revoke'));
  function paint() {
    if (!element) return;
    if (!context) {
      element.innerHTML = `<h3>Permissions</h3><p class="note" role="status">${esc(note)}</p>`;
      return;
    }
    const cards = context.permissions.map(p => {
      if (p.state === 'unavailable') return `<div class="task-question"><b>Permission history unavailable</b>
        <p class="note">This request cannot be decided until its host record is available.</p><code>${esc(p.permission_id)}</code></div>`;
      const options = p.state === 'pending' ? ['approve','decline'] : p.state === 'approved' ? ['revoke'] : [];
      const active = form?.id === p.permission_id;
      return `<section class="task-assignment"><h4>${esc(p.scope.action[0].toUpperCase() + p.scope.action.slice(1))} permission <span class="tag">${esc(p.state)}</span></h4>
        <p class="note">Requested by ${esc(p.requester.alias)}</p>
        <dl class="task-facts"><dt>Target</dt><dd><pre class="task-record-body">${esc(p.scope.target)}</pre></dd>
        <dt>Limits</dt><dd><pre class="task-record-body">${esc(p.scope.constraints)}</pre></dd></dl>
        <p>${esc(p.reason)}</p>${p.decision_reason ? `<p class="note">${esc(p.decided_by.alias)}: ${esc(p.decision_reason)}</p>` : ''}
        ${active ? `<form id="permission-decision-form"><label for="permission-decision-reason">Reason for ${esc(form.decision === 'approve' ? 'approval' : form.decision === 'decline' ? 'declining' : 'revocation')}</label>
          <textarea id="permission-decision-reason" rows="2" maxlength="2000" required${busy ? ' disabled' : ''}>${esc(state.draft(draftRow(p,form.decision)))}</textarea>
          <p class="note">Your decision applies to the exact target and limits shown above.</p>
          <button class="pill" type="submit"${busy || pending(p) || state.storageError ? ' disabled' : ''}>${busy ? 'Recording…' : 'Confirm ' + esc(form.decision === 'approve' ? 'approval' : form.decision === 'decline' ? 'decline' : 'revocation')}</button>
          <button class="pill ghost" type="button" data-permission-cancel="1"${busy ? ' disabled' : ''}>Cancel</button></form>`
        : options.map(decision => `<button class="pill${decision === 'approve' ? '' : ' ghost'}" type="button" data-permission-decision="${decision}" data-permission-id="${esc(p.permission_id)}"${busy || pending(p) || state.storageError || decision === 'approve' && !['queued','assigned','active','blocked'].includes(task.state) ? ' disabled' : ''}>${labels[decision]}…</button>`).join(' ')}
        <details class="task-record-details"><summary>Record identifiers</summary><code>${esc(p.permission_id)}</code><p class="note">Scope ${esc(p.scope_sha256)}</p></details></section>`;
    }).join('');
    element.innerHTML = `<h3>Permissions</h3><p class="note">Review requests for consequential actions here. Task comments and nudges do not grant permission.</p>
      ${cards || '<p class="detail-empty">No proven permission requests in this view.</p>'}
      ${context.has_more ? '<p class="note">More records exist beyond this view. Use the host’s task permission commands to inspect an older request.</p>' : ''}
      <p role="status" aria-live="polite">${esc(note)}</p>
      ${state.pending.filter(visible).map(row => `<div class="pending-action"><div><b>Saved decision receipt</b><p>${esc(labels[row.decision])} · ${esc(row.target.permission_id)}</p><code>${esc(row.request_id)}</code>
        ${scopeKey(row.scope) !== scopeKey(context.scope) ? '<p class="note">Saved under earlier access. Restore that access to inspect this receipt.</p>' : ''}</div>
        <button class="pill ghost" type="button" data-permission-receipt="${esc(row.request_id)}"${busy || scopeKey(row.scope) !== scopeKey(context.scope) ? ' disabled' : ''}>Check receipt</button>
        <button class="pill ghost" type="button" data-permission-discard="${esc(row.request_id)}"${busy ? ' disabled' : ''}>Remove saved receipt</button></div>`).join('')}
      ${state.storageError ? '<p class="note">Saved decisions cannot be read or saved safely. New decisions are disabled.</p>' : ''}
      ${state.corruptStorage ? `<p class="note">Save these readable request IDs before clearing damaged records: ${state.recoverable.map(row => esc(row.request_id)).join(', ')}</p><button type="button" class="pill ghost" data-permission-clear="1"${busy ? ' disabled' : ''}>Clear damaged receipt storage</button>` : ''}`;
    const field = document.getElementById('permission-decision-reason');
    if (field && form) field.oninput = () => {
      const p = context?.permissions.find(p => p.permission_id === form?.id);
      if (p) state.draft(draftRow(p,form.decision),field.value);
    };
    const node = document.getElementById('permission-decision-form');
    if (node) node.onsubmit = send;
    element.onclick = click;
  }
  function result(request, receipt) {
    if (!samePermissionReceipt(request, receipt) || receipt.status !== 'recorded')
      throw new Error('The outcome is unconfirmed. Check this original receipt before deciding again.');
    state.accept(request,receipt);
    context = {...context,permissions:context.permissions.map(p => p.permission_id === receipt.permission.permission_id ? receipt.permission : p)};
    form = null;
    const delivery = receipt.notification === 'received' ? 'The requesting bot received the notice.'
      : receipt.notification === 'not_requested' ? 'This host did not request a bot notification.'
      : receipt.notification === 'failed' ? 'The bot’s copy of the notice was altered or truncated.'
      : 'Bot notification is unconfirmed. Its saved receipt remains available to check; nothing will be resent.';
    note = `Decision recorded. Permission is currently ${receipt.permission.state}. ${delivery} Check task reports for execution results.`;
  }
  async function send(event) {
    event.preventDefault();
    if (busy || !context || !form) return;
    const p = context.permissions.find(p => p.permission_id === form.id);
    if (!p || pending(p)) return;
    const token = epoch, selected = context, decision = form.decision;
    busy = true;
    try {
      const body = document.getElementById('permission-decision-reason').value;
      state.draft(draftRow(p,decision),body);
      const request = state.prepare(selected,target(p),body,globalThis.crypto?.randomUUID?.(),decision);
      paint();
      const prepared = await api.prepareAction(request);
      if (token !== epoch || context !== selected) return;
      const retained = state.beginPrepared(selected,request,prepared);
      const receipt = await api.sendAction(retained);
      if (token !== epoch || context !== selected) return;
      result(retained,receipt);
    } catch (error) {
      if (token === epoch) note = error.effect === 'not_started'
        ? 'This submission did not start. Its saved request ID is kept for inspection; refresh the request before deciding again.'
        : error.message || 'The outcome is unconfirmed. Check the original receipt.';
    } finally { busy = false; paint(); }
  }
  async function inspect(row) {
    if (busy || !context || scopeKey(row.scope) !== scopeKey(context.scope)) return;
    const token = epoch, selected = context; busy = true; paint();
    try {
      const receipt = await api.actionReceipt(row);
      if (token === epoch && context === selected) result(row,receipt);
    } catch (error) { if (token === epoch) note = error.message || 'Receipt unavailable. No decision was resent.'; }
    finally { busy = false; paint(); }
  }
  function click(event) {
    const button = event.target.closest('[data-permission-decision], [data-permission-cancel], [data-permission-receipt], [data-permission-discard], [data-permission-clear]');
    if (!button || button.disabled || busy || !context) return;
    const data = button.dataset;
    if (data.permissionDecision) { form = {id:data.permissionId, decision:data.permissionDecision}; note = ''; paint(); document.getElementById('permission-decision-reason')?.focus(); }
    else if (data.permissionCancel) { form = null; paint(); }
    else if (data.permissionReceipt) { const row = state.pending.find(r => r.request_id === data.permissionReceipt && visible(r)); if (row) void inspect(row); }
    else if (data.permissionDiscard) {
      const row = state.pending.find(r => r.request_id === data.permissionDiscard && visible(r));
      if (row && globalThis.confirm(`Remove saved receipt ${row.request_id}? This does not undo the decision or prove its outcome. Copy the ID first if you still need to inspect it.`)) {
        try { state.discard(row); note = 'Saved receipt removed from this tab. The host decision is unchanged.'; }
        catch (error) { note = error.message; } paint();
      }
    } else if (data.permissionClear && globalThis.confirm('Clear damaged receipt storage? Copy the request IDs first. This does not undo or resend any host decision.')) {
      try { state.clearCorruptStorage(); note = 'Saved metadata cleared. Refresh the task before deciding.'; }
      catch (error) { note = error.message; } paint();
    }
  }
  async function show(node, selectedTask) {
    close(); element = node; task = selectedTask;
    if (!element) return;
    const token = epoch;
    note = typeof api.permissionContext === 'function' ? 'Checking permission requests…' : 'Permission decisions are unavailable through this connection.'; paint();
    if (typeof api.permissionContext !== 'function') return;
    try {
      const value = await api.permissionContext(task.fleet,task.task_id);
      if (token !== epoch) return;
      if (!validPermissionContext(value) || value.room !== task.fleet || value.task_id !== task.task_id
          || !['prepareAction','sendAction','actionReceipt'].every(k => typeof api[k] === 'function')) throw new Error();
      context = JSON.parse(JSON.stringify(value)); note = '';
    } catch { if (token === epoch) note = 'Permission access is unavailable. Check this computer’s owner approval, then refresh the task.'; }
    if (token === epoch) paint();
  }
  function close() { ++epoch; element = null; task = null; context = null; form = null; }
  function pause(scope) {
    if (scope && (!context || scopeKey(context.scope) !== scopeKey(scope))) return;
    ++epoch; context = null; form = null; note = 'Permission access paused. Saved receipt IDs are retained; nothing was resent.'; paint();
  }
  return {show,close,pause};
}
