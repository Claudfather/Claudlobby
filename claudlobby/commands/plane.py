"""claudlobby emit / claudlobby plane — the kernel's CLI surface.

Failure taxonomy is CENTRAL, not per-command: every plane door maps
ContractViolation -> 2, SpoolWriteError -> 3, DowngradeError -> 4 through the
same guard, so a wrong-shape request or a newer db exits by contract instead
of escaping as a traceback from whichever command happened to touch it.
"""

from __future__ import annotations

import json
import os
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
    SpoolWriteError, drain, oldest_spooled_at, quarantine_dir,
    quarantine_entry, scan_spool, spool_dir, spool_entries,
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
        # scan_spool — THE shared spool definition (external round 4: this
        # command printed 'spool: 0 pending' for a tree /api/trust called
        # unreadable; a numeric zero from an unenumerable dir is the lie).
        sc = scan_spool(root)
        if sc.spool_state == "unreadable":
            print("spool: unreadable — cannot count (a gap, not a zero)")
        else:
            oldest_at = oldest_spooled_at(sc.pending)
            oldest = ""
            if oldest_at:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(oldest_at))
                oldest = f", oldest {int(age.total_seconds())}s"
            print(f"spool: {len(sc.pending)} pending{oldest}")
        if sc.quarantine_state == "unreadable":
            print("quarantine: unreadable — cannot count")
        else:
            print(f"quarantine: {len(sc.quarantined)}")
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
        # Daemon rung (PR-B T9): three-state, evidence-based — never assume a
        # daemon SHOULD run. Serving = ok. Never-started + no socket = ok
        # (unarmed; doors fall back to the cold CLI by design). Started
        # historically but not serving = ATTENTION with the corrective command
        # (§17 direction: symptom -> exact command).
        from ..plane.daemon import probe_daemon, socket_path

        # Honor PLANE_SOCKET like the shim does (gauntlet round): doctor used
        # to probe only the default path, so an overridden-socket fleet read
        # "not serving" while every door happily used rung 1 — and the doctor
        # test could never reach the serving branch against a live fixture.
        sock = Path(os.environ["PLANE_SOCKET"]) if os.environ.get("PLANE_SOCKET") \
            else socket_path(root)
        serving = sock.exists() and probe_daemon(sock)
        started = 0
        last_ingest = None
        if path.exists():
            conn = connect(path)
            try:
                started = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='system'"
                    " AND event='daemon_started'").fetchone()[0]
                last_ingest = conn.execute(
                    "SELECT MAX(ingested_at) FROM ingest_ledger").fetchone()[0]
            except Exception:  # noqa: BLE001 — a pre-plane db has no tables
                pass
            finally:
                conn.close()
        if serving:
            rung(True, "daemon", f"serving on {sock}")
        elif started:
            rung(False, "daemon",
                 f"started {started}x historically but not serving — check:"
                 " systemctl --user status claudlobby-plane-daemon.service"
                 " (macOS: launchctl print gui/$UID/claudlobby-plane-daemon);"
                 " doors are falling back to the cold CLI meanwhile")
        else:
            rung(True, "daemon", "never armed (doors fall back to cold CLI)")
        rung(True, "last ingest", str(last_ingest or "none yet"))
        # scan_spool — the same shared definition the trust panel and
        # status consume; an unreadable enumeration is a FAILING rung and a
        # nonzero exit, never a green zero (external round 4, probed).
        sc = scan_spool(root)
        if sc.spool_state == "unreadable":
            rung(False, "spool depth",
                 "UNREADABLE — cannot enumerate (a gap, not a zero)")
            rung(False, "inflight claims", "unreadable")
        else:
            oldest_at = oldest_spooled_at(sc.pending)
            rung(not sc.pending, "spool depth",
                 f"{len(sc.pending)} pending"
                 + (f", oldest {oldest_at}" if oldest_at else ""))
            rung(not sc.inflight, "inflight claims", str(len(sc.inflight)))
        if sc.quarantine_state == "unreadable":
            rung(False, "quarantine",
                 "UNREADABLE — cannot enumerate (a gap, not a zero)")
        else:
            rung(not sc.quarantined, "quarantine", str(len(sc.quarantined)))
        # Phase 2b: provisional actors — lazily-minted identities the
        # registry has not yet observed. INFORMATIONAL (always-passing): a
        # pre-generate estate is legitimately provisional; the number is
        # here so its drop-to-zero after the first armed generate is
        # visible, and a REGROWTH after confirmation is worth a look.
        try:
            prov = conn2 = None
            import sqlite3 as _sq
            _db = Path(root) / "state" / "plane" / "plane.db"  # pure join —
            # never db.db_path(), whose mkdir side effect a read path must
            # not carry (#1387 gauntlet)
            if _db.is_file():
                conn2 = _sq.connect(f"file:{_db}?mode=ro", uri=True)
                prov = conn2.execute(
                    "SELECT COUNT(*) FROM identity_registry"
                    " WHERE provisional = 1").fetchone()[0]
                conn2.close()
            rung(True, "provisional identities",
                 str(prov) if prov is not None else "no db yet")
        except _sq.Error:
            if conn2 is not None:
                conn2.close()
            rung(True, "provisional identities", "unreadable")
        return 0 if failing == 0 else 1

    return _guarded("plane doctor", run)


