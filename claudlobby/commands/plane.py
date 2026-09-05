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
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._helpers import _load_fleet_or_exit, _resolve_paths
from ..plane.contracts import ContractViolation, export_schemas
from ..plane.db import connect, db_file, db_path, open_ro
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
    paths = _resolve_paths(args)
    root = paths.root

    def run() -> int:
        failing = 0

        def rung(ok: bool, label: str, detail: str = "") -> None:
            nonlocal failing
            mark = "ok" if ok else "ATTENTION"
            suffix = f" — {detail}" if detail else ""
            print(f"[{mark}] {label}{suffix}")
            if not ok:
                failing += 1

        path = db_file(root)
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
                # Registry-lane trust (chunk B): tombstones the F11 join
                # does not validate mean a scan died between its tombstones
                # and its completion — the reader already ignores them; the
                # rung surfaces that they exist. Last-scan freshness rides
                # the same rung set; "no scan yet" is dormancy, not fault.
                # The catch is NARROW and FAILS THE RUNG — never green
                # (round 2 deleted a blanket except that rendered defects
                # as a passing "pre-0006" diagnosis), and never a crash
                # (r3, probed: one malformed detail row killed the whole
                # doctor, taking the daemon/spool rungs an operator needs
                # most when the db is sick — unreachable ≠ empty ≠ dead
                # instrument).
                from ..plane import registry_read as _rr
                try:
                    inv = _rr.invalid_tombstones(conn)
                    rung(not inv, "tombstone validity (F11)",
                         f"{len(inv)} unvalidated"
                         + (f" — newest scan {inv[0]['scan_id']}" if inv
                            else ""))
                    ls = _rr.last_scan(conn)
                    if ls is None:
                        rung(True, "registry scan", "none yet (lane"
                             " dormant or first generate pending)")
                    else:
                        rung(bool(ls.get("complete")), "registry scan",
                             f"{ls['occurred_at']} scope={ls.get('scope')}"
                             + ("" if ls.get("complete")
                                else " — INCOMPLETE (tombstones from it"
                                     " are not honored)"))
                except (sqlite3.Error, ValueError) as exc:
                    rung(False, "registry lane", f"unreadable: {exc}")
                # Reconcile-check rung (chunk: doctor IOUs — closes the
                # chunk-B disclosure that RECONCILIATION_SQL was bench-only).
                # Counts submitted-but-not-acked transmissions. This is
                # INFORMATIONAL, never a failure: §6b rules the tmux carrier
                # yields no recipient_acknowledged at all, so a nonzero count
                # is the EXPECTED steady state, not a fault — a pass/fail
                # gate here would alarm on every tmux dispatch forever.
                try:
                    from ..plane.queries import RECONCILIATION_SQL
                    unacked = conn.execute(RECONCILIATION_SQL).fetchone()[0]
                    # RECONCILIATION_SQL filters pane_submitted, which is
                    # TMUX-ONLY (contracts): telegram emits carrier_accepted
                    # and is NOT counted here, so this rung sees only unacked
                    # tmux — expected, never a fault (§6b). No telegram claim
                    # (gauntlet: the SQL cannot deliver it).
                    rung(True, "reconcile (tmux submitted-not-acked)",
                         f"{unacked} — expected: the tmux carrier yields no"
                         " ack, so this is the steady state, not a gap")
                except sqlite3.Error as exc:
                    rung(False, "reconcile", f"unreadable: {exc}")
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
        # Composed-hash-drift rung (chunk: doctor IOUs — closes the chunk-B
        # disclosure that the --verify capability existed but doctor never
        # surfaced it). Doctor SURFACES the check; it does NOT re-run it.
        # The real drift check re-derives + re-hashes the WHOLE estate,
        # which is (a) too heavy for a per-invocation health command and
        # (b) needs the host-uid — and an earlier version of this rung
        # MINTED it on absence, reintroducing the #1429 verify BLOCKER (a
        # read-only health command leaving a write behind, phantom drift).
        # Both problems vanish by pointing at the door that owns the check.
        rung(True, "composed-hash drift",
             "run `claudlobby --fleet <name> plane registry --verify` —"
             " the read-only estate-vs-scan check (doctor stays lightweight;"
             " re-derivation is that door's job)")
        return 0 if failing == 0 else 1

    return _guarded("plane doctor", run)


