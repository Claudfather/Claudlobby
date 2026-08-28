"""The Phase-4 operator plane — `claudlobby plane view` (design walk 2026-08-28).

The v1 UI's API daemon: read-only over the plane db, serving the static UI and
seven GET surfaces. Rulings it implements (documentation/plans — Phase-4 walk):

* **Strictly read-only, structurally**: every connection opens `mode=ro` with
  `PRAGMA query_only`; the app exposes no non-GET route (pinned by test). The
  page can observe everything and touch nothing — Telegram/RC stay the fleet's
  write channels until Phase 6.
* **Panel states are SERVER-side facts** (§16: never render zero when the
  source is absent): every endpoint returns an envelope
  ``{state, provenance, remediation?, data?}`` — the UI renders `state`, it
  never infers health from an empty list. ABSENT (no db yet) and UNREADABLE
  (IO/malformed) are distinct states with distinct remediations, the
  source_state.py philosophy over HTTP.
* **Story-first channel**: /api/channel returns CONVERSATION THREADS — a
  dispatch, its delivery states, its reports, and its task closure grouped by
  work item / reply chain — not a flat ledger. Alias-first names ride along
  (`short` fields); raw ids are payload the UI may reveal, never the headline.
* **SSE, not polling** (Junction 2): /api/stream pushes ledger rows past an
  `ingest_seq` cursor; one row-pump fans out to every viewer (§14's
  never-multiply-backend-work-by-browser-count rule applied to data).

FastAPI/uvicorn live in the OPTIONAL `[plane-ui]` extra (§14 degrade rule) —
this module import-guards them so the core ledger never needs them.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
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

from .db import db_path
from .queries import ATTENTION_SQL, TASK_STATUS_SQL

UI_DIR = Path(__file__).resolve().parent / "ui"
_CHANNEL_LIMIT_MAX = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(alias: str | None) -> str | None:
    """Alias-first presentation (§11): `bot:fleet/name` -> `name`. The full
    alias stays in the payload for expand/hover; the UI leads with this."""
    if not alias:
        return alias
    return alias.rsplit("/", 1)[-1] if "/" in alias else alias


def _ro_conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _provenance(root: Path, conn: sqlite3.Connection | None) -> dict:
    prov = {"db": str(db_path(root)), "checked_at": _now_iso()}
    if conn is not None:
        row = conn.execute(
            "SELECT MAX(ingest_seq) AS seq, MAX(ingested_at) AS at"
            " FROM ingest_ledger"
        ).fetchone()
        prov["last_ingest_seq"] = row["seq"]
        prov["last_ingest_at"] = row["at"]
    return prov


def _envelope(root: Path, fn):
    """Run `fn(conn)` -> data under the panel-state contract. The three
    server-detectable states: absent (no db — the recorder has not written
    yet), unreadable (IO/corruption — remediation differs), ok. An EMPTY
    result is ok-with-empty-data: legitimately-idle is the UI's word for it,
    never the server inventing zeros (§16)."""
    db = db_path(root)
    if not db.exists():
        return {
            "state": "absent",
            "provenance": {"db": str(db), "checked_at": _now_iso()},
            "remediation": "no plane db yet — it appears on the first armed"
                           " emission (or `claudlobby emit`); check"
                           " PLANE_EMIT_ENABLED for the fleet",
        }
    try:
        conn = _ro_conn(db)
    except sqlite3.Error as exc:
        return {
            "state": "unreadable",
            "provenance": {"db": str(db), "checked_at": _now_iso()},
            "remediation": f"db exists but cannot be opened: {exc} — check"
                           " permissions; `claudlobby plane doctor`",
        }
    try:
        data = fn(conn)
        return {"state": "ok", "provenance": _provenance(root, conn),
                "data": data}
    except sqlite3.Error as exc:
        return {
            "state": "unreadable",
            "provenance": {"db": str(db), "checked_at": _now_iso()},
            "remediation": f"query failed: {exc} — schema drift? run"
                           " `claudlobby plane doctor`",
        }
    finally:
        conn.close()


def _channel_names(root: Path) -> dict:
    """Optional operator map state/plane/channels.json: raw carrier address
    (Telegram chat id) -> human name. Absent file = empty map; the UI then
    renders a generic carrier label, never the raw id (feedback ruling)."""
    p = Path(root) / "state" / "plane" / "channels.json"
    try:
        loaded = json.loads(p.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------
# Surface queries (all read-only; consume queries.py where it answers)
# --------------------------------------------------------------------------

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
        tx_by_msg.setdefault(t["msg_id"], []).append(t)

    # Thread key: work_item, else the root of the reply chain, else itself.
    reply_to = {c["msg_id"]: c["reply_to_msg_id"] for c in comms}

    def chain_root(mid: str) -> str:
        seen = set()
        while reply_to.get(mid) and mid not in seen:
            seen.add(mid)
            mid = reply_to[mid]
        return mid

    threads: dict = {}
    for c in comms:
        key = c["work_item_id"] or f"chain:{chain_root(c['msg_id'])}"
        t = threads.setdefault(key, {"key": key, "work_item_id":
                                     c["work_item_id"], "messages": []})
        c["sender_short"] = _short(c["sender_alias"])
        c["recipient_short"] = _short(c["recipient_alias"])
        if not c["recipient_alias"] and c["recipient_raw"]:
            c["recipient_short"] = names.get(
                c["recipient_raw"], "Telegram")
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
        t["title"] = (wi or {}).get("title")
        t["repo"] = (wi or {}).get("repo")
        t["task_events"] = task_events_by_wi.get(t["work_item_id"], [])
        t["latest_seq"] = max(
            [m["ingest_seq"] for m in t["messages"]]
            + [e["ingest_seq"] for e in t["task_events"]])
        out.append(t)
    out.sort(key=lambda t: t["latest_seq"], reverse=True)
    return {"threads": out}


def _fetch_tasks(conn: sqlite3.Connection) -> dict:
    status = {r["assignment_id"]: r["status"]
              for r in conn.execute(TASK_STATUS_SQL).fetchall()}
    attention = {r[0] for r in
                 conn.execute(ATTENTION_SQL, (_now_iso(),)).fetchall()}
    rows = []
    for r in conn.execute(
        "SELECT a.assignment_id, a.work_item_id, a.assignee_uid,"
        " a.expected_by, a.occurred_at, w.title,"
        " (SELECT alias FROM identity_registry i"
        "   WHERE i.uid = a.assignee_uid) AS assignee_alias"
        " FROM assignments a LEFT JOIN work_items w"
        "   ON w.work_item_id = a.work_item_id"
        " ORDER BY a.ingest_seq DESC LIMIT 200"):
        row = dict(r)
        row["assignee_short"] = _short(row["assignee_alias"])
        row["status"] = status.get(row["assignment_id"], "created_not_sent")
        row["attention"] = row["assignment_id"] in attention
        rows.append(row)
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


def _fetch_summary(conn: sqlite3.Connection, root: Path) -> dict:
    counts = {}
    for t in ("communications", "work_items", "assignments", "workstreams",
              "events", "identity_registry", "ingest_ledger"):
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    state_dir = db_path(root).parent
    spool = 0
    spool_dir = state_dir / "spool"
    if spool_dir.is_dir():
        spool = sum(1 for f in spool_dir.rglob("*") if f.is_file()
                    and not f.name.startswith("."))
    return {
        "counts": counts,
        "ingest_socket_present": (state_dir / "ingest.sock").exists(),
        "spool_files": spool,
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
        def probe(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            last = conn.execute(
                "SELECT MAX(ingested_at) FROM ingest_ledger").fetchone()[0]
            return {"schema_user_version": version, "last_ingest_at": last,
                    "view_started_at": started_at,
                    "corrective": {
                        "daemon": "launchctl kickstart -k"
                                  " gui/$UID/claudlobby-plane-daemon"
                                  "  (or systemctl --user restart"
                                  " claudlobby-plane-daemon)",
                        "doctor": "claudlobby plane doctor",
                    }}
        env = _envelope(root, probe)
        if env["state"] == "ok":
            env["data"].update(_envelope(
                root, lambda c: _fetch_summary(c, root))["data"])
        return JSONResponse(env, status_code=200 if env["state"] == "ok"
                            else 503)

    @app.get("/api/stream")
    async def stream(request: Request, cursor: int = 0, once: int = 0):
        """SSE: ledger rows past `cursor`. One pump per VIEWER connection but
        each tick is one indexed query; the payload is (seq, family) signals —
        clients refetch the boards they show, so derivation cost stays at
        human scale, not per-row scale. `once=1` emits a single batch/ping and
        closes — the bounded seam tests and curl use (an infinite generator
        wedges TestClient teardown; production clients omit it)."""
        async def gen():
            last = int(cursor)
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                env = _envelope(root, lambda c: [
                    dict(r) for r in c.execute(
                        "SELECT ingest_seq, family, ingested_at"
                        " FROM ingest_ledger WHERE ingest_seq > ?"
                        " ORDER BY ingest_seq LIMIT 500", (last,))])
                if env["state"] == "ok" and env["data"]:
                    rows = env["data"]
                    last = rows[-1]["ingest_seq"]
                    payload = json.dumps({"rows": rows, "cursor": last})
                    yield f"id: {last}\ndata: {payload}\n\n"
                elif env["state"] != "ok":
                    yield f"event: source\ndata: {json.dumps(env)}\n\n"
                else:
                    yield ": ping\n\n"
                if once:
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    return app
