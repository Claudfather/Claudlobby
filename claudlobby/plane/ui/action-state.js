// UI request bookkeeping only. The transport must authorize every operation.
// Draft bodies stay in memory; persisted pending rows contain metadata only.
const VERBS = new Set(["message", "feedback", "nudge"]);
const FIELDS = ["workspace", "host", "fleet", "viewer"];
const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 240;
export const scopeKey = scope => JSON.stringify(FIELDS.map(key => scope?.[key]));
const targetKey = (target, version) => JSON.stringify(target?.permission_id
  ? [target.permission_id, target.task_id, target.scope_sha256, target.release_id] : version === 2
  ? [target?.recipient, target?.task_id, target?.assignment_id, target?.release_id]
  : [target?.recipient, target?.task_id || ""]);
export const rowKey = row => JSON.stringify([scopeKey(row.scope), row.kind, targetKey(row.target, row.version), ...(row.kind === "permission" ? [row.decision] : [])]);
const validScope = scope => scope && FIELDS.every(key => bounded(scope[key]));
const validTarget = target => target && bounded(target.recipient)
  && (target.task_id === null || bounded(target.task_id));
const validLegacyRow = row => row && row.version !== 2 && bounded(row.request_id) && validScope(row.scope)
  && VERBS.has(row.kind) && validTarget(row.target) && bounded(row.submitted_at)
  && Number.isFinite(Date.parse(row.submitted_at));

const exact = (value, keys) => value && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...keys].sort().join();
const canonicalPart = (value, prefix) => {
  if (typeof value !== "string") return null;
  const match = value.match(new RegExp(`^(?:([a-z][a-z0-9-]{0,31})::)?${prefix}_[0-9a-f]{32}$`));
  return match && match[0] === value ? { source: match[1] || "" } : null;
};
const release = value => typeof value === "string" && /^r-[0-9a-f]{64}$/.test(value);
const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const timestamp = value => bounded(value) && /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
export function validTaskActionTarget(target) {
  if (!exact(target, ["recipient", "task_id", "assignment_id", "release_id"]) || !release(target.release_id)) return false;
  const task = canonicalPart(target.task_id, "wi"), actor = canonicalPart(target.recipient, "actor");
  const assignment = target.assignment_id === null ? task : canonicalPart(target.assignment_id, "asg");
  return !!(task && actor && assignment && task.source === actor.source && task.source === assignment.source);
}
const taskActionKeys = ["version", "request_id", "kind", "scope", "target", "submitted_at", "semantic_sha256"];
const taskKinds = new Set(["nudge", "feedback"]);
const validTaskActionRow = row => row?.version === 2 && taskKinds.has(row.kind) && uuid(row.request_id)
  && exact(row.scope, FIELDS) && validScope(row.scope) && validTaskActionTarget(row.target) && timestamp(row.submitted_at)
  && typeof row.semantic_sha256 === "string" && /^[0-9a-f]{64}$/.test(row.semantic_sha256);
const validRow = row => row?.kind === "permission" ? validPermissionRow(row)
  : row?.version === 2 ? exact(row, taskActionKeys) && validTaskActionRow(row) : validLegacyRow(row);
const logicalKey = row => row.kind === "permission"
  ? JSON.stringify([scopeKey(row.scope), row.kind, row.target.permission_id]) : row.version === 2
  ? JSON.stringify([scopeKey(row.scope), row.kind, row.target.recipient, row.target.task_id]) : rowKey(row);