def cmd_plane_registry(args) -> int:
    """The registry lane's read door (chunk B): current state, SCD history,
    field-level changes, and --verify (projection vs re-derived estate).
    Every answer is F11-validated — a tombstone counts only when its scan's
    completion says complete=true — because queries.py's shared CTE is the
    single place that rule lives."""
    paths = _resolve_paths(args)
    root = paths.root

    def run() -> int:
        from ..plane import registry_read as rr

        path = db_file(root)
        if not path.exists():
            print(f"registry: no plane db at {path} — no scan has run here"
                  " (arm PLANE_EMIT_ENABLED=1 in the fleet-tier .env and"
                  " run generate)", file=sys.stderr)
            return 1
        conn = connect(path)
        try:
            migrate(conn)
        except Exception:
            conn.close()
            raise
        try:
            if args.verify:
                # --verify re-derives the estate through the fleet config —
                # root-mode fleet.yaml or the global --fleet overlay;
                # _load_fleet_or_exit owns that resolution and its errors
                from ._helpers import _load_fleet_or_exit
                from ..plane.registry_emit import (
                    _vault_rev, assemble_entities)
                fleet, _ = _load_fleet_or_exit(paths)
                # READ the host identity the ingest path recorded — never
                # mint. ensure_host_uid(root) here minted a FRESH uid at
                # the wrong path, scoping the projection to nothing: a
                # healthy estate read as 100% phantom drift, and a read
                # door left a write behind (r3 BLOCKER, probed; the pin
                # drives THIS door, not the API — the rehearse-env-cascade
                # lesson).
                uid_file = root / "state" / "host-uid"
                try:
                    this_host = uid_file.read_text().strip()
                except FileNotFoundError:
                    this_host = ""
                except OSError as exc:
                    # unreachable ≠ absent (r4): a perms failure must not
                    # read as "no scan yet" — opposite remedies
                    print(f"registry --verify: host identity UNREADABLE"
                          f" at {uid_file} ({exc})", file=sys.stderr)
                    return 1
                if not this_host:
                    print(f"registry --verify: no host identity at"
                          f" {uid_file} — no scan has recorded here yet",
                          file=sys.stderr)
                    return 1
                assembled, complete = assemble_entities(
                    paths, fleet, _vault_rev(paths))
                rep = rr.verify_current(conn, assembled, fleet=fleet.name,
                                        host_uid=this_host)
                print(f"checked {rep.checked} entities"
                      + ("" if complete else
                         "  [enumeration INCOMPLETE — drift below is"
                         " partial evidence]"))
                for label, keys in (("DRIFT", rep.drifted),
                                    ("missing from db", rep.missing_from_db),
                                    ("missing from estate",
                                     rep.missing_from_estate)):
                    for etype, alias in keys:
                        print(f"  [{label}] {etype} {alias}")
                if rep.ok:
                    print("projection matches the estate")
                return 0 if rep.ok else 1
            if args.history:
                rows = rr.entity_history(conn, args.history)
                if not rows:
                    print(f"no registry rows for {args.history!r}",
                          file=sys.stderr)
                    return 1
                for r in rows:
                    state = "TOMBSTONE" if r["tombstone"] else \
                        (r["payload_hash"] or "")[:12]
                    until = r["valid_to"] or "now"
                    print(f"{r['valid_from']} -> {until}  {state}"
                          f"  cause={r['cause']} scan={r['scan_id']}")
                return 0
            if args.changes is not None:
                changes = rr.recent_changes(conn, limit=args.changes)
                if not changes:
                    print("no registry changes recorded yet",
                          file=sys.stderr)
                    return 0
                for c in changes:
                    print(f"{c['occurred_at']}  {c['entity_type']}"
                          f" {c['entity_alias']}  {c['change']}")
                    for fld, (old, new) in sorted(c["fields"].items()):
                        print(f"    {fld}: {old!r} -> {new!r}")
                return 0
            if args.show:
                rows = [r for r in rr.current_entities(conn)
                        if r["entity_alias"] == args.show
                        or r["entity_uid"] == args.show]
                if not rows:
                    print(f"{args.show!r} is not in the current registry"
                          " (deleted, never scanned, or a typo — try"
                          " --history)", file=sys.stderr)
                    return 1
                for r in rows:
                    print(json.dumps(
                        {k: r[k] for k in ("entity_type", "entity_alias",
                                           "entity_uid", "payload",
                                           "payload_hash", "cause",
                                           "scan_id", "occurred_at")},
                        indent=2, ensure_ascii=False))
                return 0
            rows = rr.current_entities(conn, entity_type=args.type,
                                       fleet=args.scope_fleet)
            # the trust line PRECEDES the empty early-return: one unhonored
            # tombstone deleting your only entity must not read as silence
            # (gauntlet, probed)
            inv = rr.invalid_tombstones(conn)
            if inv:
                print(f"[trust] {len(inv)} tombstone(s) NOT honored —"
                      " no complete same-scan_id scan_completed"
                      " (run plane doctor)", file=sys.stderr)
            if not rows:
                print("registry is empty for this filter (no completed"
                      " scan, or nothing matches)", file=sys.stderr)
                return 0
            for r in rows:
                print(f"{r['entity_type']:13} {r['entity_alias']:44}"
                      f" {(r['payload_hash'] or '')[:12]}"
                      f"  {r['occurred_at']}")
            return 0
        except (sqlite3.Error, ValueError) as exc:
            # a read door must not traceback on one corrupt row (r3,
            # probed: malformed declaration detail killed the whole
            # command) — refuse loudly instead, rc 1
            print(f"registry unreadable: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()

    return _guarded("plane registry", run)


def cmd_plane_prune(args) -> int:
    """Age out raw metric_samples past the retention window (chunk 3a;
    spec §F20: 30-day raws, the incident-join window). Family-scoped — the
    ONLY DELETE the plane performs, and it never touches the ledger (the
    dedupe horizon). Runs from a composed timer, NOT the ingest-only
    daemon. `--dry-run` reports the count without deleting."""
    root = _resolve_paths(args).root

    def run() -> int:
        from ..plane.retention import (
            DEFAULT_RETENTION_DAYS, prune_metric_samples)

        path = db_file(root)
        if not path.exists():
            print(f"prune: no plane db at {path} — nothing to age out",
                  file=sys.stderr)
            return 0
        days = args.days if args.days is not None else DEFAULT_RETENTION_DAYS
        if days < 0:
            # a negative window's future cutoff would delete EVERYTHING — a
            # clean contract refusal (rc 2), never a raw traceback (gauntlet)
            raise ContractViolation(
                [{"loc": ("days",), "msg": "retention days cannot be"
                  " negative (a future cutoff would delete all samples)"}])
        conn = connect(path)
        try:
            migrate(conn)   # DowngradeError -> 4 via the guard
            res = prune_metric_samples(conn, days=days,
                                       dry_run=args.dry_run)
        finally:
            conn.close()
        verb = "would delete" if res.dry_run else "deleted"
        print(f"metric_samples: {verb} {res.candidates if res.dry_run else res.deleted}"
              f" rows older than {days}d (cutoff {res.cutoff})")
        return 0

    return _guarded("plane prune", run)


def cmd_plane_expire(args) -> int:
    """Attention expiry sweep: emit a terminal `expired` task event for
    every assignment whose deadline passed more than the horizon ago and
    that nothing has closed — so the attention queue shows what needs the
    operator NOW, not last Tuesday. A Lane-B fact through normal ingest,
    idempotent by construction (already-terminal rows are excluded). Runs
    from a dormant timer, never the ingest daemon. `--dry-run` reports."""
    root = _resolve_paths(args).root

    def run() -> int:
        from ..plane.expiry import (
            DEFAULT_AFTER_DAYS, expirable, expired_events)
        from ..plane.emit_api import emit_batch

        path = db_file(root)
        if not path.exists():
            print(f"expire: no plane db at {path} — nothing to sweep",
                  file=sys.stderr)
            return 0
        days = args.after_days if args.after_days is not None \
            else DEFAULT_AFTER_DAYS
        if days < 0:
            raise ContractViolation(
                [{"loc": ("after_days",), "msg": "expiry horizon cannot be"
                  " negative"}])
        conn = connect(path)
        try:
            migrate(conn)
            plan = expirable(conn, after_days=days)
        finally:
            conn.close()
        for aid in plan.unattributed:
            print(f"expire: skipped {aid} — no fleet attribution (never"
                  " emitted under a fabricated fleet)", file=sys.stderr)
        if args.dry_run or not plan.rows:
            print(f"attention: {'would expire' if args.dry_run else 'expired'}"
                  f" {len(plan.rows)} assignment(s) overdue >{days}d"
                  f" (cutoff {plan.cutoff})")
            return 0
        emit_batch(root, expired_events(plan, after_days=days))
        print(f"attention: expired {len(plan.rows)} assignment(s) overdue"
              f" >{days}d (cutoff {plan.cutoff})")
        return 0

    return _guarded("plane expire", run)


def cmd_plane_view(args) -> int:
    """Run the Phase-4 operator-plane view daemon in the foreground (same
    supervision posture as serve: systemd/launchd own backgrounding). Binds
    LOCALHOST by default — Tailscale Serve fronts it per the design walk;
    --host is the raw-bind dev fallback."""
    root = _resolve_paths(args).root
    try:
        from ..plane.view import begin_shutdown, create_app
        import uvicorn
    except (ImportError, RuntimeError) as exc:
        print(
            "plane view: the UI needs the optional [plane-ui] extra — "
            "install with: pip install -e '.[plane-ui]'"
            f" ({exc})", file=sys.stderr)
        return 1
    app = create_app(root)

    class _ViewServer(uvicorn.Server):
        """Stops when asked. A held SSE connection kept the daemon alive
        through SIGTERM until a SIGKILL (chunk L, #1479 — measured: still
        running 20s after the signal with one `/api/stream` client attached;
        uvicorn waits on in-flight requests with no bound by default).

        The signal is where the streams have to hear it: uvicorn sends the
        lifespan shutdown only AFTER its graceful wait, so nothing inside the
        app can release the very requests that wait is waiting on. This hook
        runs first, the streams end their own responses, and the process
        exits without cancelling anything (measured: 5.18s and one
        CancelledError traceback before, 0.26s and none after)."""

        def handle_exit(self, sig, frame):   # pragma: no cover - signal path
            begin_shutdown(app)
            super().handle_exit(sig, frame)

    # The ceiling stays as the backstop for a stream that does NOT end itself
    # (a wedged read). Keep it under launchd's 20s default stop timeout —
    # systemd's is 90s — or the supervisor's SIGKILL is what stops the daemon.
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level="warning", timeout_graceful_shutdown=5)
    _ViewServer(config).run()
    # Unreachable under SIGTERM: uvicorn re-raises the captured signal on the
    # way out, so the process dies with rc -15 rather than returning here.
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
