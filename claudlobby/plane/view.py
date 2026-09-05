"""The Phase-4 operator plane — `claudlobby plane view` (design walk 2026-08-28).

The v1 UI's API daemon: read-only over the plane db, serving the static UI and
the GET surfaces. Rulings it implements (the Phase-4 walk + its gauntlet):

* **Strictly read-only, structurally**: every connection opens `mode=ro` with
  `PRAGMA query_only`; the app exposes no non-GET route (pinned). The db path
  is a PURE join — never `db.db_path()`, whose mkdir side effect made the
  read-only daemon write `state/plane/` on a cold root and 500 on an
  unwritable one (gauntlet round, probed).
* **Panel states are SERVER-side facts** (§16: never render zero when the
  source is absent): every endpoint returns ``{state, provenance,
  remediation?, data?}``. State tokens and the directory-at-path
  classification come from `source_state` — the decided-once module — so a
  directory where the db belongs reads ABSENT, never a chmod misdirection
  (the first version re-decided this and got it backwards; pinned now).
* **Story-first channel**: /api/channel returns CONVERSATION THREADS — a
  dispatch, its delivery states, its reports, and its task closure grouped by
  work item OR reply chain, with the chain PROMOTED to any member's work item
  (a reply that omits the id must not split the story — gauntlet, probed both
  directions). The server also stamps the SEMANTIC facts (`delivered`,
  `terminal`, per-tx `activated`) from queries.py's one-definition constants,
  so no client re-derives vocabulary — the first version's JS copy already
  disagreed live (last-terminal vs the reducer's first-terminal).
* **SSE**: /api/stream pushes ledger rows past a cursor. A connect WITHOUT a
  cursor starts at HEAD (the stream is a refresh trigger, not the source of
  truth — cursor-less connects used to replay the entire ledger, measured at
  108s per viewer per reconnect at estate scale), and reconnects resume from
  the `Last-Event-ID` the browser sends. One pump per connection at operator
  scale; a shared fan-out pump is a scale-time upgrade, not claimed here.

FastAPI/uvicorn live in the OPTIONAL `[plane-ui]` extra (§14 degrade rule) —
this module import-guards them so the core ledger never needs them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

try:  # §14: optional UI features degrade without disabling the core ledger
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as _exc:  # pragma: no cover - exercised via CLI refusal
    FastAPI = None  # type: ignore[assignment]
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None

from ..source_state import (
    SOURCE_ABSENT,
    SOURCE_OK,
    SOURCE_UNREADABLE,
    probe_dir,
    probe_source,
)
from .daemon import probe_daemon, socket_path
from .emit_api import CaptureConfigError, capture_mode, load_capture_config
from .ingest import CONSTRUCT_TABLES
from .spool import oldest_spooled_at, scan_spool
from .ingest import now_iso as _now_iso
from .inventory import fleet_of, qualified, qualified_labels
from .presence import STALE_AFTER_S, _parse_iso, derive_presence, presence_counts
from .sampler import PaneSampler, discover_bot_dirs
from .queries import (
    ACTIVATION_TX_EVENTS,
    ATTENTION_ARMS,
    ATTENTION_ARMS_SQL,
    LATEST_HEARTBEAT_SQL,
    NON_TERMINAL_CLAUSE,
    TASK_STATUS_SQL,
    TERMINAL_TASK_EVENTS,
    OPEN_ASSIGNMENTS_AT_SQL, fleet_alias_range, fleet_range_params,
    not_sentinel_sql,
)

UI_DIR = Path(__file__).resolve().parent / "ui"
_CHANNEL_LIMIT_MAX = 500

# "[BOTCOMMAND] erlich | task | <words> | repo:x | task:t-..." -> "<words>":
# the leading framing AND the trailing ` | key:value` envelope fields are
# carrier structure, not prose (gauntlet: the client-side strip leaked the
# tail on the two most common real message shapes). The verbatim body stays
# in the payload as evidence; `body_words` is the derived render form.
_FRAME_RE = re.compile(r"^\[BOT(?:COMMAND|REPORT)\]\s*[^|\n]*\|\s*[^|\n]*\|\s*")
_TAIL_RE = re.compile(r"(?:\s*\|\s*[A-Za-z_][A-Za-z0-9_-]*:[^|\n]*)+\s*$")


def body_words(text: str | None) -> str | None:
    if not text:
        return text
    out = _FRAME_RE.sub("", text)
    out = _TAIL_RE.sub("", out)
    return out.strip() or text


def _short(alias: str | None) -> str | None:
    """Alias-first presentation (§11): `bot:fleet/name` -> `name` and
    `human:chris` -> `chris` (#1402 — the operator renders by name, same
    grammar as every bot). The full alias stays in the payload."""
    if not alias:
        return alias
    if "/" in alias:
        return alias.rsplit("/", 1)[-1]
    if alias.startswith("human:"):
        return alias[len("human:"):] or alias
    return alias


def _qualified(alias: str | None) -> str | None:
    """A bot reads `fleet/name` (inventory.qualified — the ONE spelling a
    twin, a cross-fleet name and an all-fleets card share); anything
    without a fleet keeps the view's own short form (`human:chris` ->
    `chris`, which inventory's short rule does not strip)."""
    return qualified(alias) if alias and fleet_of(alias) else _short(alias)


class UnknownFleet(Exception):
    """`?fleet=` named a fleet this plane holds no identity for while it
    holds others. The plane's own rule (plane-lookup: a fleet with no
    identity REFUSES — a wrong name is not "nothing recorded"), applied to
    the view: a typed `unknown` state naming the fleets held, never a
    healthy empty room. The grid and presence routes also know a fleet
    from the sampler's panes (a fleet on disk before its first row)."""


def _fleet_scope(conn: sqlite3.Connection, fleet: str | None) -> str | None:
    """Normalize a route's `fleet` axis ONCE for every per-fleet route: no
    fleet, an empty string or `all` is the host-wide read (None); a named
    fleet must exist on the plane or the route answers `unknown`."""
    if not fleet or fleet == "all":
        return None
    if conn.execute("SELECT 1 FROM identity_registry WHERE kind = 'fleet'"
                    " AND alias = ?", (fleet,)).fetchone() is None:
        held = [r[1] for r in conn.execute(_FLEET_ROWS_SQL)]
        if held:
            raise UnknownFleet(
                f"no fleet '{fleet}' on this plane — it holds: " + ", ".join(held))
        # a plane holding NO fleet yet is not a wrong name: the routes'
        # own idle states carry the remedy (run `generate`)
    return fleet


_FLEET_ROWS_SQL = (
    "SELECT uid, alias, first_seen, last_seen FROM identity_registry"
    f" WHERE kind = 'fleet' AND {not_sentinel_sql()}"
    " ORDER BY alias")


def _fleet_actors(conn: sqlite3.Connection) -> dict[str, list]:
    """fleet -> its actor rows (uid, alias, provisional): ONE registry scan
    bucketed by inventory.fleet_of, the Python spelling of the fleet axis.
    The fleets door and the overview both ask "which actors belong to
    fleet X" through here, never through a per-fleet LIKE."""
    by_fleet: dict[str, list] = {}
    for r in conn.execute("SELECT uid, alias, provisional FROM identity_registry"
                          " WHERE kind = 'actor'"):
        f = fleet_of(r["alias"])
        if f is not None:
            by_fleet.setdefault(f, []).append(r)
    return by_fleet


def _fetch_fleets(conn: sqlite3.Connection, actors: dict | None = None) -> dict:
    """The host's fleets, from the registry's fleet identities — NEVER
    from the roster rail's last-seen window (U1): that read is LIMIT 200
    newest-first over every participant, so on a host whose bots and
    humans out-chatter a quiet fleet the fleet drops out of the window and
    its tab with it. `_`-prefixed scope sentinels (the `_host` fleet the
    host probe emits under) are not fleets. `bots` is every actor of the
    fleet the plane knows, and `provisional` how many of those are still
    unconfirmed by a registry scan: a mistyped dispatch target mints a
    provisional actor (tombstoned by the next scan), and the count says
    "10 bots, 1 unconfirmed" rather than absorbing the mint into a tenth
    bot — while a fleet that never keyframed (no scan yet) still shows the
    bots it evidently has, all of them unconfirmed. `default` is the fleet whose ROOM moved
    most recently (a communication sent BY the fleet or TO it — the room
    axis, deliberately not the identity's `last_seen`, which every emission
    under the fleet advances, heartbeats included, so a silent fleet with a
    chatty keepalive would win the tab); ties and silence broken
    alphabetically; it is the tab a first visit opens when the viewer has
    never picked one."""
    actors = actors if actors is not None else _fleet_actors(conn)
    fleets = []
    for uid, alias, first_seen, last_seen in conn.execute(_FLEET_ROWS_SQL):
        rows = actors.get(alias, [])
        arms = conn.execute(
            "SELECT occurred_at FROM communications WHERE fleet_uid = ?"
            " ORDER BY ingest_seq DESC LIMIT 1", (uid,)).fetchall()
        arms += conn.execute(
            "SELECT occurred_at FROM communications WHERE recipient_fleet = ?"
            " ORDER BY ingest_seq DESC LIMIT 1", (alias,)).fetchall()
        fleets.append({
            "alias": alias, "uid": uid, "first_seen": first_seen,
            "last_seen": last_seen,
            "bots": len(rows),
            "provisional": sum(1 for r in rows if r["provisional"]),
            "last_comm_at": max((r[0] for r in arms if r[0]), default=None)})
    # reverse=True keeps the alphabetical order among equal instants
    ranked = sorted(fleets, key=lambda f: f["last_comm_at"] or "", reverse=True)
    return {"fleets": fleets,
            "default": ranked[0]["alias"] if ranked else None}


