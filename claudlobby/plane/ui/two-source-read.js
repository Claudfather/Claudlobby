// Two trusted core-issued read handles. Carrier/session provisioning is external.
import { isOwnerReadHandle } from "/owner-api-client.js";

const slug = /^[a-z][a-z0-9-]{0,31}$/;
const taskID = /^wi_[0-9a-f]{32}$/;
const failed = (state = 'unavailable', remediation = 'This read is unavailable in the two-source view.') =>
  ({ state: state === 'ok' ? 'unavailable' : state, provenance: null, remediation });
const sum = (rows, key) => rows.every(r => Number.isSafeInteger(r[key]) && r[key] >= 0)
  ? rows.reduce((n, r) => n + r[key], 0) : null;

export function createTwoSourceReadTransport({ sources }) {
  if (!Array.isArray(sources) || sources.length !== 2) throw new Error('Exactly two core read handles are required.');
  const rooms = new Map(), streams = new Set();
  let disposed = false;
  const entries = sources.map(({ key, label, reader }) => {
    if (typeof key !== 'string' || !slug.test(key) || key.trim() !== key || typeof label !== 'string' || !label || label.length > 80 ||
        !isOwnerReadHandle(reader)) throw new Error('A named core read handle is required.');
    return { key, label, reader, epoch: 0, readerEpoch: null, host: null, state: 'checking', fleets: [], rosterRequest: 0, routingRevision: 0, unsubscribe: null };
  });
  if (new Set(entries.map(e => e.key)).size !== 2 || entries[0].reader === entries[1].reader)
    throw new Error('Sources must be distinct.');
  const admittedHosts = entries.map(e => e.reader.snapshot().read_profile?.host_uid).filter(Boolean);
  if (new Set(admittedHosts).size !== admittedHosts.length)
    throw new Error('Two independently admitted hosts are required.');
  function notify() { for (const stream of streams) stream.refresh(); }
  function adopt(e, snapshot) {
    const host = snapshot.read_profile?.host_uid;
    let state = snapshot.state === 'ready' ? 'ok' :
      ['denied', 'signed_out'].includes(snapshot.state) ? 'denied' : 'unavailable';
    if (state === 'ok' && (e.host && host !== e.host || entries.some(other => other !== e && other.host === host)))
      state = 'unavailable';
    if (state === 'ok') e.host = host; // Issued by the one authenticated owner admission routine.
    if (e.readerEpoch !== snapshot.epoch || e.state !== state) { e.epoch++; e.readerEpoch = snapshot.epoch; e.state = state; notify(); }
  }
  for (const e of entries) { adopt(e, e.reader.snapshot()); e.unsubscribe = e.reader.subscribe(s => adopt(e, s)); }
  const qualified = (e, value) => value == null ? value : `${e.key}::${value}`;
  const room = (e, value) => value == null ? value : `${e.key} / ${value}`;
  function fields(e, value, ids = [], aliases = [], fleetFields = []) {
    if (!value) return value;
    const out = { ...value };
    for (const k of ids) if (Object.hasOwn(out, k)) out[k] = qualified(e, out[k]);
    for (const k of aliases) if (Object.hasOwn(out, k)) out[k] = qualified(e, out[k]);
    for (const k of fleetFields) if (Object.hasOwn(out, k)) out[k] = room(e, out[k]);
    for (const k of ['short', 'actor_short', 'assignee_short', 'assigned_by_short', 'sender_short', 'recipient_short']) {
      if (typeof out[k] === 'string') out[k] = /^(actor|fleet|host)_[0-9a-f]{32}$/.test(out[k])
        ? qualified(e, out[k]) : `${e.label} / ${out[k]}`;
    }
    return out;
  }
  function event(e, v) { return fields(e, v, ['event_id', 'task_id', 'work_item_id', 'assignment_id', 'actor_uid', 'successor_id'], ['actor_alias']); }
  function assignment(e, v) {
    if (!v) return v;
    return { ...fields(e, v, ['assignment_id', 'task_id', 'assignee_uid', 'assigned_by_uid', 'dispatch_message_id'], ['assignee_alias', 'assigned_by_alias']),
      ...(v.history ? { history: v.history.map(x => event(e, x)) } : {}), terminal_event: event(e, v.terminal_event) };
  }
  function task(e, v) {
    return { ...fields(e, v, ['task_id', 'fleet_uid', 'created_by_uid'], ['created_by_alias', 'attention_by', 'nudged_by'], ['fleet']),
      current_assignment: assignment(e, v.current_assignment),
      ...(v.assignment_history ? { assignment_history: v.assignment_history.map(x => assignment(e, x)) } : {}),
      ...(v.last_event ? { last_event: event(e, v.last_event) } : {}),
      ...(v.assignments ? { assignments: v.assignments.map(x => assignment(e, x)) } : {}),
      ...(v.history ? { history: v.history.map(x => event(e, x)) } : {}), terminal_event: event(e, v.terminal_event),
      ...(v.issues ? { issues: v.issues.map(x => event(e, x)) } : {}),
      ...(v.display_ids ? { display_ids: v.display_ids.map(x => qualified(e, x)) } : {}),
      delivery: fields(e, v.delivery, ['message_id']) };
  }
  function link(e, t) {
    const v = t.task_link;
    const owning = v && e.fleets.find(f => f.alias === v.fleet && f.uid === v.fleet_uid);
    if (!owning || (typeof v.task_id !== 'string' || v.task_id.length !== 35 || !taskID.test(v.task_id)) || v.task_id !== t.work_item_id || v.host_uid !== e.host) return null;
    return { task_id: qualified(e, v.task_id), fleet: room(e, v.fleet), fleet_uid: qualified(e, v.fleet_uid), host_uid: qualified(e, v.host_uid) };
  }
  function thread(e, t) {
    return { ...fields(e, t, ['key', 'work_item_id']), task_link: link(e, t),
      task_events: (t.task_events || []).map(x => event(e, x)),
      messages: t.messages.map(m => ({ ...fields(e, m, ['msg_id', 'work_item_id', 'assignment_id', 'reply_to_msg_id'], ['sender_alias', 'recipient_alias'], ['sender_fleet', 'recipient_fleet']),
        tx: (m.tx || []).map(x => fields(e, x, ['msg_id'])) })) };
  }
  function admitted(e) {
    const snapshot = e.reader.snapshot();
    return !disposed && snapshot.state === 'ready' && e.host &&
      snapshot.read_profile.host_uid === e.host && !entries.some(other => other !== e && other.host === e.host);
  }
  async function read(e, url) {
    if (!admitted(e)) return failed(e.state, e.reader.snapshot().remediation || 'This source is not currently readable. Check its owner session.');
    const epoch = e.epoch;
    let value;
    try { value = await e.reader.jget(url); } catch { value = null; }
    if (epoch !== e.epoch || !admitted(e)) return failed(e.state);
    if (!value || typeof value.state !== 'string' || value.state === 'ok' && (!value.data || typeof value.data !== 'object')) return failed();
    // An endpoint refusal is local. Only authenticated lifecycle/stream facts
    // change source epochs or schedule a coverage refresh.
    return value;
  }
  function publishFleets(e, data, path, ticket) {
    if (ticket !== e.rosterRequest) throw new Error('A newer roster read superseded this snapshot.');
    const rows = data.fleets;
    if (!Array.isArray(rows) || rows.some(f => !f || typeof f.alias !== 'string' || !f.alias || f.alias.length > 240 ||
        typeof f.uid !== 'string' || f.uid.length !== 38 || !/^fleet_[0-9a-f]{32}$/.test(f.uid)) ||
        new Set(rows.map(f => f.alias)).size !== rows.length || new Set(rows.map(f => f.uid)).size !== rows.length ||
        path === '/api/overview' && (!data.host || !data.totals)) throw new Error('Invalid source fleet projection.');
    const next = rows.map(({alias, uid, bots, provisional}) => ({alias, uid, bots, provisional}));
    const identity = rows => JSON.stringify(rows.map(f => [f.alias, f.uid]).sort((a,b) => a[0].localeCompare(b[0])));
    if (identity(next) !== identity(e.fleets)) ++e.routingRevision;
    e.fleets = next;
    rooms.clear();
    for (const entry of entries) for (const f of entry.fleets) rooms.set(room(entry, f.alias), { e: entry, raw: f });
  }
  function fleetChoices(values) {
    return entries.flatMap((e, i) => e.fleets.map(f => ({ ...f, alias: room(e, f.alias),
      uid: qualified(e, f.uid), source_state: values[i].state })));
  }
  function coverage(values) {
    const reachable = values.filter(v => v.state === 'ok').length;
    return { total: 2, reachable, partial: reachable !== 2 };
  }
  function sourceFacts(values, path) {
    return entries.map((e, i) => ({ key: e.key, label: e.label, host_uid: qualified(e, e.host), state: values[i].state,
      provenance: values[i].provenance, remediation: values[i].remediation,
      ...(path === '/api/overview' ? { host: values[i].state === 'ok' ? values[i].data.host : null } : {}),
      ...(path === '/api/summary' ? { summary: values[i].state === 'ok' ? { daemon_serving: values[i].data.daemon_serving ?? null } : null } : {}) }));
  }
  async function jget(input) {
    try {
    if (disposed || typeof input !== 'string' || !input.startsWith('/api/') || input.includes('#')) return failed('invalid');
    const url = new URL(input, 'https://plane.invalid');
    if (url.origin !== 'https://plane.invalid') return failed('invalid');
    const path = url.pathname, detail = /^\/api\/tasks\/([^/]+)$/.exec(path);
    const roster = ['/api/fleets', '/api/channel', '/api/tasks', '/api/identities', '/api/overview', '/api/summary'];
    if (!detail && !roster.includes(path)) return failed();
    const allowed = path === '/api/channel' ? ['fleet', 'limit'] : detail || ['/api/tasks', '/api/identities'].includes(path) ? ['fleet'] : [];
    if ([...url.searchParams.keys()].some(k => !allowed.includes(k) || url.searchParams.getAll(k).length !== 1)) return failed('invalid');
    if (path === '/api/channel' && url.searchParams.has('limit') && url.searchParams.get('limit') !== '120') return failed('invalid');
    const selected = url.searchParams.get('fleet');
    const owning = selected && rooms.get(selected);
    if (selected && !owning) return failed('unknown', 'Select an admitted source team.');
    if (detail) {
      let id;
      try { id = decodeURIComponent(detail[1]); } catch { return failed('invalid'); }
      if (!owning || id !== `${owning.e.key}::${id.split('::')[1]}` || (id.split('::')[1]?.length !== 35 || !taskID.test(id.split('::')[1]))) return failed('invalid');
      const raw = id.split('::')[1], revision = owning.e.routingRevision, result = await read(owning.e, `/api/tasks/${raw}?fleet=${encodeURIComponent(owning.raw.alias)}`);
      if (result.state !== 'ok') return result;
      if (revision !== owning.e.routingRevision) return failed('unavailable', 'The source team mapping changed; refresh this task.');
      const t = result.data.task;
      if (!t || t.task_id !== raw || t.fleet !== owning.raw.alias || t.fleet_uid !== owning.raw.uid) return failed('denied', 'Task ownership did not match the selected source team.');
      return { ...result, data: { ...result.data, task: task(owning.e, t) } };
    }
    const chosen = owning ? [owning.e] : entries;
    const suffix = path === '/api/channel' ? '?limit=120' : '';
    const epochs = chosen.map(e => e.epoch), revisions = chosen.map(e => e.routingRevision);
    const rosterRead = path === '/api/fleets' || path === '/api/overview';
    const tickets = chosen.map(e => rosterRead ? ++e.rosterRequest : null);
    const values = await Promise.all(chosen.map(e => read(e, path + suffix + (owning ? `${suffix ? '&' : '?'}fleet=${encodeURIComponent(owning.raw.alias)}` : ''))));
    // One host may finish before its loss while the other host is still reading.
    // Recheck admission at combination, not only when each request completed.
    chosen.forEach((e, i) => {
      if (values[i].state === 'ok' && (epochs[i] !== e.epoch || !admitted(e) || owning && revisions[i] !== e.routingRevision))
        values[i] = failed(e.state);
    });
    if (owning) {
      const v = values[0];
      if (v.state !== 'ok') return v;
      return project(owning.e, path, v);
    }
    const good = entries.flatMap((e, i) => {
      if (values[i].state !== 'ok') return [];
      try {
        const projected = project(e, path, values[i]);
        if (rosterRead) publishFleets(e, values[i].data, path, tickets[i]);
        return [{ e, v: projected }];
      } catch {
        values[i] = failed('unavailable', 'This source returned an unsupported or superseded read projection.');
        return [];
      }
    });
    const data = { sources: sourceFacts(values, path), coverage: coverage(values),
      windows: entries.map((e, i) => ({ source: e.key, state: values[i].state, limit: values[i].data?.limit, truncated: values[i].data?.truncated, lineage: values[i].data?.lineage })) };
    if (rosterRead) Object.assign(data, { fleet_choices: fleetChoices(values), default: null });
    if (path === '/api/fleets') data.fleets = data.fleet_choices;
    if (path === '/api/channel') Object.assign(data, { threads: good.flatMap(x => x.v.data.threads), lineage: { unresolved_threads: sum(good.map(x => ({ unresolved_threads: x.v.data.lineage?.unresolved_threads || 0 })), 'unresolved_threads') } });
    if (path === '/api/identities') data.identities = good.flatMap(x => x.v.data.identities);
    if (path === '/api/tasks') Object.assign(data, { tasks: good.flatMap(x => x.v.data.tasks), issues: good.flatMap(x => x.v.data.issues || []), issue_count: sum(good.map(x => x.v.data), 'issue_count'), truncated: good.some(x => x.v.data.truncated), issues_truncated: good.some(x => x.v.data.issues_truncated), limit: 200, attention_count: sum(good.map(x => x.v.data), 'attention_count') });
    if (path === '/api/overview') {
      data.fleets = good.flatMap(x => x.v.data.fleets);
      const totals = good.map(x => x.v.data.totals);
      data.totals = Object.fromEntries(['fleets','bots','provisional','working','attention','overdue'].map(k => [k, sum(totals, k)]));
      data.totals.live_poll = totals.some(t => t.live_poll !== 'ok') ? 'unavailable' : 'ok';
      data.totals.recorder_gaps = totals.flatMap(t => t.recorder_gaps || []);
    }
    // Partial ok is deliberate and disclosed. With no readable source, do not show an empty board.
    return { state: good.length || path === '/api/fleets' ? 'ok' : 'unavailable', provenance: null, data,
      ...(!good.length ? { remediation: 'Neither source is currently readable.' } : {}) };
  }
    catch { return failed('unavailable', 'This source returned an unsupported read projection.'); }
  }
  function project(e, path, v) {
    const d = v.data;
    if (path === '/api/channel') return { ...v, data: { ...d, threads: d.threads.map(t => thread(e, t)) } };
    if (path === '/api/tasks') return { ...v, data: { ...d, tasks: d.tasks.map(t => task(e, t)), issues: (d.issues || []).map(x => event(e, x)) } };
    if (path === '/api/identities') return { ...v, data: { ...d, identities: d.identities.map(x => fields(e, x, ['uid'], ['alias'], ['fleet'])) } };
    if (path === '/api/overview' && (!d.totals || !d.host)) throw new Error('Missing source facts.');
    if (path === '/api/overview') return { ...v, data: { ...d, fleets: d.fleets.map(x => fields(e, x, ['uid'], ['acked_by'], ['alias'])) } };
    return v;
  }
  function createEventSource(url) {
    if (url !== '/api/stream') throw new Error('Only the canonical read stream is supported.');
    const listeners = new Map(), children = [];
    const facade = { onmessage: null, onopen: null, onerror: null,
      addEventListener(name, cb) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(cb); },
      removeEventListener(name, cb) { listeners.get(name)?.delete(cb); },
      refresh() { facade.onmessage?.({ data: JSON.stringify({ rows: [], coverage_changed: true }) }); },
      close() { children.forEach(s => s.close()); streams.delete(facade); } };
    for (const e of entries) {
      const child = e.reader.createEventSource(url); children.push(child);
      child.onmessage = ev => {
        if (!admitted(e)) return;
        try { const payload = JSON.parse(ev.data); facade.onmessage?.({ data: JSON.stringify({ ...payload, source: e.key, rows: payload.rows.map(r => ({ ...r, source: e.key })) }) }); } catch { /* next read refresh corrects malformed push */ }
      };
      child.onopen = ev => { facade.onopen?.(ev); facade.refresh(); };
      child.onerror = ev => facade.onerror?.(ev);
      child.addEventListener('source', ev => { try { const state = JSON.parse(ev.data).state; if (state !== e.state) { e.state = state; e.epoch++; facade.refresh(); } } catch { /* do not infer recovery */ } });
    }
    streams.add(facade); return facade;
  }
  function dispose() { disposed = true; for (const s of [...streams]) s.close(); for (const e of entries) e.unsubscribe(); }
  return Object.freeze({ jget, createEventSource, dispose });
}
