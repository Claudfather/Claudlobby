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
import json
import re
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
    probe_source,
)
from .daemon import probe_daemon, socket_path
from .ingest import now_iso as _now_iso
from .queries import (
    ACTIVATION_TX_EVENTS,
    ATTENTION_SQL,
    TASK_STATUS_SQL,
    TERMINAL_TASK_EVENTS,
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
    """Alias-first presentation (§11): `bot:fleet/name` -> `name`. The full
    alias stays in the payload for expand/hover; the UI leads with this."""
    if not alias:
        return alias
    return alias.rsplit("/", 1)[-1] if "/" in alias else alias


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


def _fetch_channel(conn: sqlite3.Connection, names: dict, limit: int) -> dict:
    comms = [dict(r) for r in conn.execute(
        "SELECT ingest_seq, msg_id, occurred_at, sender_alias,"
        " recipient_alias, recipient_raw, message_class, command_type,"
        " privacy, body, body_bytes, truncated, work_item_id, assignment_id,"
        " reply_to_msg_id, emitter"
        " FROM communications ORDER BY ingest_seq DESC LIMIT ?", (limit,)
    ).fetchall()]
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
        c["sender_short"] = _short(c["sender_alias"])
        c["recipient_short"] = _short(c["recipient_alias"])
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
        t["latest_seq"] = max(
            [m["ingest_seq"] for m in t["messages"]]
            + [e["ingest_seq"] for e in t["task_events"]])
        out.append(t)
    out.sort(key=lambda t: t["latest_seq"], reverse=True)
    return {"threads": out}


def _fetch_tasks(conn: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT a.assignment_id, a.work_item_id, a.assignee_uid,"
        " a.expected_by, a.occurred_at, w.title,"
        " (SELECT alias FROM identity_registry i"
        "   WHERE i.uid = a.assignee_uid) AS assignee_alias"
        " FROM assignments a LEFT JOIN work_items w"
        "   ON w.work_item_id = a.work_item_id"
        " ORDER BY a.ingest_seq DESC LIMIT 200")]
    if not rows:
        return {"assignments": [], "attention_count": 0}
    # Derive status/attention for the DISPLAYED ids only (gauntlet,
    # measured 20x): the unrestricted derivations walked every assignment
    # ever to render 200. The restriction is APPENDED so queries.py stays
    # the one definition; output verified byte-identical.
    ids = [r["assignment_id"] for r in rows]
    ph = ",".join("?" * len(ids))
    status = {r["assignment_id"]: r["status"] for r in conn.execute(
        TASK_STATUS_SQL + f" WHERE a.assignment_id IN ({ph})", ids)}
    attention = {r[0] for r in conn.execute(
        ATTENTION_SQL + f" AND a.assignment_id IN ({ph})",
        (_now_iso(), *ids))}
    for r in rows:
        r["title"] = body_words(r["title"])
        r["assignee_short"] = _short(r["assignee_alias"])
        r["status"] = status.get(r["assignment_id"], "created_not_sent")
        r["attention"] = r["assignment_id"] in attention
    return {"assignments": rows,
            "attention_count": sum(1 for r in rows if r["attention"])}


def _fetch_identities(conn: sqlite3.Connection) -> dict:
    rows = []
    for r in conn.execute(
        "SELECT uid, kind, alias, provisional, first_seen, last_seen"
        " FROM identity_registry ORDER BY last_seen DESC LIMIT 200"):
        row = dict(r)
        row["short"] = _short(row["alias"])
        rows.append(row)
    return {"identities": rows}


def _spool_pending(root: Path) -> tuple[int, str | None]:
    """(pending count, oldest spooled_at). Doctor's definition — NON-recursive
    *.json in the spool dir: the first version rglob'd, which counted every
    quarantined entry + its .reason sidecar + inflight claims, inflating the
    badge forever (gauntlet). spool_dir()/spool_entries() are not callable
    here — they mkdir, and this daemon is read-only — so the DEFINITION is
    replicated with this note as the drift guard."""
    d = _plane_state_dir(root) / "spool"
    if not d.is_dir():
        return 0, None
    oldest = None
    count = 0
    for f in sorted(d.glob("*.json")):
        count += 1
        try:
            at = json.loads(f.read_text()).get("spooled_at")
            if at and (oldest is None or at < oldest):
                oldest = at
        except (OSError, json.JSONDecodeError):
            continue
    return count, oldest


def _fetch_summary(conn: sqlite3.Connection, root: Path) -> dict:
    counts = {}
    for t in ("communications", "work_items", "assignments", "workstreams",
              "events", "identity_registry", "ingest_ledger"):
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    spool, spool_oldest = _spool_pending(root)
    # Socket path honors PLANE_SOCKET like the shim and doctor (the first
    # version re-derived the default path — the exact overridden-socket
    # defect the last gauntlet fixed in doctor, re-imported). Liveness is a
    # PROBE, not file presence: a crashed daemon's stale socket file stats
    # fine (health from an artifact — the fail-toward-fine direction).
    import os
    sock = Path(os.environ["PLANE_SOCKET"]) if os.environ.get("PLANE_SOCKET") \
        else socket_path(Path(root))
    serving = sock.exists() and probe_daemon(sock)
    return {
        "counts": counts,
        "ingest_socket_present": sock.exists(),
        "daemon_serving": bool(serving),
        "spool_files": spool,
        "spool_oldest_at": spool_oldest,
    }


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app(root: Path):
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError(
            "the plane UI needs the [plane-ui] extra: "
            f"pip install -e '.[plane-ui]' ({_IMPORT_ERROR})"
        )
    root = Path(root)
    app = FastAPI(title="observable plane", docs_url=None, redoc_url=None,
                  openapi_url=None)
    started_at = _now_iso()

    @app.get("/api/summary")
    def summary():
        return JSONResponse(_envelope(root, lambda c: _fetch_summary(c, root)))

    @app.get("/api/channel")
    def channel(limit: int = 120):
        limit = max(1, min(int(limit), _CHANNEL_LIMIT_MAX))
        names = _channel_names(root)
        return JSONResponse(
            _envelope(root, lambda c: _fetch_channel(c, names, limit)))

    @app.get("/api/tasks")
    def tasks():
        return JSONResponse(_envelope(root, _fetch_tasks))

    @app.get("/api/identities")
    def identities():
        return JSONResponse(_envelope(root, _fetch_identities))

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
        and closes — the bounded seam tests and curl use."""
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
            last_source_state = SOURCE_OK
            while True:
                if await request.is_disconnected():
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
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    class _NoCacheStatic(StaticFiles):
        async def get_response(self, path, scope):  # pragma: no cover - thin
            resp = await super().get_response(path, scope)
            # Deterministic freshness after a redeploy: revalidate always
            # (heuristic freshness could serve a stale app.js — gauntlet).
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/", _NoCacheStatic(directory=str(UI_DIR), html=True),
              name="ui")
    return app