def _plane_state_dir(root: Path) -> Path:
    return Path(root) / "state" / "plane"


def _db_file(root: Path) -> Path:
    return _plane_state_dir(root) / "plane.db"


def _ro_conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _provenance(root: Path, conn: sqlite3.Connection | None) -> dict:
    prov = {"db": str(_db_file(root)), "checked_at": _now_iso()}
    if conn is not None:
        # ORDER BY … LIMIT 1, never MAX(a),MAX(b): the paired aggregates
        # defeat SQLite's min/max optimization into a full ledger scan — on
        # EVERY envelope and every SSE tick (gauntlet, measured ~1000x).
        row = conn.execute(
            "SELECT ingest_seq, ingested_at FROM ingest_ledger"
            " ORDER BY ingest_seq DESC LIMIT 1"
        ).fetchone()
        prov["last_ingest_seq"] = row["ingest_seq"] if row else None
        prov["last_ingest_at"] = row["ingested_at"] if row else None
    return prov


def _envelope(root: Path, fn):
    """Run `fn(conn)` -> data under the panel-state contract. Classification
    of the pre-connect shape comes from source_state.probe_source — the
    decided-once rule — then sqlite/OS errors classify UNREADABLE. EMPTY
    data is ok-with-empty: legitimately-idle is the UI's word, never the
    server inventing zeros (§16)."""
    db = _db_file(root)

    def fail(state: str, remediation: str) -> dict:
        return {"state": state,
                "provenance": {"db": str(db), "checked_at": _now_iso()},
                "remediation": remediation}

    probe = probe_source(db)
    if probe.state == SOURCE_ABSENT:
        return fail(SOURCE_ABSENT,
                    "no plane db yet — it appears on the first armed emission"
                    " (or `claudlobby emit`); check PLANE_EMIT_ENABLED for"
                    " the fleet")
    if probe.state == SOURCE_UNREADABLE:
        return fail(SOURCE_UNREADABLE,
                    "db exists but cannot be opened — check permissions;"
                    " `claudlobby plane doctor`")
    try:
        conn = _ro_conn(db)
    except sqlite3.Error as exc:
        return fail(SOURCE_UNREADABLE,
                    f"db cannot be opened: {exc} — `claudlobby plane doctor`")
    try:
        data = fn(conn)
        return {"state": SOURCE_OK, "provenance": _provenance(root, conn),
                "data": data}
    except UnknownFleet as exc:
        return {"state": "unknown", "provenance": _provenance(root, conn),
                "remediation": str(exc)}
    except (sqlite3.Error, OSError) as exc:
        return fail(SOURCE_UNREADABLE,
                    f"query failed: {exc} — schema drift? run"
                    " `claudlobby plane doctor`")
    finally:
        conn.close()


_channel_names_cache: tuple[float, dict] | None = None


def _channel_names(root: Path) -> dict:
    """Operator map state/plane/channels.json: raw carrier address -> human
    name. mtime-cached (was re-read+parsed per request); a malformed file is
    DISCLOSED via a `_malformed` marker key, never silently an empty map —
    absent and broken have different remedies (gauntlet, structural lens).
    Follow-up (filed): compose the known half from fleet.yaml bindings."""
    global _channel_names_cache
    p = _plane_state_dir(root) / "channels.json"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if _channel_names_cache and _channel_names_cache[0] == mtime:
        return _channel_names_cache[1]
    try:
        loaded = json.loads(p.read_text())
        names = loaded if isinstance(loaded, dict) else {"_malformed": True}
    except json.JSONDecodeError:
        names = {"_malformed": True}
    except OSError:
        return {}
    _channel_names_cache = (mtime, names)
    return names


# --------------------------------------------------------------------------
# Surface queries (all read-only; semantics from queries.py, never re-derived)
# --------------------------------------------------------------------------

_TERMINAL_SET = frozenset(TERMINAL_TASK_EVENTS)
_ACTIVATION_SET = frozenset(ACTIVATION_TX_EVENTS)


def _fetch_channel(conn: sqlite3.Connection, names: dict, limit: int,
                   fleet: str | None = None) -> dict:
    cols = ("ingest_seq, msg_id, occurred_at, sender_alias,"
            " recipient_alias, recipient_raw, message_class, command_type,"
            " privacy, body, body_bytes, truncated, work_item_id,"
            " assignment_id, reply_to_msg_id, emitter, fleet_uid,"
            " recipient_fleet")
    fleet = _fleet_scope(conn, fleet)
    if fleet:
        # Per-team channels are the DEFAULT view (operator ruling 2026-08-29:
        # rooms, not a firehose). A room shows every message that TOUCHES the
        # fleet — sender OR recipient — so a cross-fleet thread stays whole in
        # both rooms (44.6% of this estate's dispatch traffic is cross-fleet;
        # a sender-only predicate halved every such conversation — gauntlet).
        # A UNION of two EQUALITY arms, never `OR recipient_alias LIKE`: the
        # OR forced a full reverse scan (0003 shipped inert against its own
        # query — three reviewers EXPLAIN'd it independently), and LIKE let a
        # fleet named `en_` absorb `eng`'s room. Each arm SEARCHes its index
        # (0003 sender / 0004 recipient_fleet) and early-exits on LIMIT;
        # UNION dedupes the same-fleet-both-arms overlap. Pinned by an
        # EXPLAIN-plan test so the scan cannot silently return.
        comms = [dict(r) for r in conn.execute(
            f"SELECT * FROM (SELECT {cols} FROM communications"
            "  WHERE fleet_uid = (SELECT uid FROM identity_registry"
            "   WHERE kind='fleet' AND alias = ?)"
            "  ORDER BY ingest_seq DESC LIMIT ?)"
            " UNION "
            f"SELECT * FROM (SELECT {cols} FROM communications"
            "  WHERE recipient_fleet = ?"
            "  ORDER BY ingest_seq DESC LIMIT ?)"
            " ORDER BY ingest_seq DESC LIMIT ?",
            (fleet, limit, fleet, limit, limit)).fetchall()]
    else:
        comms = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM communications"
            " ORDER BY ingest_seq DESC LIMIT ?", (limit,)).fetchall()]
    if not comms:
        return {"threads": []}
    msg_ids = [c["msg_id"] for c in comms]
    ph = ",".join("?" * len(msg_ids))
    tx_rows = [dict(r) for r in conn.execute(
        f"SELECT msg_id, event, carrier, attempt_no, occurred_at, ingest_seq"
        f" FROM events WHERE kind='transmission' AND msg_id IN ({ph})"
        f" ORDER BY ingest_seq", msg_ids).fetchall()]
    tx_by_msg: dict = {}
    for t in tx_rows:
        t["activated"] = t["event"] in _ACTIVATION_SET
        tx_by_msg.setdefault(t["msg_id"], []).append(t)

    # Identity keeps its fleet where it matters (U2): each party's fleet is
    # read off ITS OWN alias (inventory.fleet_of, the one spelling) — never
    # off `fleet_uid`, which is the fleet the row was EMITTED under: a
    # human writing from the data bridge to an eng bot would acquire a
    # fleet, and an eng bot emitting under data would pass as data's own
    # (adversarial lens, both reproduced). A message whose two fleets differ
    # is CROSS-FLEET, and both of its names render fleet-qualified in EVERY
    # room (`eng/erlich -> data/samir`), because inside the data room a bare
    # `erlich` reads as one of data's own. Intra-fleet names stay short in
    # their room; the host-wide read ("all") qualifies every bot through
    # inventory's ONE rule.
    labels = ({} if fleet else qualified_labels(
        a for c in comms for a in (c["sender_alias"], c["recipient_alias"])))
    reply_to = {c["msg_id"]: c["reply_to_msg_id"] for c in comms}

    def chain_root(mid: str) -> str:
        seen = set()
        while reply_to.get(mid) and mid not in seen:
            seen.add(mid)
            mid = reply_to[mid]
        return mid

    # Chain-root FIRST, then promote the whole chain to any member's work
    # item (gauntlet, probed both directions): keying per-message on
    # `work_item_id or chain` split the dispatch from its report whenever
    # exactly one side carried the id.
    root_of = {c["msg_id"]: chain_root(c["msg_id"]) for c in comms}
    wi_of_root: dict = {}
    for c in comms:
        if c["work_item_id"]:
            wi_of_root.setdefault(root_of[c["msg_id"]], c["work_item_id"])

    threads: dict = {}
    for c in comms:
        r = root_of[c["msg_id"]]
        wi = c["work_item_id"] or wi_of_root.get(r)
        key = wi or f"chain:{r}"
        t = threads.setdefault(key, {"key": key, "work_item_id": wi,
                                     "messages": []})
        c.pop("fleet_uid", None)   # the emitting fleet: machinery, off the story
        c["sender_fleet"] = fleet_of(c["sender_alias"] or "")
        c["recipient_fleet"] = fleet_of(c["recipient_alias"] or "")
        c["cross_fleet"] = bool(
            c["sender_fleet"] and c["recipient_fleet"]
            and c["sender_fleet"] != c["recipient_fleet"])
        if c["cross_fleet"]:
            c["sender_short"] = _qualified(c["sender_alias"])
            c["recipient_short"] = _qualified(c["recipient_alias"])
        else:
            c["sender_short"] = (labels.get(c["sender_alias"])
                                 or _short(c["sender_alias"]))
            c["recipient_short"] = (labels.get(c["recipient_alias"])
                                    or _short(c["recipient_alias"]))
        if not c["recipient_alias"] and c["recipient_raw"]:
            c["recipient_short"] = names.get(c["recipient_raw"], "Telegram")
        # §11: the raw carrier address is SENSITIVE and the name is resolved
        # server-side — the raw id never rides the wire to the page.
        c.pop("recipient_raw", None)
        c["body_words"] = body_words(c["body"])
        c["tx"] = tx_by_msg.get(c["msg_id"], [])
        t["messages"].append(c)

    wi_ids = [t["work_item_id"] for t in threads.values() if t["work_item_id"]]
    titles: dict = {}
    task_events_by_wi: dict = {}
    if wi_ids:
        ph = ",".join("?" * len(wi_ids))
        titles = {r["work_item_id"]: dict(r) for r in conn.execute(
            f"SELECT work_item_id, title, repo FROM work_items"
            f" WHERE work_item_id IN ({ph})", wi_ids).fetchall()}
        for r in conn.execute(
            f"SELECT work_item_id, assignment_id, event, occurred_at,"
            f" ingest_seq, detail FROM events WHERE kind='task'"
            f" AND work_item_id IN ({ph}) ORDER BY ingest_seq", wi_ids):
            task_events_by_wi.setdefault(r["work_item_id"], []).append(dict(r))

    out = []
    for t in threads.values():
        t["messages"].sort(key=lambda m: m["ingest_seq"])
        wi = titles.get(t["work_item_id"]) if t["work_item_id"] else None
        t["title"] = body_words((wi or {}).get("title"))
        t["repo"] = (wi or {}).get("repo")
        t["task_events"] = task_events_by_wi.get(t["work_item_id"], [])
        # SEMANTIC stamps from the one-definition constants (queries.py):
        # terminal = FIRST terminal event by ledger order (the monotone
        # reducer's rule — a late terminal never reopens/rewrites);
        # delivered = any activation-class transmission on the thread.
        t["terminal"] = next(
            (e["event"] for e in t["task_events"]
             if e["event"] in _TERMINAL_SET), None)
        t["delivered"] = any(
            x["activated"] for m in t["messages"] for x in m["tx"])
        # a thread with one cross-fleet message IS a cross-fleet thread —
        # the mark rides the thread, the names ride each message
        t["cross_fleet"] = any(m["cross_fleet"] for m in t["messages"])
        t["latest_seq"] = max(
            [m["ingest_seq"] for m in t["messages"]]
            + [e["ingest_seq"] for e in t["task_events"]])
        out.append(t)
    out.sort(key=lambda t: t["latest_seq"], reverse=True)
    return {"threads": out}