def cmd_plane_view(args) -> int:
    """Run the Phase-4 operator-plane view daemon in the foreground (same
    supervision posture as serve: systemd/launchd own backgrounding). Binds
    LOCALHOST by default — Tailscale Serve fronts it per the design walk;
    --host is the raw-bind dev fallback."""
    root = _resolve_paths(args).root
    try:
        from ..plane.view import create_app
        import uvicorn
    except (ImportError, RuntimeError) as exc:
        print(
            "plane view: the UI needs the optional [plane-ui] extra — "
            "install with: pip install -e '.[plane-ui]'"
            f" ({exc})", file=sys.stderr)
        return 1
    app = create_app(root)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_plane_open(args) -> int:
    """Print (and best-effort launch) the operator plane URL (§17 golden
    path). Prefers the Tailscale Serve HTTPS URL when Serve fronts the port;
    falls back to the local bind."""
    import shutil as _shutil
    import subprocess as _subprocess

    url = f"http://127.0.0.1:{args.port}/"
    ts = _shutil.which("tailscale") or (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if Path("/Applications/Tailscale.app").exists() else None)
    if ts:
        try:
            out = _subprocess.run(  # noqa: S603 - fixed argv
                [ts, "serve", "status"], capture_output=True, text=True,
                timeout=5).stdout
            # Adopt the https URL ONLY from the block that proxies OUR port
            # (gauntlet, probed: the first-https match opened someone
            # else's service the moment Serve fronted a second app).
            current = None
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("https://"):
                    current = stripped.split()[0]
                elif current and f"127.0.0.1:{args.port}" in stripped:
                    url = current
                    break
                elif stripped.startswith("http://") or not stripped:
                    current = current if stripped else None
        except (OSError, _subprocess.SubprocessError):
            pass
    print(url)
    opener = _shutil.which("open") or _shutil.which("xdg-open")
    if opener and not getattr(args, "no_browser", False):
        _subprocess.Popen([opener, url],  # noqa: S603 - fixed argv
                          stdout=_subprocess.DEVNULL,
                          stderr=_subprocess.DEVNULL)
    return 0


def cmd_plane_serve(args) -> int:
    """Run the ingest daemon in the foreground (supervision owns backgrounding
    — systemd Restart=always / launchd KeepAlive, never a self-fork)."""
    root = _resolve_paths(args).root

    def run() -> int:
        from ..plane.daemon import (
            DaemonAlreadyRunning, PlaneDaemon, SocketOverrideInvalid,
            SocketPathTooLong,
        )

        try:
            PlaneDaemon(
                root,
                socket_override=Path(args.socket) if args.socket else None,
                drain_interval=float(args.drain_interval),
            ).serve()
        except (DaemonAlreadyRunning, SocketOverrideInvalid,
                SocketPathTooLong) as exc:
            print(f"plane serve: REFUSED — {exc}", file=sys.stderr)
            return 1
        return 0

    return _guarded("plane serve", run)


def cmd_plane_schema(args) -> int:
    print(json.dumps(export_schemas(), indent=2, sort_keys=True))
    return 0
