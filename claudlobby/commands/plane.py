"""Plane diagnostics and foreground commands.

Public ingest and maintenance use separate common-result adapters.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ._helpers import _resolve_paths
from ..plane.contracts import ContractViolation, export_schemas
from ..plane.db import connect_ro, db_file, open_ro
from ..plane.emit_api import _load_capture_config, capture_mode, DEFAULT_CAPTURE, emit_batch
from ..plane.identity import provisional_actors
from ..plane.migrations import DowngradeError, SCHEMA_USER_VERSION
from ..plane.schema_state import PendingMigrationError, require_current_schema
from ..plane.spool import SpoolWriteError, oldest_spooled_at, scan_spool

#: #1711. A spooled batch is ACCEPTED and DURABLE but not RECORDED — it is on
#: disk and invisible to every reader until a drain. It needs a code of its own
#: because 0 is a lie about it and the failure codes are a lie in the other
#: direction: nothing was lost and nothing needs retrying.
RC_SPOOLED = 6


def _guarded(label: str, fn) -> int:
    """THE exception-to-exit mapping (one copy). DowngradeError is caught for
    every door. Ordinary readers/writers require explicit migration first;
    they never create or advance the schema while answering a diagnostic."""
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
    except PendingMigrationError as exc:
        print(f"{label}: REFUSED — {exc}", file=sys.stderr)
        return 7
    except DowngradeError as exc:
        # Never spooled (round-2 F6): a newer db is an operator condition,
        # not transient infrastructure — retrying it forever helps no one.
        print(f"{label}: REFUSED — {exc}", file=sys.stderr)
        return 4


def _switch_fleet(paths):
    """The fleet whose switches this run may speak for, or None.

    Optional by the same rule ``doctor --switches`` uses: a host with overlay
    fleets has no root fleet.yaml, and a host-wide plane doctor must still
    print its host rows. What it must NOT do is answer for a fleet nobody
    named — `resolve` marks those rows unknown.
    """
    from ..config import load_fleet

    try:
        fleet, _md = load_fleet(paths.fleet_yaml)
        return fleet
    except Exception:  # noqa: BLE001 — no fleet is a host run, not an error
        return None


def cmd_plane_doctor(args) -> int:
    """Kernel-scoped health rungs (§10/§17 — the golden-path doctor grows in
    Phase 2; these are the checks the kernel alone can answer)."""
    from ..command_result import CommandFailure, CommandOutput, execute
    from ..context import resolve_paths

    rungs: list[dict] = []
    lines: list[str] = []

    def run() -> int:
        paths = resolve_paths(root=getattr(args, "root", None),
                              fleet=getattr(args, "fleet", None),
                              seed=getattr(args, "seed", False))
        root = paths.root
        failing = 0

        def rung(ok: bool, label: str, detail: str = "") -> None:
            nonlocal failing
            mark = "ok" if ok else "ATTENTION"
            suffix = f" — {detail}" if detail else ""
            rungs.append({"name": label, "status": "ok" if ok else "attention",
                          "detail": detail})
            lines.append(f"[{mark}] {label}{suffix}")
            if not ok:
                failing += 1

        path = db_file(root)
        if not path.exists():
            rung(True, "db", f"absent (not yet used): {path}")
        else:
            conn = connect_ro(path)
            try:
                require_current_schema(conn)   # DowngradeError -> 4 via the guard
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
            # Report the mode IN FORCE, not the shipped default. Every other
            # rung here states live state; this one named a constant, so it
            # was informative exactly when the setting did not matter
            # (unconfigured) and uninformative exactly when it did. Resolved
            # through `capture_mode` — the ONE rule the recorder and the view
            # also resolve through — never re-derived here.
            modes = _load_capture_config(root)
            host_mode = capture_mode(modes, None)
            differing = sorted(
                f"{fleet}={mode}"
                for fleet, mode in modes.items()
                if fleet != "*" and mode != host_mode
            )
            if not modes:
                detail = (
                    f'{host_mode} (shipped default) — opt out per fleet or '
                    f'host-wide with {{"*": "metadata"}}'
                )
            elif "*" in modes:
                kind = (
                    "host-wide opt-out"
                    if host_mode != DEFAULT_CAPTURE
                    else "host-wide"
                )
                detail = f"{host_mode} ({kind})"
            else:
                detail = f"{host_mode} (shipped default)"
            if differing:
                detail += (
                    f" · {len(differing)} fleet(s) differ: {', '.join(differing)}"
                )
            rung(True, "capture config", detail)
        except ContractViolation as exc:
            errors = getattr(exc, "errors", None)
            rung(False, "capture config", str(errors[0] if errors else exc))
        # Daemon rung (PR-B T9): three-state, evidence-based — never assume a
        # daemon SHOULD run. Serving = ok. Never-started + no socket = ok
        # (unarmed; doors stage raw input until it can be replayed). Started
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
            conn = connect_ro(path)
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
                 " doors stage raw input for daemon replay meanwhile (pending, not committed)")
        else:
            rung(True, "daemon", "never armed (doors stage raw input for daemon replay)")
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
        # --- the two things this door could not see (#1657) -----------------
        # Both live beside the db as plain files because both are written on the
        # path where the PLANE is what could not be reached — recording them
        # through the plane would make the instrument depend on its subject.
        #
        # UNREADABLE IS A FAILING RUNG, NOT A ZERO, matching the spool rungs
        # directly above: a counter this door cannot enumerate is a gap, and the
        # whole point of #1657's third defect is that a silent maybe read as a
        # clean bill. `plane doctor` printed fourteen green rungs over a live
        # reap and an armed breaker.
        plane_state = root / "state" / "plane"
        losses = plane_state / ".emit-losses"
        if not losses.exists():
            # Absent is legitimately clean: the file is created on first loss.
            rung(True, "emit losses", "none recorded")
        else:
            try:
                rows = [r for r in losses.read_text().splitlines() if r.strip()]
            except OSError as exc:
                rung(False, "emit losses", f"UNREADABLE — {exc} (a gap, not a zero)")
                rows = None
            if rows is not None:
                reaps = [r for r in rows if "\treap\t" in r]
                detail = f"{len(reaps)} reaped emit(s) in the last 24h"
                if reaps:
                    doors = sorted({r.split("\t")[2] for r in reaps if len(r.split("\t")) > 2})
                    detail += (f" — doors: {', '.join(doors[:4])}"
                               f"{' …' if len(doors) > 4 else ''}."
                               " Each is a batch whose commit is UNDETERMINED:"
                               " re-emitting is safe (ingest dedupes on the"
                               " pre-minted event id)")
                rung(not reaps, "emit losses", detail)

        # The breaker's own state. Not a defect in itself — measured on this
        # estate it damps (89% of episodes are a single arming) and nothing is
        # lost, so #1657 closes it as acceptable. But a condition closed as
        # acceptable still has to be VISIBLE, or the closure is the same
        # overstatement it was meant to retire: this door never looked at the
        # marker at all.
        wedged = plane_state / ".socket-wedged"
        if not wedged.exists():
            rung(True, "socket breaker", "not armed")
        else:
            try:
                age = int(time.time() - wedged.stat().st_mtime)
                rung(True, "socket breaker",
                     f"ARMED {age}s ago — doors stage raw input until it"
                     " expires. Pending, not committed; see #1693 for the cause")
            except OSError as exc:
                rung(False, "socket breaker", f"UNREADABLE — {exc}")
        # The WAL (#1905). A reader holding a snapshot keeps the daemon's
        # checkpoint from resetting it: the #1693 canary grew it to 81.8 MB in
        # 60 s behind one held reader, and no surface reported the file's size.
        # Over the ceiling is ATTENTION, and the holder is named from the
        # kernel's lock table where the platform has one, so the answer is a
        # process to go and look at.
        if path.exists():
            from ..plane import wal as _wal
            try:
                wal_bytes = _wal.wal_size(root)
            except OSError as exc:
                rung(False, "wal", f"UNREADABLE — {exc} (a gap, not a zero)")
            else:
                ceiling = _wal.human_bytes(_wal.WAL_CEILING_BYTES)
                if wal_bytes <= _wal.WAL_CEILING_BYTES:
                    rung(True, "wal", f"{_wal.human_bytes(wal_bytes)} (ceiling {ceiling})")
                else:
                    from ..plane.daemon import lock_path
                    holders = _wal.snapshot_holders(
                        root, exclude=_wal.writer_pids(lock_path(sock)))
                    rung(False, "wal", f"{_wal.human_bytes(wal_bytes)}, over the {ceiling}"
                         " ceiling — " + _wal.holders_detail(holders))
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
        # The plane-scoped switch subset — the same registry and the same
        # renderer `claudlobby host doctor` uses, filtered to the plane's own doors.
        # A plane whose daemon, probe, retention or expiry sweep is off is not
        # BROKEN, so this is never a failing rung: it is the answer to "why is
        # the Host card empty / why does nothing expire", which is otherwise a
        # question you can only answer by reading four source files.
        try:
            from .. import switches as _sw
            # The fleet this run was GIVEN, not None (F5). `plane doctor
            # --fleet f` and `claudlobby --fleet f doctor --switches` were
            # answering differently about the same fleet: this door discarded
            # the name and then reported fleet-tier switches as their shipped
            # defaults, which is an assertion about a scope it never read.
            # Without a --fleet the rows say "not read here" rather than
            # inventing one.
            _rows = _sw.resolve(paths, _switch_fleet(paths))
            rung(True, "switches", _sw.summary_line(
                [r for r in _rows if r.switch.plane]))
            lines.append(_sw.format_table(_rows, plane_only=True))
        except Exception as exc:  # noqa: BLE001 — a health command never crashes
            rung(False, "switches", f"unavailable: {exc}")
        return failing

    def operation() -> CommandOutput:
        def refuse(code: str, message: str):
            if not getattr(args, "json", False):
                if lines:
                    print("\n".join(lines))
            raise CommandFailure(code, message,
                                 data={"status": "refused", "rungs": rungs,
                                       "attention_count": sum(row["status"] == "attention" for row in rungs)})

        try:
            failing = run()
        except PendingMigrationError:
            refuse("migration_required", "plane doctor: explicit Plane migration is required")
        except DowngradeError:
            refuse("downgrade", "plane doctor: Plane storage is newer than this release")
        except sqlite3.Error:
            refuse("unavailable", "plane doctor: Plane storage cannot be read")
        except (OSError, ImportError):
            refuse("unavailable", "plane doctor: host data or dependencies are unavailable")
        except RuntimeError:
            refuse("unavailable", "plane doctor: installed release resources are unavailable")
        except ValueError:
            refuse("invalid_argument", "plane doctor: invalid host or fleet selection")
        except ContractViolation:
            refuse("conflict", "plane doctor: Plane contract is invalid")
        data = {"status": "attention" if failing else "ok", "rungs": rungs,
                "attention_count": failing}
        if failing:
            if not getattr(args, "json", False):
                print("\n".join(lines))
            raise CommandFailure("conflict", f"plane doctor: {failing} rung(s) need attention", data=data)
        return CommandOutput(data, lines=tuple(lines))

    return execute("plane.doctor", operation, json_output=getattr(args, "json", False))


def cmd_plane_import_workstreams(args) -> int:
    """#1635: one-shot import of a pre-cutover `workstreams.json` into the
    plane -- the registry's write side moved with the F18 closure, the DATA
    did not. Pure `plan()` decides what to send (see
    `plane/workstream_import.py`'s docstring for the two rules it honours
    and the two R1-gauntlet hazards it reproduces from the shell writer);
    this command's job is the I/O: read the file, hold the SAME lock the
    shell writer holds, materialize the current registry inside it, plan,
    emit, optionally archive the source file on success. `--dry-run` prints
    the plan and touches neither the file nor the plane."""
    from ..brief import resolve_fleet_name
    from ..paths import load_lib_module
    from ..plane.workstream_import import batch_id, plan, registry_lock
    from ..source_state import SOURCE_ABSENT, SOURCE_UNREADABLE, probe_source

    paths = _resolve_paths(args)
    root = paths.root
    fleet = resolve_fleet_name(paths)
    if not fleet:
        print("migration workstreams: no fleet named -- pass --fleet or run from"
              " a fleet-scoped root", file=sys.stderr)
        return 2
    src = Path(args.file) if args.file else (paths.fleet_state / "workstreams.json")

    probe = probe_source(src)
    if probe.state == SOURCE_ABSENT:
        print(f"migration workstreams: no residual file at {src} -- nothing to import", file=sys.stderr)
        return 0
    if probe.state == SOURCE_UNREADABLE:
        print(f"migration workstreams: {src} exists but could not be opened -- refusing"
              " rather than reporting nothing to import", file=sys.stderr)
        return 3

    try:
        file_doc = json.loads(src.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"migration workstreams: {src} is present but unreadable: {exc}", file=sys.stderr)
        return 3

    from ..workstreams import lease_days_env
    lease_days = lease_days_env()

    pr = load_lib_module(paths.lib, "plane-readers.py")
    if pr is None:
        print(f"migration workstreams: lib/plane-readers.py is not readable under {paths.lib}",
              file=sys.stderr)
        return 3

    mode = capture_mode(_load_capture_config(root), fleet)
    if mode != "full":
        print(f"migration workstreams: this fleet's capture mode is {mode!r} -- the"
              " imported note/next_step text on progressed/renewed/blocked events"
              " will be STRIPPED at the door (workstream_event.note,"
              " workstream_event.next_step are CONTENT fields); the construct's"
              " goal is not content-classified and survives either way.",
              file=sys.stderr)

    def run() -> int:
        lock_path = paths.fleet_state / "workstreams.lock"
        with registry_lock(lock_path):
            # Read-only, non-creating (#1748 review): the dedup read must
            # not have the SIDE EFFECT of bringing a plane into existence,
            # or --dry-run -- which promises to touch neither the file nor
            # the plane -- leaves a stray db behind against a root that
            # never had one. open_ro is the same exists-before-connect
            # probe every read door shares: no plane yet means nothing to
            # dedup against, which is the unconditional-import case, not a
            # reason to skip (unlike prune/expire, where no db means
            # nothing to sweep). The REAL write, when there is one, still
            # creates the plane exactly as any door's first-ever emit does
            # -- that happens below, inside emit_batch, never here.
            ro_conn, _note = open_ro(root)
            if ro_conn is None:
                existing = {"workstreams": {}, "archived": []}
            else:
                try:
                    existing = pr.workstream_registry(ro_conn, fleet, lease_days=lease_days, or_empty=True)
                finally:
                    ro_conn.close()

            batch = batch_id(src.stat().st_mtime)
            the_plan = plan(file_doc, existing, fleet=fleet, import_batch=batch, lease_days=lease_days)

            for w in the_plan.warnings:
                print(f"migration workstreams: {w.workstream_id}: {w.detail}", file=sys.stderr)
            for s in the_plan.skipped:
                print(f"migration workstreams: skipped {s.workstream_id} -- {s.reason}", file=sys.stderr)

            if not args.apply:
                for ev in the_plan.events:
                    print(json.dumps(ev, separators=(",", ":")))
                print(f"migration workstreams: would emit {len(the_plan.events)} event(s)"
                      f" for {len(file_doc.get('workstreams', {})) - len(the_plan.skipped)}"
                      f" row(s), batch {batch}", file=sys.stderr)
                return 0

            if not the_plan.events:
                print("migration workstreams: nothing new to import (every row already"
                      " on the plane, or the file holds none)")
                return 0

            outcomes = emit_batch(root, the_plan.events)
            committed = sum(1 for o in outcomes if o.status == "committed")
            duplicate = sum(1 for o in outcomes if o.status == "duplicate")
            spooled = [o for o in outcomes if o.status == "spooled"]
            if spooled:
                print(f"migration workstreams: {len(spooled)} event(s) SPOOLED -- durable"
                      " on disk, not yet in the plane; retry with `claudlobby plane"
                      " spool retry` before archiving the source file", file=sys.stderr)
                return RC_SPOOLED

            print(f"migration workstreams: {committed} event(s) committed,"
                  f" {duplicate} already present, batch {batch}")

            if args.archive:
                dest = src.with_name(f"{src.name}.imported-{batch}")
                src.rename(dest)
                print(f"migration workstreams: archived {src.name} -> {dest.name}")
            return 0

    return _guarded("migration workstreams", run)


def cmd_plane_view(args) -> int:
    """Run the Phase-4 operator-plane view daemon in the foreground (same
    supervision posture as serve: systemd/launchd own backgrounding). Binds
    LOCALHOST by default — Tailscale Serve fronts it per the design walk;
    --host is the raw-bind dev fallback."""
    paths = _resolve_paths(args)
    root = paths.root
    try:
        from ..plane.view import begin_shutdown, create_app
        import uvicorn
    except (ImportError, RuntimeError) as exc:
        print(
            "plane view: the UI needs the optional [plane-ui] extra — "
            "install with: pip install -e '.[plane-ui]'"
            f" ({exc})", file=sys.stderr)
        return 1
    app = create_app(root, package=paths.package)

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
    — systemd Restart=always / launchd KeepAlive, never a self-fork). Exits 4
    when the db is newer than this code, so that same supervision relaunches
    it on the install's current modules (#1485)."""
    root = _resolve_paths(args).root

    def run() -> int:
        from ..plane.daemon import (
            DOWNGRADE_EXIT_CODE, DaemonAlreadyRunning, PlaneDaemon,
            PlaneDowngradeExit, SocketOverrideInvalid, SocketPathTooLong,
        )

        try:
            PlaneDaemon(
                root,
                socket_override=Path(args.socket) if args.socket else None,
                drain_interval=float(args.drain_interval),
            ).serve()
        except PlaneDowngradeExit:
            # #1485: the daemon already printed the ONE line naming the
            # condition and why it is exiting. Caught by name — ahead of
            # _guarded's generic DowngradeError REFUSED line — so a
            # supervisor's journal shows one line per relaunch, not two.
            return DOWNGRADE_EXIT_CODE
        except (DaemonAlreadyRunning, SocketOverrideInvalid,
                SocketPathTooLong) as exc:
            print(f"plane serve: REFUSED — {exc}", file=sys.stderr)
            return 1
        return 0

    return _guarded("plane serve", run)


def cmd_plane_schema(args) -> int:
    print(json.dumps(export_schemas(), indent=2, sort_keys=True))
    return 0