def _fetch_tasks(conn: sqlite3.Connection, fleet: str | None = None) -> dict:
    # `fleet` scopes the board to that fleet's ASSIGNEES (U1 — the same axis
    # every per-fleet route filters on); with no fleet the host-wide board
    # labels a twin-named assignee `fleet/name` (inventory's one rule).
    where, params = "", []
    fleet = _fleet_scope(conn, fleet)
    if fleet:
        where = (" WHERE a.assignee_uid IN (SELECT uid FROM identity_registry"
                 f"  WHERE kind = 'actor' AND {fleet_alias_range()})")
        params = list(fleet_range_params(fleet))
    rows = [dict(r) for r in conn.execute(
        "SELECT a.assignment_id, a.work_item_id, a.assignee_uid,"
        " a.expected_by, a.occurred_at, w.title,"
        " (SELECT alias FROM identity_registry i"
        "   WHERE i.uid = a.assignee_uid) AS assignee_alias"
        " FROM assignments a LEFT JOIN work_items w"
        "   ON w.work_item_id = a.work_item_id"
        f"{where} ORDER BY a.ingest_seq DESC LIMIT 200", params)]
    if not rows:
        return {"assignments": [], "attention_count": 0}
    labels = ({} if fleet
              else qualified_labels(r["assignee_alias"] for r in rows))
    # Derive status/attention for the DISPLAYED ids only (gauntlet,
    # measured 20x): the unrestricted derivations walked every assignment
    # ever to render 200. The restriction is APPENDED so queries.py stays
    # the one definition; output verified byte-identical.
    ids = [r["assignment_id"] for r in rows]
    ph = ",".join("?" * len(ids))
    # status AND the instant it ended, from ONE row (chunk L fold, #1479):
    # `terminal_at` is the occurred_at of the same first-terminal event
    # TASK_STATUS_SQL names as the status, so a finished card reads
    # "completed 1m ago" instead of a deadline it no longer has — and can
    # never read one event's name over another's instant, which a separate
    # MIN(occurred_at) did.
    status = {r["assignment_id"]: (r["status"], r["terminal_at"])
              for r in conn.execute(
                  TASK_STATUS_SQL + f" WHERE a.assignment_id IN ({ph})", ids)}
    now = _now_iso()
    # WHY a row needs attention — the queue's OWN arms, stamped by the query
    # that selected the row (ATTENTION_ARMS_SQL is ATTENTION_SQL plus one
    # column per arm, both built from `queries.ATTENTION_ARMS`): the card says
    # "send failed 12h ago" / "queued 12h ago, never delivered" / "overdue 2h"
    # instead of a bare flag, and nothing here re-derives an arm in Python.
    arms = {r["assignment_id"]: r for r in conn.execute(
        ATTENTION_ARMS_SQL + f" AND a.assignment_id IN ({ph})",
        (now, now, *ids))}
    for r in rows:
        r["title"] = body_words(r["title"])
        r["assignee_short"] = (labels.get(r["assignee_alias"])
                               or _short(r["assignee_alias"]))
        st, terminal_at = status.get(r["assignment_id"],
                                     ("created_not_sent", None))
        r["status"] = st
        r["terminal_at"] = terminal_at
        arm = arms.get(r["assignment_id"])
        r["attention"] = arm is not None
        # the arms in the operator's priority order; the two SEND arms date
        # from the dispatch, the deadline arm from the deadline
        reasons = [name for name, _ in ATTENTION_ARMS if arm and arm[name]]
        # the PRIMARY arm dates the card: only a purely-overdue row is dated
        # from its deadline (`overdue` is last in the priority order)
        since = None
        if reasons:
            since = (r["expected_by"] if reasons[0] == "overdue"
                     else r["occurred_at"])
        r["attention_reason"] = reasons
        r["attention_since"] = since
    return {"assignments": rows,
            "attention_count": sum(1 for r in rows if r["attention"])}


# The rail renders PARTICIPANTS. The 2b registry scan mints an identity
# for every entity it keyframes — 200+ library items, projects, the host,
# the vault, and a bot_instance twin per actor — and an unfiltered read
# flooded the fleet rail with all of them (operator-flagged: "it used to
# only have bots!"). Entity identities belong to the registry surfaces
# (chunk B), not the roster.
_RAIL_KINDS = ("fleet", "actor")   # humans resolve as actors


def _fetch_identities(conn: sqlite3.Connection,
                      fleet: str | None = None) -> dict:
    ph = ",".join("?" * len(_RAIL_KINDS))
    scope, params = "", list(_RAIL_KINDS)
    fleet = _fleet_scope(conn, fleet)
    if fleet:
        # the room's participants (U1): the fleet itself, its bots, and every
        # human — a human is a participant of every room (the operator talks
        # to both fleets and belongs to neither)
        scope = (" AND ((kind = 'fleet' AND alias = ?)"
                 f"  OR (kind = 'actor' AND ({fleet_alias_range()}"
                 "   OR alias LIKE 'human:%')))")
        params += [fleet, *fleet_range_params(fleet)]
    rows = []
    for r in conn.execute(
        "SELECT uid, kind, alias, provisional, first_seen, last_seen"
        f" FROM identity_registry WHERE kind IN ({ph})"
        f" AND {not_sentinel_sql()}"
        f"{scope} ORDER BY last_seen DESC LIMIT 200", params):
        row = dict(r)
        row["short"] = _short(row["alias"])
        # a human actor is legitimately-provisional (never in a roster to
        # confirm) — do NOT badge it as an unconfirmed suspect
        if row["alias"].startswith("human:"):
            row["provisional"] = 0
        rows.append(row)
    if not fleet:
        # the host-wide rail: twins across fleets read `fleet/name`
        labels = qualified_labels(r["alias"] for r in rows if r["kind"] == "actor")
        for row in rows:
            row["short"] = labels.get(row["alias"]) or row["short"]
    return {"identities": rows}


