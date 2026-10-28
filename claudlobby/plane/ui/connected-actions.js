import { scopeKey, validContext, validTaskActionContext, validTaskActionTarget, samePreparation, sameReceipt, validPermissionContext, validPermissionTarget, samePermissionReceipt } from "./action-state.js";

const unknown = () => new Error('Action outcome unknown. Check the original receipt.');
const uid = (value, prefix) => typeof value === 'string' && value.length === prefix.length + 33
  && new RegExp(`^${prefix}_[0-9a-f]{32}$`).test(value);
// Called only by the carrier with its private canonical action interface.
// Qualification is presentation identity, never an action grant or credential.
export function createSourceActionInterface({key, owner, snapshot, stamp, onPause = () => {}}) {
  if (typeof key !== 'string' || !/^[a-z][a-z0-9-]{0,31}$/.test(key) || key.trim() !== key) throw unknown();
  const contexts = new Map(), contextRequests = new Map();
  const qualify = value => `${key}::${value}`;
  function bare(value, prefix) {
    if (typeof value !== 'string' || !value.startsWith(key + '::')) throw unknown();
    const raw = value.slice(key.length + 2);
    if (!uid(raw,prefix)) throw unknown();
    return raw;
  }
  const room = value => key + ' / ' + value;
  function scope(value) { return {...value,host:qualify(value.host),fleet:room(value.fleet)}; }
  function permission(value) {
    if (!value) return value;
    const result = {...value, permission_id:qualify(value.permission_id), task_id:qualify(value.task_id)};
    if (value.state === 'unavailable') return result;
    return {...result, request_event_id:qualify(value.request_event_id),
      assignment_id:value.assignment_id === null ? null : qualify(value.assignment_id),
      requester:{...value.requester,uid:qualify(value.requester.uid)},
      decided_by:value.decided_by === null ? null : {...value.decided_by,uid:qualify(value.decided_by.uid)}};
  }
  function target(value) {
    if (value.permission_id) return {...value, permission_id:qualify(value.permission_id), task_id:qualify(value.task_id)};
    return {...value,recipient:qualify(value.recipient),task_id:value.task_id === null ? null : qualify(value.task_id),
      ...(Object.hasOwn(value,'assignment_id') ? {assignment_id:value.assignment_id === null ? null : qualify(value.assignment_id)} : {})};
  }
  async function context(rawRoom, kind) {
    if (snapshot().state !== 'ready' || typeof rawRoom !== 'string' || !rawRoom || rawRoom === 'all') return null;
    const before = stamp(), ticket = {};
    contextRequests.set(kind,ticket);
    const read = kind === 'message' ? owner.interactionContext : kind === 'nudge' ? owner.nudgeContext : owner.feedbackContext;
    let value;
    try { value = await read(rawRoom); }
    catch (error) {
      if (contextRequests.get(kind) !== ticket || before !== stamp() || snapshot().state !== 'ready') return null;
      throw error;
    }
    if (contextRequests.get(kind) !== ticket || before !== stamp() || snapshot().state !== 'ready' || value?.scope?.host !== snapshot().read_profile?.host_uid
        || value.room !== rawRoom || value.scope.fleet !== rawRoom || value.simulation !== false
        || !(kind === 'message' ? validContext(value) && value.actions.length === 1 && value.actions[0] === kind
          : validTaskActionContext(value,kind)) || !value.recipients.every(r => uid(r.id,'actor'))) return null;
    const raw = JSON.parse(JSON.stringify(value));
    const ui = Object.freeze({...raw,room:room(rawRoom),scope:Object.freeze(scope(raw.scope)),
      recipients:Object.freeze(raw.recipients.map(r => Object.freeze({...r,id:qualify(r.id)})))});
    contexts.set(kind,{raw,ui}); return ui;
  }
  async function permissionContext(rawRoom, selectedTask) {
    if (snapshot().state !== 'ready') return null;
    const task = bare(selectedTask,'wi'), before = stamp(), ticket = {};
    contextRequests.set('permission',ticket);
    let value;
    try { value = await owner.permissionContext(rawRoom,task); }
    catch (error) {
      if (contextRequests.get('permission') !== ticket || before !== stamp() || snapshot().state !== 'ready') return null;
      throw error;
    }
    if (contextRequests.get('permission') !== ticket || before !== stamp() || snapshot().state !== 'ready'
        || !validPermissionContext(value) || value.scope.host !== snapshot().read_profile?.host_uid
        || value.room !== rawRoom || value.task_id !== task) return null;
    const raw = JSON.parse(JSON.stringify(value));
    const ui = Object.freeze({...raw,room:room(rawRoom),scope:Object.freeze(scope(raw.scope)),
      task_id:qualify(raw.task_id),permissions:raw.permissions.map(permission)});
    contexts.set('permission',{raw,ui}); return ui;
  }
  function request(value, receipt) {
    const capability = contexts.get(value?.kind);
    if (!capability || snapshot().state !== 'ready' || scopeKey(value.scope) !== scopeKey(capability.ui.scope)) throw unknown();
    if (value.kind === 'permission') {
      if (!validPermissionTarget(value.target)) throw unknown();
      const rawTarget = {...value.target,permission_id:bare(value.target.permission_id,'perm'),task_id:bare(value.target.task_id,'wi')};
      if (rawTarget.task_id !== capability.raw.task_id || !receipt && (rawTarget.release_id !== capability.raw.release_id
          || !capability.raw.permissions.some(p => p.permission_id === rawTarget.permission_id && p.scope_sha256 === rawTarget.scope_sha256))) throw unknown();
      return {...value,scope:{...capability.raw.scope},target:rawTarget};
    }
    const v2 = value.version === 2;
    if (v2 ? !validTaskActionTarget(value.target) : value.version !== undefined || value.kind !== 'message' || value.target?.task_id !== null) throw unknown();
    const rawTarget = {...value.target,recipient:bare(value.target.recipient,'actor'),
      task_id:value.target.task_id === null ? null : bare(value.target.task_id,'wi'),
      ...(v2 ? {assignment_id:value.target.assignment_id === null ? null : bare(value.target.assignment_id,'asg')} : {})};
    if (!capability.raw.recipients.some(r => r.id === rawTarget.recipient)
        || v2 && !receipt && rawTarget.release_id !== capability.raw.release_id) throw unknown();
    return {...value,scope:{...capability.raw.scope},target:rawTarget};
  }
  async function operation(name, value) {
    const preparing = name === 'prepareAction', receipt = name === 'actionReceipt';
    const raw = request(value,receipt), before = stamp();
    const result = await owner[name](raw);
    if (before !== stamp() || snapshot().state !== 'ready') throw unknown();
    if (preparing ? !samePreparation(raw,result) : raw.kind === 'permission' ? !samePermissionReceipt(raw,result,true)
      : !sameReceipt(raw,{...result,status:result?.status === 'unknown' ? 'recorded' : result?.status})) throw unknown();
    return {...result,scope:scope(result.scope),target:target(result.target),
      ...(raw.kind === 'permission' && !preparing ? {permission:permission(result.permission)} : {})};
  }
  return Object.freeze({
    interactionContext:fleet => context(fleet,'message'),nudgeContext:fleet => context(fleet,'nudge'),feedbackContext:fleet => context(fleet,'feedback'),
    permissionContext,
    prepareAction:value => operation('prepareAction',value),sendAction:value => operation('sendAction',value),actionReceipt:value => operation('actionReceipt',value),
    invalidate() { contexts.clear(); contextRequests.clear(); },
    pause(rawScope,kind,recipient) {
      const current = contexts.get(kind);
      if (!current || scopeKey(rawScope) !== scopeKey(current.raw.scope)
          || recipient && !current.raw.recipients.some(r => r.id === recipient)) return;
      contexts.delete(kind); contextRequests.delete(kind); onPause(current.ui.scope,kind,recipient ? qualify(recipient) : undefined);
    },
  });
}
