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
export function connectionLabel(state) {
  return ({ready:"Connected", not_connected:"Not connected", denied:"Access denied", disconnected:"Disconnected",
    signed_out:"Disconnected", checking:"Checking connection", unavailable:"Connection unavailable",
    renewing:"Keeping connected", logout:"Disconnecting"})[state] || "Connection unconfirmed";
}