# FTS markers: snippet() must not hand the client pre-built markup (every
# body is bot-authored text that the client escapes wholesale), so matches
# are bracketed with markers the client swaps for <mark> AFTER escaping the
# whole string. The markers are PER-REQUEST RANDOM (control byte + hex —
# untouched by esc()), bound as snippet() parameters and shipped in the
# payload: a bot-authored body cannot predict them, so a literal \x01 in a
# message can never forge a highlight (gauntlet, probed — fixed markers
# could).


def _fts_query(q: str) -> str:
    """User text -> safe FTS5 query: each whitespace token double-quoted
    (implicit AND). Kills advanced MATCH syntax deliberately — an unbalanced
    quote or stray NEAR( from a human search must never read as a syntax
    ERROR (which the envelope would misclassify as source trouble). Control
    bytes are stripped FIRST: a NUL terminates the bound string mid-quote,
    defeating the quoting into exactly that misclassification (the 8th
    hostile shape — gauntlet, probed)."""
    q = "".join(c if c >= " " else " " for c in q)
    toks = [t.replace('"', '""') for t in q.split()]
    return " ".join(f'"{t}"' for t in toks)


def _room_filter(fleet: str) -> tuple[str, list]:
    """The room axis — sender fleet OR recipient fleet — as a filter clause.
    Search uses this post-MATCH form (FTS drives the query, so the OR is a
    cheap filter); the CHANNEL must keep its UNION-of-equality-arms form
    (0004's measured lesson) — an axis change (e.g. a third arm) visits
    both sites."""
    return (" (fleet_uid = (SELECT uid FROM identity_registry"
            "  WHERE kind='fleet' AND alias = ?)"
            "  OR recipient_fleet = ?)"), [fleet, fleet]


def _fetch_search(conn: sqlite3.Connection, q: str, fleet: str | None,
                  limit: int) -> dict:
    fleet = _fleet_scope(conn, fleet)
    # §11 completeness clause: search must STATE what it cannot see — both
    # halves of the spec's wording, "redacted OR TRUNCATED". body IS NULL =
    # no words at all (metadata capture / body-less send); truncated = the
    # words were CUT at the capture cap, so a term past the cap is
    # unfindable while the row still looks searchable (external review,
    # probed with a term at byte 16390). TWO count queries, each predicate
    # in its own WHERE — never FILTER aggregates: SQLite cannot use a
    # partial index whose predicate lives only inside a FILTER, so the
    # combined form scanned every room row past all four partials
    # (external round 2, measured 43ms -> 1ms at 500k). Pinned by an
    # index-USAGE EXPLAIN test, not mere index existence.
    def _completeness_count(pred: str) -> int:
        sql = f"SELECT COUNT(*) FROM communications WHERE {pred}"
        params: list = []
        if fleet:
            clause, params = _room_filter(fleet)
            sql += " AND" + clause
        return conn.execute(sql, params).fetchone()[0]

    unsearchable = _completeness_count("body IS NULL")
    partially_indexed = _completeness_count(
        "truncated = 1 AND body IS NOT NULL")

    out = {"query": q, "unsearchable": unsearchable,
           "partially_indexed": partially_indexed}
    match = _fts_query(q)
    if not match:
        return {**out, "results": []}
    mopen = "\x01" + secrets.token_hex(4) + "\x01"
    mclose = "\x02" + secrets.token_hex(4) + "\x02"
    sql = (
        "SELECT c.ingest_seq, c.msg_id, c.occurred_at, c.sender_alias,"
        " c.recipient_alias, c.message_class, c.work_item_id,"
        " snippet(comms_fts, 0, ?, ?, ' … ', 12) AS snip"
        " FROM comms_fts JOIN communications c"
        "   ON c.ingest_seq = comms_fts.rowid"
        " WHERE comms_fts MATCH ?")
    params: list = [mopen, mclose, match]
    if fleet:
        # Unqualified columns bind to `c` unambiguously — comms_fts declares
        # only `body` (measured; the earlier .replace() qualification was
        # probed fragile against substring column names — round 2).
        clause, extra = _room_filter(fleet)
        sql += " AND" + clause
        params.extend(extra)
    # ORDER BY the FTS rowid, never c.ingest_seq: identical order by
    # construction (rowid IS ingest_seq under 0005), but the rowid form
    # rides FTS5's internal index and early-exits on LIMIT where the column
    # form temp-B-tree-sorts every match (measured 123ms -> 0.2ms at 100k
    # rows — gauntlet). Pinned by an EXPLAIN test.
    sql += " ORDER BY comms_fts.rowid DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params)]
    # the host-wide read qualifies twins (U2b) — a hit attributed to `one`
    # on a two-fleet host names nobody
    labels = ({} if fleet else qualified_labels(
        a for r in rows for a in (r["sender_alias"], r["recipient_alias"])))
    for row in rows:
        sa, ra = row.pop("sender_alias"), row.pop("recipient_alias")
        row["sender_short"] = labels.get(sa) or _short(sa)
        row["recipient_short"] = labels.get(ra) or _short(ra)
    return {**out, "results": rows,
            "marker_open": mopen, "marker_close": mclose}


def _fetch_trust(conn: sqlite3.Connection, root: Path) -> dict:
    """The trust/gaps surface (§16 F8): what the plane can and cannot see —
    refused events (quarantine, with reasons), not-yet-ingested (spool),
    per-door emitter freshness, per-fleet capture policy + last activity,
    unconfirmed identities. The panel that keeps an empty board honest."""
    # Gaps = events that ARRIVED and were refused. Quarantine entries carry
    # a .reason sidecar; sample the newest few verbatim.
    # Quarantine: LISTABILITY probed via source_state.probe_dir (the
    # decided-once rule) — an unreadable dir must never read as
    # quarantined=0 (the false all-clear this panel exists to kill;
    # macOS glob swallows PermissionError silently — probed). Entries are
    # guarded per-file: the daemon mutates this dir concurrently, and one
    # reaped-between-glob-and-stat entry must not take down the whole
    # panel (probed TOCTOU); a directory named *.json is not an event.
    quarantined, reasons = 0, []
    # Same shared door as the spool count (scan_spool — external rounds 3+4:
    # enumeration-atomic, state-bearing, one definition across surfaces).
    qscan = scan_spool(root)
    quarantine_state = qscan.quarantine_state
    if quarantine_state == "ok":
        entries = []
        for f in qscan.quarantined:
            try:
                if f.is_file():
                    entries.append((f.stat().st_mtime, f))
            except OSError:
                continue  # reaped mid-walk — skip the entry, never crash
        entries.sort(reverse=True)
        quarantined = len(entries)
        for _, f in entries[:5]:
            sidecar = f.with_name(f.name + ".reason")
            try:
                # the door writes reason + newline — strip for display
                reason = sidecar.read_text()[:300].strip()
            except OSError:
                reason = "(no reason recorded)"
            reasons.append({"event": f.name, "reason": reason})
    spool, spool_oldest, spool_state = _spool_pending(root)

    # Per-door freshness: every emitter ever seen, across EVERY envelope-
    # bearing table — the roster is ingest.py's _CONSTRUCT_TABLE registry
    # (+ events), never a hand-list (the first version hand-listed three of
    # five tables and a door whose only activity was workstream opens read
    # as NEVER FIRED — the exact misread this panel exists to prevent;
    # gauntlet, all three reviewers). Freshness is keyed on ingest_seq —
    # the schema's ordering authority — never MAX(occurred_at): emitter
    # clocks skew (the RTC-less Pi future-stamps at every boot, and one
    # future timestamp would shadow real freshness forever), and mixed-
    # offset ISO strings compare lexically wrong (probed). Aggregates are
    # pushed into each arm so the outer GROUP BY sees per-table rollups,
    # not every row (measured 735ms -> 470ms at 505k rows, unindexed).
    tables = ["events", *sorted(set(CONSTRUCT_TABLES.values()))]
    arms = " UNION ALL ".join(
        f"SELECT emitter, MAX(ingest_seq) AS seq, COUNT(*) AS n"
        f" FROM {t} GROUP BY emitter" for t in tables)
    emitters = [dict(r) for r in conn.execute(
        f"SELECT emitter, MAX(seq) AS last_seq, SUM(n) AS events"
        f" FROM ({arms}) GROUP BY emitter ORDER BY last_seq DESC")]
    for e in emitters:
        row = conn.execute(
            "SELECT ingested_at FROM ingest_ledger WHERE ingest_seq = ?",
            (e.pop("last_seq"),)).fetchone()
        e["last_at"] = row["ingested_at"] if row else None

    # Per-fleet: capture policy (the words-vs-metadata knob) + last comm.
    # A fleet with a policy but no rows is a DORMANT emitter — unarmed or
    # never fired; the difference is compose-side and disclosed as such.
    try:
        capture = load_capture_config(root)
        capture_state = "ok"
    except CaptureConfigError:
        capture, capture_state = {}, "malformed"
    # Liveness keyed on ingest_seq -> LEDGER time, the same rule the emitter
    # panel already followed — MAX(occurred_at) trusts producer clocks, and
    # one 2099-dated row from a skewed producer pinned a fleet at "0s ago"
    # forever (external review, probed). Producer time is not shown here;
    # if it ever is, it must be labeled producer-reported/untrusted.
    fleets = [dict(r) for r in conn.execute(
        "SELECT i.alias AS fleet, MAX(c.ingest_seq) AS last_seq,"
        " COUNT(c.msg_id) AS comms"
        " FROM identity_registry i"
        " LEFT JOIN communications c ON c.fleet_uid = i.uid"
        " WHERE i.kind='fleet' GROUP BY i.alias")]
    for f in fleets:
        seq = f.pop("last_seq")
        row = conn.execute(
            "SELECT ingested_at FROM ingest_ledger WHERE ingest_seq = ?",
            (seq,)).fetchone() if seq is not None else None
        f["last_comm_at"] = row["ingested_at"] if row else None
    seen = {f["fleet"] for f in fleets}
    for f in fleets:
        f["capture"] = capture_mode(capture, f["fleet"])  # the ONE rule
    for alias, mode in sorted(capture.items()):
        if alias != "*" and alias not in seen:
            fleets.append({"fleet": alias, "last_comm_at": None, "comms": 0,
                           "capture": mode,
                           "note": "policy declared, no events ever —"
                                   " dormant (unarmed or never fired)"})

    provisional = conn.execute(
        "SELECT COUNT(*) FROM identity_registry WHERE provisional = 1"
    ).fetchone()[0]
    return {
        "quarantined": quarantined,
        "quarantine_state": quarantine_state,
        "quarantine_reasons": reasons,
        "spool_pending": spool,
        "spool_oldest_at": spool_oldest,
        "spool_state": spool_state,
        "emitters": emitters,
        "fleets": fleets,
        "capture_config": capture_state,
        "provisional_identities": provisional,
    }


