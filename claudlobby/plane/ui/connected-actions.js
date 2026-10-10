import { scopeKey, validContext, validTaskActionContext, validTaskActionTarget, samePreparation, sameReceipt } from "./action-state.js";

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
  function target(value) {
    return {...value,recipient:qualify(value.recipient),task_id:value.task_id === null ? null : qualify(value.task_id),
      ...(Object.hasOwn(value,'assignment_id') ? {assignment_id:value.assignment_id === null ? null : qualify(value.assignment_id)} : {})};
  }
  async function context(rawRoom, kind) {
    if (snapshot().state !== 'ready' || typeof rawRoom !== 'string' || !rawRoom || rawRoom === 'all') return null;
    const before = stamp(), ticket = {};
    contextRequests.set(kind,ticket);
    const read = kind === 'message' ? owner.interactionContext : kind === 'nudge' ? owner.nudgeContext : owner.feedbackContext;
    const value = await read(rawRoom);
    if (contextRequests.get(kind) !== ticket || before !== stamp() || snapshot().state !== 'ready' || value?.scope?.host !== snapshot().read_profile?.host_uid
        || value.room !== rawRoom || value.scope.fleet !== rawRoom || value.simulation !== false
        || !(kind === 'message' ? validContext(value) && value.actions.length === 1 && value.actions[0] === kind
          : validTaskActionContext(value,kind)) || !value.recipients.every(r => uid(r.id,'actor'))) return null;
    const raw = JSON.parse(JSON.stringify(value));
    const ui = Object.freeze({...raw,room:room(rawRoom),scope:Object.freeze(scope(raw.scope)),
      recipients:Object.freeze(raw.recipients.map(r => Object.freeze({...r,id:qualify(r.id)})))});
    contexts.set(kind,{raw,ui}); return ui;
  }
  function request(value, receipt) {
    const capability = contexts.get(value?.kind);
    if (!capability || snapshot().state !== 'ready' || scopeKey(value.scope) !== scopeKey(capability.ui.scope)) throw unknown();
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
    if (preparing ? !samePreparation(raw,result) : !sameReceipt(raw,{...result,status:result?.status === 'unknown' ? 'recorded' : result?.status})) throw unknown();
    return {...result,scope:scope(result.scope),target:target(result.target)};
  }
  return Object.freeze({
    interactionContext:fleet => context(fleet,'message'),nudgeContext:fleet => context(fleet,'nudge'),feedbackContext:fleet => context(fleet,'feedback'),
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