export function validTaskActionContext(context, kind = context?.actions?.[0]) {
  return taskKinds.has(kind) && context?.version === 2 && context.simulation === false && validScope(context.scope)
    && exact(context.scope, FIELDS) && bounded(context.room) && context.scope.fleet === context.room && release(context.release_id)
    && Array.isArray(context.actions) && context.actions.length === 1 && context.actions[0] === kind
    && Array.isArray(context.recipients) && context.recipients.length === 1
    && !!canonicalPart(context.recipients[0].id, "actor") && bounded(context.recipients[0].label) && context.recipients[0].lead === true
    && (!canonicalPart(context.recipients[0].id, "actor").source
      || context.room.startsWith(canonicalPart(context.recipients[0].id, "actor").source + " / "));
}
export const validNudgeContext = context => validTaskActionContext(context, "nudge");
export const validFeedbackContext = context => validTaskActionContext(context, "feedback");
const permissionKeys = ["version", "request_id", "kind", "scope", "target", "decision", "semantic_sha256"];
const digest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const text = (value, cap) => typeof value === "string" && value.trim().length > 0 && new TextEncoder().encode(value).length <= cap;
const sameSource = (...values) => values.every(v => v && v.source === values[0]?.source);
export function validPermissionTarget(target) {
  return exact(target, ["permission_id", "task_id", "scope_sha256", "release_id"])
    && sameSource(canonicalPart(target.permission_id, "perm"), canonicalPart(target.task_id, "wi"))
    && digest(target.scope_sha256) && release(target.release_id);
}
export function validPermissionRecord(value) {
  const task = canonicalPart(value?.task_id, "wi"), permission = canonicalPart(value?.permission_id, "perm");
  if (!sameSource(task, permission)) return false;
  if (value.state === "unavailable") return exact(value, ["permission_id", "task_id", "state"]);
  const actor = who => who && exact(who, ["uid", "alias"]) && sameSource(task, canonicalPart(who.uid, "actor")) && text(who.alias, 512);
  return ["pending", "approved", "declined", "revoked"].includes(value.state)
    && sameSource(task, canonicalPart(value.request_event_id, "ev"), value.assignment_id === null ? task : canonicalPart(value.assignment_id, "asg"))
    && actor(value.requester) && digest(value.scope_sha256) && text(value.reason, 2048)
    && exact(value.scope, ["action", "target", "constraints"]) && ["merge", "deploy", "spend", "delete"].includes(value.scope.action)
    && text(value.scope.target, 1024) && text(value.scope.constraints, 4096)
    && (value.state === "pending" ? value.decided_by === null && value.decision_reason === null
      : actor(value.decided_by) && text(value.decision_reason, 2048));
}
export function validPermissionContext(context) {
  const task = canonicalPart(context?.task_id, "wi"), host = canonicalPart(context?.scope?.host, "host");
  return context?.version === 2 && context.simulation === false && exact(context.scope, FIELDS) && validScope(context.scope)
    && bounded(context.room) && context.room === context.scope.fleet && release(context.release_id)
    && sameSource(task, host) && (!task.source || context.room.startsWith(task.source + " / "))
    && Array.isArray(context.actions) && context.actions.length === 1 && context.actions[0] === "permission"
    && typeof context.has_more === "boolean" && Array.isArray(context.permissions) && context.permissions.length <= 50
    && context.permissions.every(p => validPermissionRecord(p) && p.task_id === context.task_id)
    && new Set(context.permissions.map(p => p.permission_id)).size === context.permissions.length;
}
export function validPermissionRow(row) {
  return exact(row, permissionKeys) && row.version === 2 && row.kind === "permission" && uuid(row.request_id)
    && exact(row.scope, FIELDS) && validScope(row.scope) && validPermissionTarget(row.target)
    && sameSource(canonicalPart(row.scope.host, "host"), canonicalPart(row.target.task_id, "wi"))
    && ["approve", "decline", "revoke"].includes(row.decision) && digest(row.semantic_sha256);
}
export function samePermissionReceipt(request, receipt, allowUnknown = false) {
  if (!exact(receipt, [...permissionKeys, "status", "permission", "execution", "notification"])
      || !validPermissionRow(Object.fromEntries(permissionKeys.map(k => [k, receipt[k]])))
      || receipt.request_id !== request.request_id || receipt.semantic_sha256 !== request.semantic_sha256
      || rowKey(receipt) !== rowKey(request) || receipt.execution !== "not_requested" || receipt.notification !== "not_requested") return false;
  if (receipt.status === "unknown") return allowUnknown && receipt.permission === null;
  return receipt.status === "recorded" && validPermissionRecord(receipt.permission)
    && ({approve:["approved","revoked"],decline:["declined"],revoke:["revoked"]}[request.decision] || []).includes(receipt.permission.state)
    && receipt.permission.permission_id === request.target.permission_id
    && receipt.permission.task_id === request.target.task_id && receipt.permission.scope_sha256 === request.target.scope_sha256;
}

export function samePreparation(request, prepared) {
  if (request?.kind === "permission") return validPermissionRow(prepared) && prepared.request_id === request.request_id
    && rowKey(prepared) === rowKey(request);
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
  if (request?.kind === "permission") return samePermissionReceipt(request, receipt);
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
    if (row.kind === "permission") return {version: 2, request_id: row.request_id, kind: "permission", decision: row.decision,
      scope: {...row.scope}, target: {...row.target}, semantic_sha256: row.semantic_sha256};
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

  prepare(context, target, body, requestId, decision) {
    if (context?.actions?.[0] === "permission") {
      const permission = context.permissions?.find(p => p.permission_id === target?.permission_id);
      if (!validPermissionContext(context) || !validPermissionTarget(target) || !permission
          || permission.task_id !== target.task_id || permission.scope_sha256 !== target.scope_sha256
          || target.release_id !== context.release_id || !["approve", "decline", "revoke"].includes(decision)
          || permission.state !== (decision === "revoke" ? "approved" : "pending"))
        throw new Error("This permission changed. Refresh its details before deciding.");
      if (!text(body, 2048) || /[\u0000-\u0008\u000b-\u001f]/.test(body)
          || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(body)) throw new Error("Enter a reason of up to 2,048 bytes.");
      const request = {version: 2, kind: "permission", scope: {...context.scope}, target: {...target}, request_id: requestId, decision, body};
      if (!uuid(requestId)) throw new Error("A safe request ID is unavailable. Nothing was sent.");
      if (this.unresolved(request)) throw new Error("Check the original receipt before deciding again.");
      if (this.storageError || this.pending.length >= 20 || this.pending.some(p => p.request_id === requestId) || this.discarded.has(requestId))
        throw new Error("This decision cannot be saved safely. Nothing was sent.");
      return request;
    }
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
    this.prepare(context, request.target, request.body, request.request_id, request.decision);
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
    if (receipt.status !== "recorded" || request.kind === "permission") {
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
    if ((receipt.status === "delivered" || request.kind === "permission") && body !== undefined
        && this.draft(request) === body) this.draft(request, "");
    if (receipt.status !== "recorded" || request.kind === "permission") this.sentBodies.delete(request.request_id);
    return receipt.status;
  }
}