def _spool_pending(root: Path) -> tuple[int, str | None, str]:
    """(pending count, oldest spooled_at, source state). Doctor's definition —
    NON-recursive *.json in the spool dir (the first version rglob'd, which
    counted quarantine + sidecars + inflight claims — gauntlet).
    spool_dir()/spool_entries() are not callable here — they mkdir, and this
    daemon is read-only — so the DEFINITION is replicated with this note as
    the drift guard. The state is probed, never inferred from an empty glob:
    an unreadable spool (or spool PARENT — external review's blocker) must
    surface as 'unreadable', because the numeric zero is a lie there and a
    green zero from a tree the reader cannot reach is the false all-clear
    this whole panel exists to kill. ABSENT stays a legitimate zero — the
    spool dir is created lazily on first spooled event."""
    # THE spool definition — spool.scan_spool, shared with plane status and
    # plane doctor so the three surfaces can never disagree about the same
    # tree (external round 4: doctor printed a green zero for the exact
    # spool this panel called unreadable). State-bearing and enumeration-
    # atomic (scan_dir inside); the count is withheld unless readable.
    sc = scan_spool(root)
    if sc.spool_state == "unreadable":
        return 0, None, "unreadable"
    return len(sc.pending), oldest_spooled_at(sc.pending), "ok"


# --------------------------------------------------------------------------
# Presence inputs — shared by /api/presence and /api/overview (U3), so the
# strip's per-fleet verdicts are the SAME derivation the presence panel
# shows, never a second copy of its scoping.
# --------------------------------------------------------------------------

def _stale_after_s() -> float:
    # the staleness horizon is keepalive's active window (a separate
    # process, so an env knob is the only carrier) — coupled, not a twin
    # literal; default matches the keepalive default
    try:
        return float(os.environ.get("KEEPALIVE_ACTIVE_WINDOW_S",
                                    STALE_AFTER_S))
    except ValueError:
        return STALE_AFTER_S


def _live_panes(sampler) -> tuple[list, bool]:
    """The sampler's live half: (panes, degraded). It must fail
    INDEPENDENTLY of the recorded half: snapshot() is a pure cache read,
    but a raise or a shape without "panes" must not 500 a panel and take
    the recorded half down with it (probed) — degrade to no live poll,
    disclosed, exactly as the db side does."""
    if not sampler.available:
        return [], False
    try:
        return list(sampler.snapshot().get("panes", [])), False
    except Exception:  # noqa: BLE001 — a read door must not crash
        return [], True


def _heartbeat_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(zip(("alias", "value", "ingested_at"), row))
            for row in conn.execute(LATEST_HEARTBEAT_SQL)]


def _scope_presence(recorded: list, live: list, fleet: str) -> tuple[list, list]:
    """Both halves scoped to ONE fleet (U1): a tab's verdicts and counts
    are the room's, not the host's — and a twin-named bot on the other
    fleet never joins this room's count. Exact on the fleet segment, like
    every SQL arm (queries.fleet_alias_range): a case-variant fleet name
    is a different fleet on the plane, so it is a different room here."""
    prefix = f"bot:{fleet}/"
    return ([r for r in recorded
             if str(r.get("alias") or "").startswith(prefix)],
            [p for p in live if p.get("fleet") == fleet])


# --------------------------------------------------------------------------
# The two-fleet overview strip (U3): one row per fleet, one host row
# --------------------------------------------------------------------------

# the room's newest report: the channel's UNION-of-equality-arms form
# (0003 sender arm / 0004 recipient arm — never an OR, 0004's lesson)
_NEWEST_REPORT_SQL = (
    "SELECT occurred_at FROM ("
    " SELECT * FROM (SELECT occurred_at, ingest_seq FROM communications"
    "  WHERE fleet_uid = ? AND message_class = 'report'"
    "  ORDER BY ingest_seq DESC LIMIT 1)"
    " UNION"
    " SELECT * FROM (SELECT occurred_at, ingest_seq FROM communications"
    "  WHERE recipient_fleet = ? AND message_class = 'report'"
    "  ORDER BY ingest_seq DESC LIMIT 1))"
    " ORDER BY ingest_seq DESC LIMIT 1")
_REPORTS_SINCE_SQL = (
    "SELECT COUNT(*) FROM ("
    " SELECT msg_id FROM communications WHERE fleet_uid = ?"
    "  AND message_class = 'report' AND occurred_at >= ?"
    " UNION"
    " SELECT msg_id FROM communications WHERE recipient_fleet = ?"
    "  AND message_class = 'report' AND occurred_at >= ?)")
_HOST_SAMPLES_SQL = (
    # the host probe's newest value per facet (subject_kind=host — the
    # `_host` sentinel's samples, keyed by hostname): the ONE recorded place
    # the Host card's hardware facts come from; a facet the probe never
    # emitted is simply absent (a Pi-only facet off a Pi), never a 0
    "WITH latest AS ("
    " SELECT metric, value, occurred_at,"
    "  ROW_NUMBER() OVER (PARTITION BY metric ORDER BY ingest_seq DESC) AS rn"
    " FROM metric_samples WHERE subject_kind = 'host'"
    "  AND metric IN ('host.load', 'host.mem_available_mb', 'host.disk_free_gb',"
    "                 'host.thermal_flags', 'host.undervoltage', 'host.boot_time'))"
    " SELECT metric, value, occurred_at FROM latest WHERE rn = 1")

# The page's ingest-lag warning threshold, stamped by the API so a JSON
# consumer gets the state and not only the number (structural lens)
_INGEST_LAG_WARN_S = 120


def _plane_readers(root: Path):
    """The install's stdlib `lib/plane-readers.py` — the reader brief and
    `claudlobby report-back` answer through — so the card counts EXACTLY the
    rows the manager's brief lists (`report_rows` + `unacked_rows`, one rule);
    None when the install carries no readable copy (disclosed on the card)."""
    from types import SimpleNamespace
    from ..brief import load_lib_module
    # the plane root's own lib/ (the install on a host), else the package's
    # checkout (an editable install serving a plane under another root)
    for lib in (Path(root) / "lib", Path(__file__).resolve().parents[2] / "lib"):
        if (lib / "plane-readers.py").is_file():
            return load_lib_module(SimpleNamespace(lib=lib), "plane-readers.py")
    return None


def _host_samples(conn: sqlite3.Connection) -> dict | None:
    """metric -> {value, occurred_at} for the host probe's newest facets;
    None when the probe has never recorded (dormant, or armed on another
    host) — absent is not an empty dashboard."""
    rows = conn.execute(_HOST_SAMPLES_SQL).fetchall()
    if not rows:
        return None

    def _val(raw):
        try:
            return json.loads(raw)   # `value` is the JSON the emitter sent
        except (TypeError, ValueError):
            return raw
    return {r[0]: {"value": _val(r[1]), "occurred_at": r[2]} for r in rows}


def _spawn_epoch(bot_dir: Path) -> int | None:
    """Mtime of `<bot_dir>/data/.spawn` — start-bot.sh touches it on EVERY
    start, so it dates the current incarnation of the session — the same
    reading as `dispatch-overdue.py`'s `_spawn_epoch` (a stdlib script every
    consumer shells, so it is mirrored, not imported); None = unreadable,
    and the matcher's rule for that is "not an orphan" (the row stays
    overdue), never a guess."""
    try:
        return int((bot_dir / "data" / ".spawn").stat().st_mtime)
    except OSError:
        return None


