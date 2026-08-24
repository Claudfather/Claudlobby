"""claudlobby emit / claudlobby plane — the kernel's CLI surface.

Failure taxonomy is CENTRAL, not per-command: every plane door maps
ContractViolation -> 2, SpoolWriteError -> 3, DowngradeError -> 4 through the
same guard, so a wrong-shape request or a newer db exits by contract instead
of escaping as a traceback from whichever command happened to touch it.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._helpers import _resolve_paths
from ..plane.contracts import ContractViolation, export_schemas
from ..plane.db import connect, db_path
from ..plane.emit_api import emit, emit_batch, _load_capture_config
from ..plane.identity import provisional_actors
from ..plane.ids import ensure_host_uid
from ..plane.migrations import DowngradeError, SCHEMA_USER_VERSION, migrate
from ..plane.spool import (
    SpoolWriteError, drain, quarantine_dir, quarantine_entry, spool_dir,
    spool_entries,
)

_FAMILY_COUNTS = {
    "communication": ("communications", None),
    "transmission": ("events", "transmission"),
    "work_item": ("work_items", None),
    "assignment": ("assignments", None),
    "task": ("events", "task"),
}

_SPOOL_NAME_RE = re.compile(r"ev_[0-9a-f]{32}\.json")


def _guarded(label: str, fn) -> int:
    """THE exception-to-exit mapping (one copy). DowngradeError is caught for
    every door — plane status and spool retry run migrate() too, and a newer
    db must refuse at 4 from any of them, never traceback at 1."""
    try:
        return fn()
    except ContractViolation as exc:
        errors = getattr(exc, "errors", None)
        first = errors[0] if errors else str(exc)
        print(f"{label}: contract violation: {first}", file=sys.stderr)
        return 2
    except SpoolWriteError as exc:
        print(f"{label}: TOTAL FAILURE — {exc}", file=sys.stderr)
        return 3
    except DowngradeError as exc:
        # Never spooled (round-2 F6): a newer db is an operator condition,
        # not transient infrastructure — retrying it forever helps no one.
        print(f"{label}: REFUSED — {exc}", file=sys.stderr)
        return 4


def _require_object(obj, where: str) -> dict:
    """Valid JSON is not yet a valid request: [] / null / 42 / "x" used to
    escape as TypeError tracebacks past the JSONDecodeError catch."""
    if not isinstance(obj, dict):
        raise ContractViolation(
            [{"loc": (where,),
              "msg": f"request must be a JSON object, got {type(obj).__name__}"}]
        )
    return obj


def cmd_emit(args) -> int:
    root = _resolve_paths(args).root
    try:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
        request = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"emit: unreadable request: {exc}", file=sys.stderr)
        return 2

    def run() -> int:
        req = _require_object(request, "request")
        req["event_type"] = args.event_type
        outcome = emit(root, req)
        print(outcome.event_id)
        if outcome.status == "spooled":
            print(f"plane: db unavailable — spooled {outcome.detail}", file=sys.stderr)
        return 0

    return _guarded("emit", run)


def cmd_emit_batch(args) -> int:
    """One atomic unit of work: {"events": [...]} or a bare JSON array (F4)."""
    root = _resolve_paths(args).root
    try:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
        parsed = json.loads(raw)
        requests = parsed["events"] if isinstance(parsed, dict) else parsed
        assert isinstance(requests, list) and requests
    except (OSError, json.JSONDecodeError, KeyError, AssertionError) as exc:
        print(f"emit-batch: unreadable request: {exc}", file=sys.stderr)
        return 2

    def run() -> int:
        members = [
            _require_object(r, f"events[{i}]") for i, r in enumerate(requests)
        ]
        outcomes = emit_batch(root, members)
        for o in outcomes:
            print(o.event_id)
        if outcomes and outcomes[0].status == "spooled":
            print(f"plane: db unavailable — spooled {outcomes[0].detail}", file=sys.stderr)
        return 0

    return _guarded("emit-batch", run)


def _oldest_spooled_at(entries: list[dict]) -> str | None:
    """min over PARSED spooled_at — filenames are random event ids, so
    filename order says nothing about age."""
    stamps = []
    for e in entries:
        raw = e.get("spooled_at")
        if not raw:
            continue
        try:
            stamps.append(datetime.fromisoformat(raw))
        except ValueError:
            continue
    return min(stamps).isoformat() if stamps else None


def cmd_plane_status(args) -> int:
    root = _resolve_paths(args).root

    def run() -> int:
        path = db_path(root)
        print(f"db: {path} ({'present' if path.exists() else 'absent'})")
        if path.exists():
            conn = connect(path)
            try:
                migrate(conn)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                print(f"schema user_version: {version}")
                top = conn.execute(
                    "SELECT COALESCE(MAX(ingest_seq), 0) FROM ingest_ledger"
                ).fetchone()[0]
                print(f"ingest_seq high-water: {top}")
                for family, (table, kind) in _FAMILY_COUNTS.items():
                    if kind is None:
                        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    else:
                        n = conn.execute(
                            "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)
                        ).fetchone()[0]
                    print(f"  {family}: {n}")
                prov = provisional_actors(conn)
                print(f"provisional actors: {len(prov)}")
            finally:
                conn.close()
        entries = spool_entries(root)
        oldest_at = _oldest_spooled_at(entries)
        oldest = ""
        if oldest_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(oldest_at)
            oldest = f", oldest {int(age.total_seconds())}s"
        print(f"spool: {len(entries)} pending{oldest}")
        print(f"quarantine: {len(list(quarantine_dir(root).glob('*.json')))}")
        return 0

    return _guarded("plane status", run)


def cmd_plane_spool(args) -> int:
    root = _resolve_paths(args).root

    def run() -> int:
        if args.spool_action == "list":
            for e in spool_entries(root):
                print(
                    f"{e['_file']}  events={e.get('event_ids')}"
                    f"  attempts={e.get('attempts')}"
                )
            return 0
        if args.spool_action == "inspect":
            if not _SPOOL_NAME_RE.fullmatch(args.name or ""):
                print(f"invalid spool entry name: {args.name!r}", file=sys.stderr)
                return 1
            src = spool_dir(root) / args.name
            if not src.exists():
                src = quarantine_dir(root) / args.name
                if not src.exists():
                    print(f"no such spool entry: {args.name}", file=sys.stderr)
                    return 1
                reason = src.with_name(src.name + ".reason")
                if reason.exists():
                    print(f"quarantined: {reason.read_text().strip()}", file=sys.stderr)
            try:
                entry = json.loads(src.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                print(f"unreadable spool entry: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(entry, indent=2, sort_keys=True, default=str))
            return 0
        if args.spool_action == "retry":
            conn = connect(db_path(root))
            try:
                migrate(conn)
                host = ensure_host_uid(root / "state")
                report = drain(root, conn, host)
            finally:
                conn.close()
            print(
                f"ingested={report.ingested} duplicates={report.duplicates}"
                f" quarantined={report.quarantined} remaining={report.remaining}"
            )
            return 0
        if args.spool_action == "quarantine":
            if not _SPOOL_NAME_RE.fullmatch(args.name or ""):
                # Round-2 F9: the name is a filesystem operand — only validated
                # spool basenames, never path components.
                print(f"invalid spool entry name: {args.name!r}", file=sys.stderr)
                return 1
            src = spool_dir(root) / args.name
            if not src.exists():
                print(f"no such spool entry: {args.name}", file=sys.stderr)
                return 1
            quarantine_entry(root, src, "operator")
            print(f"quarantined {args.name}")
            return 0
        return 1

    return _guarded("plane spool", run)


def cmd_plane_doctor(args) -> int:
    """Kernel-scoped health rungs (§10/§17 — the golden-path doctor grows in
    Phase 2; these are the checks the kernel alone can answer). Exit 0 when
    every rung passes, 1 when any needs attention; version refusals still
    exit 4 through the guard."""
    root = _resolve_paths(args).root

    def run() -> int:
        failing = 0

        def rung(ok: bool, label: str, detail: str = "") -> None:
            nonlocal failing
            mark = "ok" if ok else "ATTENTION"
            suffix = f" — {detail}" if detail else ""
            print(f"[{mark}] {label}{suffix}")
            if not ok:
                failing += 1

        path = db_path(root)
        if not path.exists():
            rung(True, "db", f"absent (not yet used): {path}")
        else:
            conn = connect(path)
            try:
                migrate(conn)   # DowngradeError -> 4 via the guard
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                rung(version == SCHEMA_USER_VERSION, "schema",
                     f"user_version {version} (code supports {SCHEMA_USER_VERSION})")
                prov = provisional_actors(conn)
                rung(True, "provisional actors", str(len(prov)))
            finally:
                conn.close()
        try:
            _load_capture_config(root)
            rung(True, "capture config", "valid or absent (default: metadata)")
        except ContractViolation as exc:
            errors = getattr(exc, "errors", None)
            rung(False, "capture config", str(errors[0] if errors else exc))
        entries = spool_entries(root)
        oldest_at = _oldest_spooled_at(entries)
        rung(not entries, "spool depth",
             f"{len(entries)} pending" + (f", oldest {oldest_at}" if oldest_at else ""))
        inflight = len(list(spool_dir(root).glob("*.json.inflight.*")))
        rung(inflight == 0, "inflight claims", str(inflight))
        quarantined = len(list(quarantine_dir(root).glob("*.json")))
        rung(quarantined == 0, "quarantine", str(quarantined))
        return 0 if failing == 0 else 1

    return _guarded("plane doctor", run)


def cmd_plane_serve(args) -> int:
    """Run the ingest daemon in the foreground (supervision owns backgrounding
    — systemd Restart=always / launchd KeepAlive, never a self-fork)."""
    root = _resolve_paths(args).root

    def run() -> int:
        from ..plane.daemon import (
            DaemonAlreadyRunning, PlaneDaemon, SocketPathTooLong,
        )

        try:
            PlaneDaemon(
                root,
                socket_override=Path(args.socket) if args.socket else None,
                drain_interval=float(args.drain_interval),
            ).serve()
        except DaemonAlreadyRunning as exc:
            print(f"plane serve: REFUSED — {exc}", file=sys.stderr)
            return 1
        except SocketPathTooLong as exc:
            print(f"plane serve: REFUSED — {exc}", file=sys.stderr)
            return 1
        return 0

    return _guarded("plane serve", run)


def cmd_plane_schema(args) -> int:
    print(json.dumps(export_schemas(), indent=2, sort_keys=True))
    return 0
