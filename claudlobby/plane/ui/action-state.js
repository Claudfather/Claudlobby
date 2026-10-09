// UI request bookkeeping only. The transport must authorize every operation.
// Draft bodies stay in memory; persisted pending rows contain metadata only.
const VERBS = new Set(["message", "feedback", "nudge"]);
const FIELDS = ["workspace", "host", "fleet", "viewer"];
const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 240;
export const scopeKey = scope => JSON.stringify(FIELDS.map(key => scope?.[key]));
export const targetKey = target => JSON.stringify([target?.recipient, target?.task_id || ""]);
const rowKey = row => JSON.stringify([scopeKey(row.scope), row.kind, targetKey(row.target)]);
const validScope = scope => scope && FIELDS.every(key => bounded(scope[key]));
const validTarget = target => target && bounded(target.recipient)
  && (target.task_id === null || bounded(target.task_id));
const validRow = row => row && bounded(row.request_id) && validScope(row.scope)
  && VERBS.has(row.kind) && validTarget(row.target) && Number.isFinite(Date.parse(row.submitted_at));

export function validContext(context) {
  return context?.version === 1 && validScope(context.scope)
    && typeof context.simulation === "boolean"
    && Array.isArray(context.recipients) && context.recipients.length > 0
    && context.recipients.length <= 100
    && context.recipients.every(r => bounded(r.id) && bounded(r.label))
    && new Set(context.recipients.map(r => r.id)).size === context.recipients.length
    && Array.isArray(context.actions) && context.actions.every(a => VERBS.has(a))
    && (!context.actions.some(a => a !== "message")
      || context.recipients.filter(r => r.lead === true).length === 1);
}

export function sameReceipt(request, receipt) {
  return receipt?.version === 1 && validRow(receipt)
    && receipt.request_id === request.request_id && rowKey(receipt) === rowKey(request)
    && ["recorded", "delivered", "rejected"].includes(receipt.status);
}

export class ActionState {
  constructor(storage, key = "plane.pending-actions.v1") {
    this.storage = storage;
    this.key = key;
    this.drafts = new Map();
    this.sentBodies = new Map();
    this.pending = [];
    this.storageError = false;
    try {
      const raw = storage.getItem(key);
      if (raw && raw.length > 50000) throw new Error("Pending data too large");
      const rows = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(rows) || rows.length > 20 || !rows.every(validRow))
        throw new Error("Pending data cannot be read");
      this.pending = rows.map(row => this.metadata(row));
    } catch { this.storageError = true; }
  }

  metadata(row) {
    return { request_id: row.request_id, kind: row.kind,
      scope: Object.fromEntries(FIELDS.map(key => [key, row.scope[key]])),
      target: { recipient: row.target.recipient, task_id: row.target.task_id },
      submitted_at: row.submitted_at };
  }

  draft(row, value) {
    const key = rowKey(row);
    if (value === undefined) return this.drafts.get(key) || "";
    if (value) this.drafts.set(key, value.slice(0, 2000));
    else this.drafts.delete(key);
  }

  unresolved(row) { return this.pending.find(p => rowKey(p) === rowKey(row)); }

  begin(context, kind, target, body, requestId) {
    if (!validContext(context) || !context.actions.includes(kind)
        || !context.recipients.some(r => r.id === target.recipient)
        || (kind !== "message" && !context.recipients.some(r => r.id === target.recipient && r.lead === true))
        || !validTarget(target) || (kind !== "message" && !target.task_id))
      throw new Error("This action is not available for this target.");
    if (typeof body !== "string" || !body.trim() || body.length > 2000)
      throw new Error("Enter a message of up to 2,000 characters.");
    const request = { scope: context.scope, kind, target,
      request_id: requestId, submitted_at: new Date().toISOString() };
    if (!validRow(request)) throw new Error("Invalid request metadata.");
    if (this.unresolved(request)) throw new Error("Check the original receipt before sending again.");
    if (this.storageError || this.pending.length >= 20)
      throw new Error("Pending requests cannot be saved. Nothing was sent.");
    const rows = [...this.pending, this.metadata(request)];
    // Must succeed BEFORE the adapter can send. Never lose an uncertain ID.
    this.storage.setItem(this.key, JSON.stringify(rows));
    this.pending = rows;
    this.sentBodies.set(requestId, body);
    return { ...this.metadata(request), body };
  }

  accept(request, receipt) {
    if (!sameReceipt(request, receipt)) throw new Error("Receipt does not match this request.");
    if (receipt.status !== "recorded") {
      const rows = this.pending.filter(p => p.request_id !== request.request_id
        || rowKey(p) !== rowKey(request));
      this.storage.setItem(this.key, JSON.stringify(rows));
      this.pending = rows;
    }
    // A new draft typed while an old request was pending must not be cleared.
    const body = request.body ?? this.sentBodies.get(request.request_id);
    if (receipt.status === "delivered" && body !== undefined
        && this.draft(request) === body) this.draft(request, "");
    if (receipt.status !== "recorded") this.sentBodies.delete(request.request_id);
    return receipt.status;
  }
}