def _epoch(iso: str | None) -> float | None:
    d = _parse_iso(str(iso)) if iso else None   # presence's parser, not an eighth
    return d.timestamp() if d else None


def _fetch_overview(conn: sqlite3.Connection, root: Path, live: list,
                    live_poll: str) -> dict:
    """The strip's rows. Every figure is a PLANE fact through the doors
    that already define it — never a bare zero where a source is absent:

    * `bots` — `_fetch_fleets`'s actor count, with its unconfirmed
      (provisional) part disclosed beside it, never absorbed.
    * `presence` — `derive_presence` over the SAME scoped halves the
      presence panel uses; `live_poll` says whether the sampler answered.
    * `open` — the MATCHER's rule, `OPEN_ASSIGNMENTS_AT_SQL` per actor as
      of now: the same count `claudlobby brief --bot`, fleet-pulse and
      `dispatch-overdue.py` show (its sibling closure retires a re-dispatch
      of one task id when a later one completes). ONE definition of open,
      so the strip can never disagree with the watchdog on the same fleet;
      per actor because the query is indexed on the assignee.
    * `attention` / `overdue` — `ATTENTION_ARMS_SQL` with the fleet's
      assignees APPENDED as a restriction (the tasks door's pattern:
      queries.py stays the one definition); overdue is the deadline ARM,
      read off the query's own column rather than re-derived here.
    * `orphaned` — the watchdog's split (#835): an id'd overdue dispatch
      older than the bot's `.spawn` was lost to a restart. It needs the
      bot's DIRECTORY, so a fleet with none under the view's root reports
      None + a reason (UNKNOWN is not zero — #1014's rule).
    * `newest_report_at` / `reports_24h` — `report`-class communications
      on the room axis (sent by the fleet OR to it).
    * `unacked` — the same axis past the fleet's newest ack (chunk K:
      `brief --ack` records a `reports_acked` event; the manager is whoever
      acks, the newest ack of any actor wins); None + a reason when the
      fleet has never acked — no read position is a different fact from a
      backlog, and "everything ever" would be a number nobody asked for.
    * `last_activity_at` — the fleet identity's `last_seen`: LEDGER time,
      advanced by every emission under the fleet (a producer clock would
      let one future-stamped row pin a fleet at "0s ago" forever).
    * `capture` — the recorder's policy through `capture_mode`, the ONE
      rule, as /api/trust reads it.
    The host row is `_fetch_summary` plus the ingest lag, and `totals` is
    those rows summed once for the header — with the disclosures a sum
    swallows (`_totals`)."""
    from datetime import datetime, timedelta, timezone
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    day_ago = (now_dt - timedelta(hours=24)).isoformat()
    try:
        capture = load_capture_config(root)
        capture_state = "ok"
    except CaptureConfigError:
        capture, capture_state = {}, "malformed"
    recorded = _heartbeat_rows(conn)
    stale_after = _stale_after_s()
    bot_dirs = {(fl, b): d for fl, b, d in discover_bot_dirs(root)}
    actors = _fleet_actors(conn)
    pr = _plane_readers(root)
    fl = _fetch_fleets(conn, actors)
    rows = []
    for f in fl["fleets"]:
        alias = f["alias"]
        mine = actors.get(alias, [])
        uids = [r["uid"] for r in mine]
        alias_of = {r["uid"]: r["alias"] for r in mine}
        rec, lv = _scope_presence(recorded, live, alias)
        verdicts = derive_presence(rec, lv, now=now_dt, stale_after_s=stale_after)
        # open rows: (uid, occurred_at, source_ref, assignment_id, expected_by)
        open_rows = [(uid, *r) for uid in uids for r in conn.execute(
            OPEN_ASSIGNMENTS_AT_SQL, (uid, None, None, None, None, None, None))]
        open_n = len(open_rows)
        attention = overdue = 0
        if uids:
            ph = ",".join("?" * len(uids))
            # the queue's rows for this fleet WITH their arms: ONE read
            # answers attention AND overdue (its deadline arm), through the
            # same query the task board reads its reasons from — the strip
            # had a third Python re-derivation of the same arm (fold F2)
            att = conn.execute(
                ATTENTION_ARMS_SQL + f" AND a.assignee_uid IN ({ph})",
                (now, now, *uids)).fetchall()
            attention = len(att)
            overdue = sum(1 for r in att if r["overdue"])
        orphaned: int | None = 0
        orphaned_reason = None
        fleet_dirs = {b: d for (fl_, b), d in bot_dirs.items() if fl_ == alias}
        if not fleet_dirs:
            orphaned, orphaned_reason = None, (
                "no bot directories for this fleet under the view's root —"
                " orphan-ness compares a dispatch against the bot's .spawn")
        else:
            for uid, occurred, ref, _asg, expected_by in open_rows:
                if not (expected_by and expected_by < now):
                    continue
                # only an id'd dispatch can orphan (the matcher's rule: an
                # id-less one closes on ANY later terminal report)
                idd = bool(ref) and ref.startswith("dispatch-log:") \
                    and not ref.startswith("dispatch-log:sha:")
                bot_dir = fleet_dirs.get(_short(alias_of.get(uid)) or "")
                spawn = _spawn_epoch(bot_dir) if bot_dir else None
                da = _epoch(occurred)
                # whole seconds on both sides, the matcher's comparison: a
                # restart in the dispatch's own second is not an orphan
                if idd and spawn is not None and da is not None \
                        and spawn > int(da):
                    orphaned += 1
        newest = conn.execute(_NEWEST_REPORT_SQL, (f["uid"], alias)).fetchone()
        reports_24h = conn.execute(
            _REPORTS_SINCE_SQL, (f["uid"], day_ago, alias, day_ago)).fetchone()[0]
        unacked, unacked_reason, acked_by, acked_at = None, None, None, None
        if pr is None:
            unacked_reason = ("the install's lib/plane-readers.py is unreadable — the card"
                              " cannot count what the brief lists")
        elif not uids:
            unacked_reason = "no actor of this fleet on the plane — nobody could have acked"
        else:
            # the fleet's newest readable ack by ANY of its actors (the manager
            # is whoever acks; the card is a glance), then the rows past it
            # through the SAME rule brief's list applies (unacked_rows)
            ack = pr.newest_ack(conn, uids)
            if ack is None:
                unacked_reason = ("no ack recorded — `claudlobby brief --ack` has never"
                                  " run for this fleet")
            else:
                past = pr.report_rows(conn, alias, since_seq=ack["seq"])
                unacked = len(pr.unacked_rows(past, ack["seq"], pr.TERMINAL_STATUSES))
                acked_by, acked_at = _short(ack["by"]), ack["acked_at"]
        rows.append({
            "alias": alias, "bots": f["bots"], "provisional": f["provisional"],
            "presence": {"counts": presence_counts(verdicts),
                         "live_poll": live_poll},
            "open": open_n, "attention": attention, "overdue": overdue,
            "orphaned": orphaned, "orphaned_reason": orphaned_reason,
            "newest_report_at": newest[0] if newest else None,
            "reports_24h": reports_24h,
            "unacked": unacked, "unacked_reason": unacked_reason,
            "acked_by": acked_by, "acked_at": acked_at,
            "last_activity_at": f["last_seen"],
            "last_comm_at": f["last_comm_at"],
            "capture": capture_mode(capture, alias),
        })
    prov = _provenance(root, conn)
    last_in = _epoch(prov.get("last_ingest_at"))
    lag = (round(max(0.0, now_dt.timestamp() - last_in), 1)
           if last_in is not None else None)
    host = {
        **_recorder_state(root),
        "rows": conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0],
        "last_ingest_at": prov.get("last_ingest_at"),
        # lag from LEDGER time; None (never a zero) when nothing was ingested
        "ingest_lag_s": lag,
        "ingest_lag_state": ("none" if lag is None
                             else "warn" if lag > _INGEST_LAG_WARN_S else "ok"),
        "samples": _host_samples(conn),
    }
    return {"fleets": rows, "default": fl["default"], "host": host,
            "capture_config": capture_state, "totals": _totals(rows, live_poll)}


# worst-first: the header must not report a healthy poll because one fleet's
# was fine (§16 — a disclosure a total swallows is a total that lies)
_POLL_RANK = {"ok": 0, "degraded": 1, "unavailable": 2}


def _totals(rows: list[dict], live_poll: str) -> dict:
    """The host-wide totals the header renders, summed HERE with the
    disclosures the cards carry (chunk L fold, #1479): the page summed
    `bots`/`working`/`attention`/`overdue` itself and dropped the unconfirmed
    part and the live-poll state on the way — one definition of a total, and
    it ships the caveats with the number. `fleets` is the count summed over,
    so a header can say "no fleet recorded" rather than four zeros."""
    def _sum(pick) -> int:
        return sum(int(pick(r) or 0) for r in rows)

    worst = max((r["presence"]["live_poll"] for r in rows),
                key=lambda s: _POLL_RANK.get(s, 3), default=live_poll)
    return {
        "fleets": len(rows),
        "bots": _sum(lambda r: r["bots"]),
        "provisional": _sum(lambda r: r["provisional"]),
        "working": _sum(lambda r: r["presence"]["counts"].get("working")),
        "attention": _sum(lambda r: r["attention"]),
        "overdue": _sum(lambda r: r["overdue"]),
        "live_poll": worst,
    }


