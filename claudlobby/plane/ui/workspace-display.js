// Presentation preferences never change source routing or write authority.
const HIDDEN_TEAMS = "plane.hidden-teams.v1";
export function hiddenTeams(storage) {
  try {
    const value = JSON.parse((storage || globalThis.localStorage).getItem(HIDDEN_TEAMS));
    return new Set(Array.isArray(value) ? value.filter(v => typeof v === "string" && v.trim() && v.length <= 320).slice(0, 300) : []);
  } catch { return new Set(); }
}
export function setTeamHidden(uid, hidden, storage) {
  const next = hiddenTeams(storage);
  if (typeof uid !== "string" || !uid.trim() || uid.length > 320) return next;
  if (hidden) next.add(uid); else next.delete(uid);
  try { (storage || globalThis.localStorage).setItem(HIDDEN_TEAMS, JSON.stringify([...next])); } catch { /* local convenience only */ }
  return next;
}
export function fleetStatusCounts(fleet) {
  const recorded = Number.isSafeInteger(fleet.bots) && fleet.bots >= 0 ? fleet.bots : null;
  const counts = fleet.presence?.counts;
  const states = ["working", "idle", "down", "stale", "unknown", "sampling"];
  if (recorded === null || fleet.presence?.live_poll !== "ok" || !counts
      || !states.every(k => Number.isSafeInteger(counts[k]) && counts[k] >= 0)
      || states.reduce((n, k) => n + counts[k], 0) !== recorded)
    return { recorded, running: null, stopped: null, unconfirmed: recorded };
  return { recorded, running: counts.working + counts.idle, stopped: counts.down,
    unconfirmed: counts.stale + counts.unknown + counts.sampling };
}
export function fleetStatusCopy(fleet) {
  const c = fleetStatusCounts(fleet);
  return `${c.running ?? "unknown"} running · ${c.stopped ?? "unknown"} not running · ${c.unconfirmed ?? "unknown"} unconfirmed`;
}
export function aggregateFleetStatusCounts(fleets, recorded) {
  const unknown = {recorded, running:null, stopped:null, unconfirmed:recorded};
  if (!Number.isSafeInteger(recorded) || recorded < 0)
    return {recorded:null, running:null, stopped:null, unconfirmed:null};
  const rows = fleets.map(fleetStatusCounts);
  const represented = rows.reduce((n, row) => n + (row.recorded ?? 0), 0);
  if (!Number.isSafeInteger(represented) || represented > recorded) return unknown;
  const running = rows.reduce((n, row) => n + (row.running ?? 0), 0);
  const stopped = rows.reduce((n, row) => n + (row.stopped ?? 0), 0);
  if (!Number.isSafeInteger(running + stopped) || running + stopped > recorded) return unknown;
  // Invalid/missing fleet samples contribute no confirmed state. The host's
  // authoritative recorded total still accounts for every remaining identity.
  return {recorded, running, stopped, unconfirmed:recorded - running - stopped};
}
export function aggregateFleetStatusCopy(fleets, recorded) {
  const c = aggregateFleetStatusCounts(fleets, recorded);
  const qualifier = c.unconfirmed > 0 ? "confirmed " : "";
  return `${c.running ?? "unknown"} ${qualifier}running · ${c.stopped ?? "unknown"} ${qualifier}not running · ${c.unconfirmed ?? "unknown"} unconfirmed`;
}
export function connectionLabel(state) {
  return ({ready:"Connected", not_connected:"Not connected", denied:"Access denied", disconnected:"Disconnected",
    signed_out:"Disconnected", checking:"Checking connection", unavailable:"Connection unavailable",
    renewing:"Keeping connected", logout:"Disconnecting"})[state] || "Connection unconfirmed";
}
