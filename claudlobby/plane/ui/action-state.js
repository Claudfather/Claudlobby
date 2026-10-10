// UI request bookkeeping only. The transport must authorize every operation.
// Draft bodies stay in memory; persisted pending rows contain metadata only.
const VERBS = new Set(["message", "feedback", "nudge"]);
const FIELDS = ["workspace", "host", "fleet", "viewer"];
const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 240;
export const scopeKey = scope => JSON.stringify(FIELDS.map(key => scope?.[key]));
const targetKey = (target, version) => JSON.stringify(version === 2
  ? [target?.recipient, target?.task_id, target?.assignment_id, target?.release_id]
  : [target?.recipient, target?.task_id || ""]);
export const rowKey = row => JSON.stringify([scopeKey(row.scope), row.kind, targetKey(row.target, row.version)]);
const validScope = scope => scope && FIELDS.every(key => bounded(scope[key]));
const validTarget = target => target && bounded(target.recipient)
  && (target.task_id === null || bounded(target.task_id));
const validLegacyRow = row => row && row.version !== 2 && bounded(row.request_id) && validScope(row.scope)
  && VERBS.has(row.kind) && validTarget(row.target) && bounded(row.submitted_at)
  && Number.isFinite(Date.parse(row.submitted_at));

const exact = (value, keys) => value && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...keys].sort().join();
const canonical = (value, prefix) => typeof value === "string" && new RegExp(`^${prefix}_[0-9a-f]{32}$`).test(value);
const release = value => typeof value === "string" && /^r-[0-9a-f]{64}$/.test(value);
const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const timestamp = value => bounded(value) && /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
export const validTaskActionTarget = target => exact(target, ["recipient", "task_id", "assignment_id", "release_id"])
  && canonical(target.recipient, "actor") && canonical(target.task_id, "wi")
  && (target.assignment_id === null || canonical(target.assignment_id, "asg")) && release(target.release_id);
export const validNudgeTarget = validTaskActionTarget;
const taskActionKeys = ["version", "request_id", "kind", "scope", "target", "submitted_at", "semantic_sha256"];
const taskKinds = new Set(["nudge", "feedback"]);
const validTaskActionRow = row => row?.version === 2 && taskKinds.has(row.kind) && uuid(row.request_id)
  && exact(row.scope, FIELDS) && validScope(row.scope) && validTaskActionTarget(row.target) && timestamp(row.submitted_at)
  && typeof row.semantic_sha256 === "string" && /^[0-9a-f]{64}$/.test(row.semantic_sha256);
const validRow = row => row?.version === 2 ? exact(row, taskActionKeys) && validTaskActionRow(row) : validLegacyRow(row);
const logicalKey = row => row.version === 2
  ? JSON.stringify([scopeKey(row.scope), row.kind, row.target.recipient, row.target.task_id]) : rowKey(row);