def _recorder_state(root: Path) -> dict:
    """The recorder's liveness and spool, shared by the summary and the
    Host card. Socket path honors PLANE_SOCKET like the shim and doctor
    (the first version re-derived the default path — the exact
    overridden-socket defect the last gauntlet fixed in doctor,
    re-imported). Liveness is a PROBE, not file presence: a crashed
    daemon's stale socket file stats fine (health from an artifact — the
    fail-toward-fine direction)."""
    spool, spool_oldest, spool_state = _spool_pending(root)
    sock = Path(os.environ["PLANE_SOCKET"]) if os.environ.get("PLANE_SOCKET") \
        else socket_path(Path(root))
    return {
        "ingest_socket_present": sock.exists(),
        "daemon_serving": bool(sock.exists() and probe_daemon(sock)),
        "spool_files": spool,
        "spool_oldest_at": spool_oldest,
        "spool_state": spool_state,
    }


def _fetch_summary(conn: sqlite3.Connection, root: Path) -> dict:
    counts = {}
    for t in ("communications", "work_items", "assignments", "workstreams",
              "events", "identity_registry", "ingest_ledger"):
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return {"counts": counts, **_recorder_state(root)}


# --------------------------------------------------------------------------
# Shutdown: the held-stream release (chunk L fold, #1479)
# --------------------------------------------------------------------------

