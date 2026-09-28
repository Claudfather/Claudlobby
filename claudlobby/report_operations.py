"""Acknowledge one served fleet-report prefix through the existing Plane fact."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
from uuid import UUID


@dataclass(frozen=True)
class AckResult:
    request_id: str
    viewer: str
    fleet_uid: str
    acked_through_seq: int
    count: int
    recording: str
    request_persisted: bool
    replayed: bool


class AckError(RuntimeError):
    """A refused ACK, optionally carrying independently proved commit truth."""

    def __init__(self, code: str, message: str, *, result: AckResult | None = None):
        self.code, self.result = code, result
        super().__init__(message)


@contextmanager
def _viewer_lock(root: Path, fleet_uid: str, viewer_uid: str):
    """Serialize distinct request UUIDs for one viewer without a task lock."""
    directory = root / "state"
    for part in ("report-ack-locks", fleet_uid):
        directory = directory / part
        if directory.is_symlink():
            raise AckError("conflict", "report ACK lock directory is redirected")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise AckError("conflict", "report ACK lock directory is not private")
    path = directory / (viewer_uid + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise AckError("conflict", "report ACK lock is not a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AckError("conflict", "another report ACK holds this viewer") from exc
        linked = path.lstat()
        if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
            raise AckError("conflict", "report ACK lock was replaced")
        yield
    finally:
        os.close(fd)


def _proof(root: Path, fact):
    from .plane.db import connect_ro, db_file
    from .plane.schema_state import require_current_schema
    from .request_facts import reconcile_facts

    with closing(connect_ro(db_file(root))) as conn:
        conn.execute("BEGIN")
        require_current_schema(conn)
        return reconcile_facts(conn, (fact,))


def _result(ctx, request_id: str, prefix, *, persisted: bool, replayed: bool) -> AckResult:
    return AckResult(request_id, ctx.caller.alias, ctx.fleet_uid, prefix.through_seq,
                     prefix.count, "committed", persisted, replayed)


def ack_reports(ctx, selected: dict, cursor: str, *, request_id: str) -> AckResult:
    """Commit one previously served prefix; caller holds mutation admission.

    An existing UUID is reconciled before any new effect. No missing/partial
    proof becomes an empty report set, and no recording outage enters a spool.
    """
    from .activation_identity import read_selected_identity_bindings
    from .activation_state import read_selection
    from .brief import ack_request, plane_session
    from .plane import PLANE_SCHEMA_VERSION
    from .plane.emit_api import emit_batch, validate_item
    from .plane.ids import mint_event_id
    from .plane.schema_state import require_current_schema
    from .report_cursors import CursorError, decode_cursor, validate_prefix
    from .request_facts import expected_fact
    from .request_receipts import (ReceiptError, RequestIntent, StagePlan,
                                   locked_request, semantic_digest)

    root = ctx.root
    fleet = ctx.context.fleet.name
    viewers = [name for name, actor in ctx.bots.items() if actor == ctx.caller]
    bot = viewers[0] if len(viewers) == 1 else None
    if (not isinstance(selected, dict) or set(selected) != {
            "schema", "activation_id", "release_id", "plan_id"}
            or read_selection(root) != selected or bot not in ctx.context.fleet.bots
            or ctx.caller.alias != f"bot:{fleet}/{bot}"
            or ctx.caller_fleet_uid != ctx.fleet_uid):
        raise AckError("conflict", "report ACK requires the selected generated viewer")
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise AckError("invalid_argument", "report ACK requires a canonical request UUID") from exc
    bindings = read_selected_identity_bindings(root, fleet, package=ctx.context.paths.package)
    if (bindings["host_uid"] != ctx.host_uid or bindings["fleet_uid"] != ctx.fleet_uid
            or bindings["bots"].get(bot) != ctx.caller.uid):
        raise AckError("conflict", "report viewer differs from active identity bindings")
    identity = {"host_uid": ctx.host_uid, "fleet_uid": ctx.fleet_uid,
                "viewer_uid": ctx.caller.uid, "release_id": selected["release_id"],
                "activation_id": selected["activation_id"], "plan_id": selected["plan_id"]}
    try:
        prefix = decode_cursor(root, cursor, identity=identity)
    except CursorError as exc:
        code = "unavailable" if exc.unavailable else "conflict"
        raise AckError(code, "report ACK cursor is unavailable or differs from this viewer") from exc
    if prefix.count < 1:
        raise AckError("conflict", "report ACK cursor has no served reports")
    semantic = semantic_digest({"cursor": cursor})

    try:
        with locked_request(root, ctx.fleet_uid, request_id) as store:
            with _viewer_lock(root, ctx.fleet_uid, ctx.caller.uid):
                previous = store.load()
                if previous is not None:
                    intent = previous.intent
                    if (intent.operation != "fleet.reports.ack" or intent.operation_version != 1
                            or intent.host_uid != ctx.host_uid or intent.fleet_uid != ctx.fleet_uid
                            or intent.caller_uid != ctx.caller.uid or intent.recipient_uid is not None
                            or intent.semantic_sha256 != semantic or len(intent.stages) != 1
                            or intent.stages[0].kind != "recording"
                            or len(intent.stages[0].facts) != 1
                            or intent.stages[0].facts[0].family != "system"):
                        raise AckError("conflict", "request UUID has different report ACK semantics")
                    proof = _proof(root, intent.stages[0].facts[0])
                    if proof.status == "committed":
                        if previous.stages[0].status == "unknown":
                            try:
                                store.outcome(0, "committed")
                            except OSError as exc:
                                raise AckError("unavailable", "ACK committed but request outcome persistence failed",
                                               result=_result(ctx, request_id, prefix, persisted=False,
                                                              replayed=True)) from exc
                        elif previous.stages[0].status != "committed":
                            raise AckError("conflict", "ACK fact differs from retained attempt status")
                        return _result(ctx, request_id, prefix, persisted=True, replayed=True)
                    raise AckError("unavailable" if proof.status == "unknown" else "conflict",
                                   "existing report ACK cannot be proved committed; inspect request")

                session, _ = plane_session(ctx.context.paths, fleet)
                if session is None:
                    raise AckError("unavailable", "Plane report reader is unavailable")
                with session:
                    session.conn.execute("BEGIN")
                    require_current_schema(session.conn)
                    if session.pr.fleet_uid(session.conn, fleet) != ctx.fleet_uid:
                        raise AckError("conflict", "report fleet differs from active identity")
                    entry = session.pr.roster(session.conn, fleet).get(bot.lower())
                    if entry is None or ctx.caller.uid not in entry["uids"]:
                        raise AckError("unavailable", "report viewer history is unavailable")
                    ack = session.pr.newest_ack(session.conn, entry["uids"])
                    if ((ack["seq"] if ack else None) != prefix.prior_seq
                            or (ack["landed_seq"] if ack else None) != prefix.prior_ack_seq):
                        raise AckError("conflict", "report read position advanced after this prefix was served")
                    rows = session.pr.report_rows(session.conn, fleet, since_seq=prefix.prior_seq)
                    rows = session.pr.unacked_rows(rows, prefix.prior_seq)
                    rows.sort(key=lambda row: (row["_seq"], row["plane_msg_id"]))
                    try:
                        validate_prefix(prefix, rows)
                    except CursorError as exc:
                        raise AckError("conflict", "served report prefix changed") from exc
                if read_selection(root) != selected:
                    raise AckError("conflict", "active report selection changed")

                raw = ack_request(fleet, bot, acked_through_seq=prefix.through_seq,
                                  acked_through_ts=prefix.through_ts, count=prefix.count)
                raw.update(emitter="fleet.reports.ack", event_id=mint_event_id(),
                           source_ref=f"request:{request_id}",
                           occurred_at=datetime.now(timezone.utc).isoformat(),
                           schema_version=PLANE_SCHEMA_VERSION)
                fact = expected_fact(validate_item(raw, {})[0], host_uid=ctx.host_uid,
                                     fleet_uid=ctx.fleet_uid,
                                     parties={ctx.caller.alias: ctx.caller.uid})
                store.prepare(RequestIntent("fleet.reports.ack", 1, ctx.host_uid, ctx.fleet_uid,
                                            ctx.caller.uid, None, semantic,
                                            (StagePlan("recording", (fact,)),)))
                store.begin_attempt()
                store.stage(0)
                try:
                    outcome = emit_batch(root, [raw], require_commit=True)
                except (OSError, sqlite3.Error) as exc:
                    raise AckError("unavailable", "report ACK recording is unconfirmed; inspect request") from exc
                if len(outcome) != 1 or outcome[0].event_id != raw["event_id"]:
                    raise AckError("unavailable", "report ACK recording result is unconfirmed")
                proof = _proof(root, fact)
                if proof.status == "conflict":
                    raise AckError("conflict", "report ACK differs from its frozen fact")
                if proof.status != "committed":
                    raise AckError("unavailable", "report ACK recording proof is unavailable")
                try:
                    store.outcome(0, "committed")
                except OSError as exc:
                    raise AckError("unavailable", "ACK committed but request outcome persistence failed",
                                   result=_result(ctx, request_id, prefix, persisted=False,
                                                  replayed=False)) from exc
                return _result(ctx, request_id, prefix, persisted=True, replayed=False)
    except ReceiptError as exc:
        raise AckError("conflict", "report ACK request receipt is unavailable or conflicts") from exc
    except (OSError, sqlite3.Error) as exc:
        raise AckError("unavailable", "report ACK state or Plane proof is unavailable") from exc