export function validTaskActionContext(context, kind = context?.actions?.[0]) {
  return taskKinds.has(kind) && context?.version === 2 && context.simulation === false && validScope(context.scope)
    && exact(context.scope, FIELDS) && bounded(context.room) && context.scope.fleet === context.room && release(context.release_id)
    && Array.isArray(context.actions) && context.actions.length === 1 && context.actions[0] === kind
    && Array.isArray(context.recipients) && context.recipients.length === 1
    && canonical(context.recipients[0].id, "actor") && bounded(context.recipients[0].label) && context.recipients[0].lead === true;
}
export const validNudgeContext = context => validTaskActionContext(context, "nudge");
export const validFeedbackContext = context => validTaskActionContext(context, "feedback");
export function samePreparation(request, prepared) {
  return exact(prepared, taskActionKeys) && validTaskActionRow(prepared)
    && prepared.request_id === request.request_id && prepared.submitted_at === request.submitted_at
    && rowKey(prepared) === rowKey(request);
}

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
  if (request?.version === 2) return exact(receipt, [...taskActionKeys, "status"]) && receipt.version === 2 && validTaskActionRow(receipt)
    && receipt.request_id === request.request_id && receipt.submitted_at === request.submitted_at
    && receipt.semantic_sha256 === request.semantic_sha256 && rowKey(receipt) === rowKey(request)
    && ["recorded", "delivered", "rejected"].includes(receipt.status);
  return receipt?.version === 1 && validLegacyRow(receipt)
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
    this.recoverable = [];
    this.storageError = false;
    this.corruptStorage = false;
    this.discarded = new Map();
    let read = false;
    try {
      const raw = storage.getItem(key);
      read = true;
      const rows = raw ? JSON.parse(raw) : [];
      // Preserve every readable ID before refusing a mixed/oversized store.
      // These rows are view/copy only: receipt/discard writes must not overwrite
      // the invalid records before the explicit recovery confirmation.
      if (Array.isArray(rows)) this.recoverable = rows.filter(validRow).map(row => this.metadata(row));
      if ((raw && raw.length > 50000) || !Array.isArray(rows) || rows.length > 20 || !rows.every(validRow))
        throw new Error("Pending data cannot be read");
      this.pending = this.recoverable;
      this.recoverable = [];
    } catch { this.storageError = true; this.corruptStorage = read; }
  }

  metadata(row) {
    return { ...(row.version === 2 ? { version: 2, semantic_sha256: row.semantic_sha256 } : {}),
      request_id: row.request_id, kind: row.kind,
      scope: Object.fromEntries(FIELDS.map(key => [key, row.scope[key]])),
      target: { recipient: row.target.recipient, task_id: row.target.task_id,
        ...(row.version === 2 ? { assignment_id: row.target.assignment_id, release_id: row.target.release_id } : {}) },
      submitted_at: row.submitted_at };
  }

  draft(row, value) {
    // A task reason follows its logical task; pending wire metadata stays frozen.
    const key = logicalKey(row);
    if (value === undefined) return this.drafts.get(key) || "";
    if (value) this.drafts.set(key, value.slice(0, 2000));
    else this.drafts.delete(key);
  }

  unresolved(row) { return this.pending.find(p => logicalKey(p) === logicalKey(row)); }

  // Must succeed BEFORE the adapter can send. Never lose an uncertain ID.
  persist(rows) {
    try {
      const serialized = JSON.stringify(rows);
      if (serialized.length > 50000) throw new Error();
      this.storage.setItem(this.key, serialized);
    } catch { throw new Error("Pending requests cannot be saved. Nothing was sent."); }
    this.pending = rows;
  }

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
    this.persist([...this.pending, this.metadata(request)]);
    this.sentBodies.set(requestId, body);
    return { ...this.metadata(request), body };
  }

  prepare(context, target, body, requestId) {
    if (!validTaskActionContext(context) || !validTaskActionTarget(target)
        || target.recipient !== context.recipients[0].id || target.release_id !== context.release_id)
      throw new Error("This task action is unavailable. Refresh the task and select it again.");
    if (typeof body !== "string" || !body.trim() || body.length > 2000 || body.includes("\u0000") || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(body))
      throw new Error(context.actions[0] === "feedback" ? "Enter a comment of up to 2,000 characters." : "Enter a reason of up to 2,000 characters.");
    const request = { version: 2, request_id: requestId, kind: context.actions[0], scope: { ...context.scope },
      target: { ...target }, submitted_at: new Date().toISOString(), body };
    if (!uuid(requestId)) throw new Error("A safe request ID is unavailable. Nothing was sent.");
    if (this.unresolved(request)) throw new Error("Check the original receipt before sending again.");
    if (this.pending.some(p => p.request_id === requestId) || this.discarded.has(requestId))
      throw new Error("This request ID was already used. Nothing was sent.");
    if (this.storageError || this.pending.length >= 20)
      throw new Error("Pending requests cannot be saved. Nothing was sent.");
    return request; // Preparation is nonmutating; no saved row or sent body.
  }

  beginPrepared(context, request, prepared) {
    if (!samePreparation(request, prepared) || request.kind !== context?.actions?.[0]
        || scopeKey(request.scope) !== scopeKey(context.scope)) throw new Error("Task action preparation did not match. Nothing was sent.");
    // Recheck the capability and logical pending block after the async prepare.
    this.prepare(context, request.target, request.body, request.request_id);
    const row = this.metadata(prepared);
    this.persist([...this.pending, row]);
    this.sentBodies.set(row.request_id, request.body);
    return { ...row, body: request.body };
  }

  // Only the explicit, confirmed UI recovery action calls this. Never clear
  // unreadable records during startup or send, since they may hold uncertain IDs.
  clearCorruptStorage() {
    if (!this.corruptStorage) return;
    try { this.storage.setItem(this.key, "[]"); }
    catch { throw new Error("Saved requests could not be cleared. Sending remains disabled."); }
    this.pending = [];
    this.recoverable = [];
    this.storageError = false;
    this.corruptStorage = false;
  }

  discard(request) {
    const rows = this.pending.filter(p => p.request_id !== request.request_id
      || rowKey(p) !== rowKey(request));
    if (rows.length === this.pending.length) return;
    try { this.storage.setItem(this.key, JSON.stringify(rows)); }
    catch { throw new Error("Saved request could not be discarded. Its receipt is still pending."); }
    this.pending = rows;
    // Retain IDs and context for this tab, even after releasing its send block.
    this.discarded.set(request.request_id, this.metadata(request));
    this.sentBodies.delete(request.request_id);
  }

  accept(request, receipt) {
    if (!sameReceipt(request, receipt)) throw new Error("Receipt does not match this request.");
    if (receipt.status !== "recorded") {
      const rows = this.pending.filter(p => p.request_id !== request.request_id
        || rowKey(p) !== rowKey(request));
      this.storage.setItem(this.key, JSON.stringify(rows));
      this.pending = rows;
      const discarded = this.discarded.get(request.request_id);
      if (discarded && rowKey(discarded) === rowKey(request))
        this.discarded.delete(request.request_id);
    }
    // A new draft typed while an old request was pending must not be cleared.
    const body = request.body ?? this.sentBodies.get(request.request_id);
    if (receipt.status === "delivered" && body !== undefined
        && this.draft(request) === body) this.draft(request, "");
    if (receipt.status !== "recorded") this.sentBodies.delete(request.request_id);
    return receipt.status;
  }
}