def begin_shutdown(app) -> None:
    """Tell every held SSE stream to finish NOW.

    Called from the RUNNER's signal hook, which is the only moment that
    works: uvicorn sets `should_exit` on the signal, then waits
    `timeout_graceful_shutdown` for in-flight requests, and sends the
    lifespan shutdown only AFTER that wait (`uvicorn/server.py::shutdown`).
    An event set in the lifespan's shutdown branch alone would therefore be
    set BY the streams' own cancellation, far too late to prevent it —
    measured with this hook removed and that branch left in place: 5.23s to
    exit, against 5.18s for no event at all and 0.26s for this.

    Safe from a signal handler: the set is handed to the loop with
    `call_soon_threadsafe`, which also wakes it. A view with no running
    lifespan (a bare `create_app` in a test) has no event and no streams to
    release, so this is a no-op."""
    state = getattr(app, "state", None)
    event = getattr(state, "shutting_down", None)
    if event is None:
        return
    loop = getattr(state, "loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(event.set)
    else:   # pragma: no cover - the loop is gone; nothing is streaming
        event.set()


def _stopping(app) -> bool:
    event = getattr(getattr(app, "state", None), "shutting_down", None)
    return bool(event is not None and event.is_set())


async def _idle_tick(app, seconds: float) -> bool:
    """The stream's idle wait: `seconds`, or less if the daemon starts to
    stop. True = stopping (the caller ends the response)."""
    event = getattr(getattr(app, "state", None), "shutting_down", None)
    if event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        return False
    return True


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app(root: Path, sampler: PaneSampler | None = None):
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError(
            "the plane UI needs the [plane-ui] extra: "
            f"pip install -e '.[plane-ui]' ({_IMPORT_ERROR})"
        )
    root = Path(root)
    sampler = sampler or PaneSampler(root)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app):  # pragma: no cover - exercised live
        # The held-stream release (chunk L fold, #1479). Created INSIDE the
        # running loop and published on app.state; `begin_shutdown` sets it
        # the moment the daemon is asked to stop, and the SSE generators end
        # their response normally instead of being cancelled at the graceful
        # ceiling. Also set on the way out, for a runner that reaches the
        # lifespan's shutdown first (uvicorn does not — see begin_shutdown).
        _app.state.shutting_down = asyncio.Event()
        _app.state.loop = asyncio.get_running_loop()
        sampler.start()
        try:
            yield
        finally:
            _app.state.shutting_down.set()
            await sampler.stop()

    app = FastAPI(title="observable plane", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=_lifespan)
    started_at = _now_iso()

    @app.get("/api/summary")
    def summary():
        return JSONResponse(_envelope(root, lambda c: _fetch_summary(c, root)))

    @app.get("/api/channel")
    def channel(limit: int = 120, fleet: str | None = None):
        limit = max(1, min(int(limit), _CHANNEL_LIMIT_MAX))
        names = _channel_names(root)
        return JSONResponse(
            _envelope(root,
                      lambda c: _fetch_channel(c, names, limit, fleet)))

    @app.get("/api/tasks")
    def tasks(fleet: str | None = None):
        return JSONResponse(_envelope(root, lambda c: _fetch_tasks(c, fleet)))

    @app.get("/api/identities")
    def identities(fleet: str | None = None):
        return JSONResponse(
            _envelope(root, lambda c: _fetch_identities(c, fleet)))

    @app.get("/api/fleets")
    def fleets():
        """The fleet dimension (U1): every fleet the host records, with the
        tab a first visit should open. Read from the registry's fleet
        identities, never the rail's bounded window."""
        return JSONResponse(_envelope(root, _fetch_fleets))

    @app.get("/api/grid")
    def grid(focus: str | None = None, fleet: str | None = None):
        """Thumbnail grid from the ONE bounded sampler (§14: browsers read
        the cache; sampling cadence is viewer-count-invariant). `focus=bot`
        raises that pane's cadence/height for a short TTL — view-internal
        lens state, touching neither fleet nor db; the read-only ruling is
        about the FLEET, and this endpoint stays observational."""
        if not sampler.available:
            return JSONResponse({
                "state": "unavailable",
                "provenance": {"source": "tmux", "checked_at": _now_iso()},
                "remediation": "tmux not found on the view daemon's PATH —"
                               " the grid needs it; channel/tasks are"
                               " unaffected (§14 degradable)",
            })
        fleet = fleet if fleet != "all" else None
        if fleet:
            probe = _envelope(root, lambda c: _fleet_scope(c, fleet))
            if probe.get("state") == "unknown" and fleet not in {
                    p.get("fleet") for p in sampler.snapshot().get("panes", [])}:
                return JSONResponse(probe)
        if focus:
            sampler.focus(focus, fleet)
        snap = sampler.snapshot()
        if fleet and not focus:
            # the tab's grid (U4): one fleet's panes; a twin-named bot on the
            # other fleet keeps its own slot rather than colliding into this
            # one (panes are keyed (fleet, bot) by the sampler already)
            snap = {**snap, "panes": [p for p in snap["panes"]
                                      if p.get("fleet") == fleet]}
        if focus:
            # The focus overlay renders ONE pane — ship one, not all 18
            # (measured 6.3x payload waste). Filter on the flag the sampler
            # stamped: focus() already resolved the (fleet, bot) ambiguity,
            # and a second half-copy of that predicate shipped TWO panes for
            # twin-named bots (gauntlet round 2, probed).
            snap = {**snap, "panes": [p for p in snap["panes"]
                                      if p["focused"]]}
        return JSONResponse({
            "state": SOURCE_OK,
            "provenance": {"source": "tmux capture-pane", "pid": os.getpid(),
                           "checked_at": _now_iso()},
            "data": snap,
        })

    @app.get("/api/presence")
    def presence(fleet: str | None = None):
        """The Lane C presence derivation (chunk 2): the latest recorded
        heartbeat per bot joined with the sampler's live liveness poll into
        one working/idle/down/stale/unknown/sampling verdict + header
        counts. The two inputs have opposite failure modes and are fetched
        independently — a db that cannot answer returns the panel state
        (the sampler half still renders); the sampler being unavailable
        just leaves every live status ``sampling`` (the recorded half
        still types staleness). Never a table; computed per request."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        # the live half fails INDEPENDENTLY of the recorded half (the
        # endpoint's own law) — `_live_panes` owns that rule for this
        # panel and the overview strip alike
        live, sampler_degraded = _live_panes(sampler)
        fleet = fleet if fleet != "all" else None
        if fleet:
            probe = _envelope(root, lambda c: _fleet_scope(c, fleet))
            if probe.get("state") == "unknown" and fleet not in {
                    p.get("fleet") for p in live}:
                return JSONResponse(probe)
        env = _envelope(root, _heartbeat_rows)
        recorded = env["data"] if env["state"] == SOURCE_OK else []
        if fleet:
            # the tab's verdicts and counts (U1) — both halves scoped to
            # the fleet, so the header strip is the room's, not the host's
            recorded, live = _scope_presence(recorded, live, fleet)
        rows = derive_presence(recorded, live, now=now,
                               stale_after_s=_stale_after_s())
        body = {"data": {"bots": [r.__dict__ for r in rows],
                         "counts": presence_counts(rows),
                         "sampler_available": (sampler.available
                                               and not sampler_degraded)}}
        if env["state"] != SOURCE_OK:
            # recorded half unreachable — still serve the live half, and
            # DISCLOSE the recorded gap (never a silent zero for a source
            # that failed); the UI surfaces this, it does not swallow it
            body["state"] = env["state"]
            body["provenance"] = env.get("provenance", {})
            body["remediation"] = env.get("remediation")
            body["data"]["recorded_unavailable"] = True
        else:
            body["state"] = SOURCE_OK
            body["provenance"] = {
                "source": "heartbeat samples + live sampler",
                "checked_at": _now_iso()}
        return JSONResponse(body)

    @app.get("/api/overview")
    def overview():
        """The two-fleet overview strip (U3): one row per fleet the host
        records — bots, presence, open/attention/overdue/orphaned, report
        freshness, activity, capture policy — and one host row (recorder,
        spool, ingest lag). Every figure through the door that already
        defines it; a figure whose source is absent is None + a reason,
        never a zero (§16). GET, read-only like every route."""
        live, degraded = _live_panes(sampler)
        live_poll = ("unavailable" if not sampler.available
                     else "degraded" if degraded else "ok")
        return JSONResponse(_envelope(
            root, lambda c: _fetch_overview(c, root, live, live_poll)))

    @app.get("/api/inventory")
    def inventory(fleet: str | None = None):
        """Fleet inventory (#1405): what is active on a fleet — bots (compact
        equipment summary), projects, and library items with a used_by
        rollup. A pure read over chunk B's registry doors; the content the
        v1 rail demoted, given its own room. `fleet` omitted = every fleet
        this host records."""
        from .inventory import fleet_inventory
        return JSONResponse(
            _envelope(root, lambda c: fleet_inventory(c, _fleet_scope(c, fleet))))

    @app.get("/api/equipment")
    def equipment(alias: str):
        """One bot's composition + registry change history (#1405). A bot
        with no current keyframe is a typed `idle` state with a remedy —
        absent ≠ empty, never a bare {} the UI would render as a blank
        card."""
        from .inventory import bot_equipment
        env = _envelope(root, lambda c: bot_equipment(c, alias))
        if env.get("state") == SOURCE_OK and env.get("data") is None:
            return JSONResponse({
                "state": "idle",
                "provenance": env.get("provenance", {}),
                "remediation": f"no current keyframe for {alias} — run"
                               " `claudlobby --fleet <name> generate` to scan",
            })
        return JSONResponse(env)

    @app.get("/api/org")
    def org(fleet: str | None = None):
        """The reporting tree from the fleet keyframe (Phase 6): a pure
        read; no fleet keyframe yet is a typed idle state, never {}."""
        from .orgchart import org_tree
        env = _envelope(root, lambda c: org_tree(c, _fleet_scope(c, fleet)))
        if env.get("state") == SOURCE_OK and env.get("data") is None:
            return JSONResponse({"state": "idle", "provenance": env.get("provenance", {}),
                                 "remediation": "no fleet keyframe yet — run"
                                                " `claudlobby --fleet <name> generate`"})
        return JSONResponse(env)

    @app.get("/api/utilization")
    def utilization(fleet: str | None = None):
        """Busy/idle % per bot from the recorded heartbeat samples (Phase 6),
        the legacy rollup's math over the plane's series — one definition."""
        from .utilization import bot_utilization
        return JSONResponse(_envelope(
            root, lambda c: bot_utilization(c, fleet=_fleet_scope(c, fleet))))

    @app.get("/api/search")
    def search(q: str = "", fleet: str | None = None, limit: int = 50):
        limit = max(1, min(int(limit), 200))
        return JSONResponse(
            _envelope(root, lambda c: _fetch_search(c, q, fleet, limit)))

    @app.get("/api/trust")
    def trust():
        return JSONResponse(
            _envelope(root, lambda c: _fetch_trust(c, root)))

    @app.get("/healthz")
    def healthz():
        # ONE envelope, ONE connection (the first version ran two — a db
        # failing between them KeyError'd the health endpoint into a raw
        # 500, probed). 503 on any non-ok state: this endpoint is a
        # data-freshness probe, documented as such in the runbook.
        def probe(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            data = {"schema_user_version": version,
                    "view_started_at": started_at,
                    "corrective": {
                        "daemon": "launchctl kickstart -k"
                                  " gui/$UID/claudlobby-plane-daemon"
                                  "  (or systemctl --user restart"
                                  " claudlobby-plane-daemon)",
                        "doctor": "claudlobby plane doctor",
                    }}
            data.update(_fetch_summary(conn, root))
            return data

        env = _envelope(root, probe)
        return JSONResponse(env, status_code=200 if env["state"] == SOURCE_OK
                            else 503)

    @app.get("/api/stream")
    async def stream(request: Request, cursor: int | None = None,
                     once: int = 0):
        """SSE ledger tail. Cursor resolution: `Last-Event-ID` (browser
        reconnect) > explicit ?cursor= > HEAD. `once=1` emits one batch/ping
        and closes — the bounded seam tests and curl use.

        The stream ENDS ITSELF when the daemon is stopping (chunk L fold,
        #1479): the idle tick waits on the shutdown event OR one second, so
        the response completes normally and the process exits at once. The
        first build wrapped the tail in `except CancelledError`, which
        suppressed nothing — the cancellation lands on the ASGI task inside
        starlette, not on this generator: with the wrapper in place a real
        SIGTERM still took 5.18s and still printed the traceback."""
        last_event_id = request.headers.get("last-event-id")

        async def gen():
            if last_event_id and last_event_id.isdigit():
                last = int(last_event_id)
            elif cursor is not None:
                last = int(cursor)
            else:
                head = _envelope(root, lambda c: c.execute(
                    "SELECT ingest_seq FROM ingest_ledger"
                    " ORDER BY ingest_seq DESC LIMIT 1").fetchone())
                last = (head.get("data") or {"ingest_seq": 0})["ingest_seq"] \
                    if head["state"] == SOURCE_OK and head.get("data") else 0
            yield "retry: 3000\n\n"
            async for frame in _tail(last):
                yield frame

        async def _tail(last: int):
            last_source_state = SOURCE_OK
            while True:
                if await request.is_disconnected() or _stopping(request.app):
                    return
                env = _envelope(root, lambda c: [
                    dict(r) for r in c.execute(
                        "SELECT ingest_seq, family, ingested_at"
                        " FROM ingest_ledger WHERE ingest_seq > ?"
                        " ORDER BY ingest_seq LIMIT 500", (last,))])
                if env["state"] == SOURCE_OK and env["data"]:
                    last_source_state = SOURCE_OK
                    rows = env["data"]
                    last = rows[-1]["ingest_seq"]
                    payload = json.dumps({"rows": rows, "cursor": last})
                    yield f"id: {last}\ndata: {payload}\n\n"
                elif env["state"] != SOURCE_OK:
                    # Deduplicated: one typed frame per state CHANGE, not a
                    # 1/s spam of identical envelopes (gauntlet).
                    if env["state"] != last_source_state:
                        last_source_state = env["state"]
                        yield f"event: source\ndata: {json.dumps(env)}\n\n"
                    else:
                        yield ": ping\n\n"
                else:
                    last_source_state = SOURCE_OK
                    yield ": ping\n\n"
                if once:
                    return
                if await _idle_tick(request.app, 1.0):
                    return   # the daemon is stopping — end the response

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    # Cache-bust token: no-store alone could not EVICT an ES module already
    # pinned in a browser's module map across a redeploy (a stale app.js
    # survived two hard refreshes); a new asset URL forces a fresh fetch
    # unconditionally, stamped onto every asset reference INCLUDING app.js's
    # internal `import "/panel-state.js"` so the module graph busts together.
    # Derived per request from the files' mtimes, never once per process:
    # this estate updates source under running daemons by design
    # (update-siblings pulls weekly; weekly-worker-restart restarts BOTS,
    # not host services), so a process-lifetime token went stale in exactly
    # the redeploy window it was built for (gauntlet round 2). Four stats
    # per page load — index() already reads the file per request.
    _UI_FILES = ("index.html", "app.js", "panel-state.js", "style.css")

    def asset_token() -> str:
        stamp = ":".join(
            str((UI_DIR / f).stat().st_mtime_ns) if (UI_DIR / f).exists()
            else "absent" for f in _UI_FILES)
        return hashlib.sha256(stamp.encode()).hexdigest()[:12]

    def _no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    from fastapi.responses import HTMLResponse, Response

    def _rewritten_index() -> "HTMLResponse":
        tok = asset_token()
        html = (UI_DIR / "index.html").read_text()
        html = (html.replace("/app.js", f"/app.js?v={tok}")
                    .replace("/style.css", f"/style.css?v={tok}"))
        return _no_store(HTMLResponse(html))

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _rewritten_index()

    @app.get("/index.html", response_class=HTMLResponse)
    def index_alias():
        # The mount served this path RAW — an unbusted second door to the
        # page that re-pins stale modules (gauntlet round 2, probed).
        return _rewritten_index()

    @app.get("/app.js")
    def app_js():
        js = (UI_DIR / "app.js").read_text()
        # bust the intra-module import too, or the browser reuses a pinned
        # panel-state.js from its module map.
        js = js.replace('"/panel-state.js"',
                        f'"/panel-state.js?v={asset_token()}"')
        return _no_store(Response(js, media_type="text/javascript"))

    class _NoStoreStatic(StaticFiles):
        async def get_response(self, path, scope):  # pragma: no cover - thin
            return _no_store(await super().get_response(path, scope))

    # html=True dropped: the explicit routes own / and /index.html now.
    app.mount("/", _NoStoreStatic(directory=str(UI_DIR)), name="ui")
    return app
