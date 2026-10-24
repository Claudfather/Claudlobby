"""Consume the clauDNA export contract, one durable event per sealed segment.

Only export/ack touches the store. Per-item ingest preserves normal summaries
on replay; deterministic refusals get a separate status event so cursors can move.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from .automation_state import _state_lock, _write
from .context import resolve_paths
from .fleet_notification import notify_fleet
from .plane.contracts import ContractViolation
from .plane.emit_api import emit_batch
from .plane.fleet_events import fleet_event_request
from .plane.ids import derive_session_uid, derive_uid
from .plane.migrations import DowngradeError
from .plane.registries import cap_for
from .plane.schema_state import PendingMigrationError
from .plane.spool import SpoolWriteError

ENTRYPOINT_SCHEMA = "claudna.entrypoint/1"
EXPORT_SCHEMA = "claudna.export/1"
CONSUMER = "claudlobby"
EMITTER = "session-export"
DATA_SCHEMA = "session_summary/1"
EXPORT_TIMEOUT_S = 60.0
DATA_CAP_BYTES = cap_for("system", "data") - 4_096
MIN_PLUGIN_VERSION = (0, 28, 0)
STALL_AFTER = 3
CAPS = {
    "title": 300,
    "intent": 300,
    "outcome": 300,
    "arc": 2_000,
    "list_items": 12,
    "list_item": 300,
    "skipped_reason": 64,
}
LANDED = {"committed", "duplicate", "spooled"}
# The plane takes no write while its database is absent or its schema is older
# or newer than this release's, and a write it can neither store nor spool (a
# full disk) is lost. Neither is the item's fault: the tick fails and keeps its
# cursor until the plane can take the write (#2477).
PLANE_CANNOT_WRITE = (PendingMigrationError, DowngradeError, SpoolWriteError)


@dataclass(frozen=True)
class Entrypoint:
    python: str
    entrypoint: str
    plugin_version: str | None
    agent_cli: str | None


@dataclass(frozen=True)
class BotOutcome:
    bot: str
    emitted: int = 0
    duplicate: int = 0
    spooled: int = 0
    unexportable: int = 0
    acked: dict[str, int] | None = None
    skipped: str | None = None
    failed: str | None = None
    records: tuple[dict, ...] = ()


def read_entrypoint(state_dir: Path) -> Entrypoint | str:
    try:
        doc = json.loads((state_dir / "entrypoint.json").read_text())
    except FileNotFoundError:
        return "no_entrypoint"
    except (OSError, ValueError):
        return "unknown_schema"
    if not isinstance(doc, dict) or doc.get("schema") != ENTRYPOINT_SCHEMA:
        return "unknown_schema"
    version = doc.get("plugin_version")
    match = (
        re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version or "")
        if isinstance(version, str)
        else None
    )
    if not match or tuple(map(int, match.groups())) < MIN_PLUGIN_VERSION:
        return "old_entrypoint"
    entry = doc.get("entrypoint")
    if (
        not isinstance(entry, str)
        or not Path(entry).is_absolute()
        or not Path(entry).exists()
    ):
        return "stale_entrypoint"
    python = doc.get("python") or sys.executable
    if not isinstance(python, str) or not Path(python).is_absolute():
        return "stale_entrypoint"
    return Entrypoint(python, entry, version, doc.get("agent_cli"))


def export_argv(entry: Entrypoint, state_dir: Path) -> list[str]:
    return [
        entry.python or sys.executable,
        "-S",
        entry.entrypoint,
        "export",
        "--root",
        str(state_dir),
        "--consumer",
        CONSUMER,
        "--include-skipped",
        "--json",
    ]


def ack_argv(entry: Entrypoint, state_dir: Path, sid: str, through: int) -> list[str]:
    return [
        entry.python or sys.executable,
        "-S",
        entry.entrypoint,
        "export",
        "--root",
        str(state_dir),
        "--consumer",
        CONSUMER,
        "--ack",
        "--sid",
        sid,
        "--through",
        str(through),
    ]


def _instant(raw, fallback: datetime) -> datetime:
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return at if at.tzinfo is not None and at.utcoffset() is not None else fallback
    except (ValueError, TypeError, AttributeError):
        return fallback


def _identity(item: dict, fleet: str, bot: str, now: datetime) -> tuple[dict, datetime]:
    session = item["session"]
    segment = item.get("segment") or {}
    at = _instant(segment.get("sealed_at") or session.get("closed_at"), now)
    agent_cli = session.get("agent_cli") or "claude"
    counts = segment.get("counts") or {}
    return {
        "schema": DATA_SCHEMA,
        "ts": at.isoformat(),
        "session_id": item["sid"],
        "session_uid": derive_session_uid(item["sid"], agent_cli),
        "agent_cli": agent_cli,
        "bot": bot,
        "fleet": fleet,
        "seg": item["seg"],
        "sealed_at": segment.get("sealed_at"),
        "sealed_by": segment.get("sealed_by"),
        **{
            key: counts.get(key)
            for key in ("prompts", "skills", "failures", "interrupts")
        },
    }, at


def summary_record(
    item: dict, *, fleet: str, bot: str, now: datetime
) -> tuple[dict, datetime]:
    record, at = _identity(item, fleet, bot, now)
    summary = item.get("summary")
    record.update(
        status="skipped" if summary is None else "ok",
        skipped_reason=(item.get("skipped") or {}).get("reason")
        if summary is None
        else None,
        turns=None,
        transcript_bytes=None,
        journey=None,
        blocks=None,
        procedures=None,
        producer=None,
    )
    if summary is not None:
        inp = summary["input"]
        record.update(
            turns=inp["turns"],
            transcript_bytes=inp["range"]["end"] - inp["range"]["start"],
            journey=summary["journey"],
            blocks={
                "count": len(summary["blocks"]),
                "kinds": dict(Counter(b["home"] for b in summary["blocks"])),
            },
            procedures=len(summary["procedures"]),
            producer=summary["producer"],
        )
    return bound(record), at


def unexportable_record(
    item: dict, *, fleet: str, bot: str, now: datetime
) -> tuple[dict, datetime]:
    record, at = _identity(item, fleet, bot, now)
    record.update(
        status="skipped",
        skipped_reason="unexportable",
        turns=None,
        transcript_bytes=None,
        journey=None,
        blocks=None,
        procedures=None,
        producer=None,
    )
    return bound(record), at


def bound(record: dict) -> dict:
    """Cap prose, then shed arc and lists before metadata; never rely on ingest truncation."""
    result = dict(record)
    journey = result.get("journey")
    if isinstance(journey, dict):

        def trim(value, key=""):
            if isinstance(value, str):
                return value[: CAPS.get(key, CAPS["list_item"])]
            if isinstance(value, list):
                return [trim(v) for v in value[: CAPS["list_items"]]]
            if isinstance(value, dict):
                return {k: trim(v, k) for k, v in value.items()}
            return value

        journey = result["journey"] = trim(journey)
    if isinstance(result.get("skipped_reason"), str):
        result["skipped_reason"] = result["skipped_reason"][: CAPS["skipped_reason"]]

    def fits():
        return len(json.dumps(result, ensure_ascii=False).encode()) <= DATA_CAP_BYTES

    if not fits() and isinstance(journey, dict):
        journey["arc"] = []
        for key in ("done", "in_progress", "next"):
            if fits():
                break
            journey[key] = []
    if not fits():
        # Unbounded producer/unknown additive fields cannot consume the identity budget.
        result["producer"] = None
    if not fits():
        raise ContractViolation(
            [{"loc": ("data",), "msg": "summary identity exceeds export budget"}]
        )
    return result


def _system_event(
    event_type: str,
    *,
    fleet: str,
    bot: str,
    sid: str,
    seg: int,
    occurred_at: datetime,
    observed_at: datetime,
    data: dict,
    fallback: bool = False,
) -> dict:
    material = (
        f"export_stalled:{fleet}:{bot}:{sid}"
        if event_type == "export_stalled"
        else f"session_summary{'_unexportable' if fallback else ''}:{fleet}:{sid}:{seg}"
    )
    return fleet_event_request(
        event_type,
        fleet=fleet,
        subject_kind="actor",
        subject=f"bot:{fleet}/{bot}",
        bot=bot,
        source=EMITTER,
        data=data,
        occurred_at=occurred_at.isoformat(),
        observed_at=observed_at.isoformat(),
        event_id=derive_uid("ev", material),
    )


def report_stall(
    root: Path, fleet: str, bot: str, state: dict, *, emit=None, notify=None
) -> dict:
    emit = emit or emit_batch
    notify = notify or notify_fleet
    if state.get("consecutive_failures", 0) < STALL_AFTER:
        return state
    stall = state.setdefault(
        "stall",
        {
            "since": state.get("failure_since") or state["last_tick"],
            "recorded": False,
            "notified": False,
        },
    )
    at = _instant(stall["since"], datetime.now(timezone.utc))
    if not stall.get("recorded"):
        try:
            raw = _system_event(
                "export_stalled",
                fleet=fleet,
                bot=bot,
                sid=stall["since"],
                seg=0,
                occurred_at=at,
                observed_at=_instant(state["last_tick"], at),
                data={
                    "bot": bot,
                    "since": stall["since"],
                    "failure": state.get("last_failure"),
                },
            )
            stall["recorded"] = (
                emit(root, [raw], require_commit=False)[0].status in LANDED
            )
        except (ContractViolation, RuntimeError, sqlite3.Error, OSError):
            pass  # This part is retried, independently of the page, next failed tick.
    if not stall.get("notified"):
        try:
            result = notify(
                root=root,
                fleet=fleet,
                level="alert",
                event="export_stalled",
                message=f"session-export for {bot} failed {state['consecutive_failures']} ticks; its cursor is stalled",
            )
            detail = result if isinstance(result, dict) else result.data()
            stall["notified"] = detail.get("notification") == "submitted"
        except (ValueError, RuntimeError, OSError):
            pass
    return state


def _envelope(raw: str) -> dict:
    doc = json.loads(raw)
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != EXPORT_SCHEMA
        or doc.get("consumer") != CONSUMER
        or not isinstance(doc.get("items"), list)
        or not isinstance(doc.get("next"), dict)
    ):
        raise ValueError("invalid clauDNA export envelope")
    for sid, through in doc["next"].items():
        if (
            not isinstance(sid, str)
            or not sid
            or type(through) is not int
            or through < 1
        ):
            raise ValueError("invalid export next cursor")
    for item in doc["items"]:
        if (
            not isinstance(item, dict)
            or item.get("sid") not in doc["next"]
            or type(item.get("seg")) is not int
            or not 1 <= item["seg"] <= doc["next"][item["sid"]]
            or not isinstance(item.get("session"), dict)
        ):
            raise ValueError("invalid export item identity")
    return doc


def export_bot(
    root: Path,
    fleet: str,
    bot: str,
    state_dir: Path,
    *,
    run=subprocess.run,
    now: datetime | None = None,
    dry_run: bool = False,
    emit=None,
    notify=None,
    fleet_state: Path | None = None,
) -> BotOutcome:
    """Hold the per-bot lock through read, per-session landing/ack, and durable state."""
    now = now or datetime.now(timezone.utc)
    emit = emit or emit_batch
    state_file = (
        (fleet_state or resolve_paths(root=root, fleet=fleet).fleet_state)
        / "session-export"
        / f"{bot}.json"
    )
    if dry_run:
        return _export(root, fleet, bot, state_dir, {}, run, now, True, emit, notify)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(state_file.with_suffix(".json.lock")):
        try:
            state = json.loads(state_file.read_text())
            if not isinstance(state, dict):
                raise ValueError("session-export state is not an object")
        except FileNotFoundError:
            state = {}
        outcome = _export(
            root, fleet, bot, state_dir, state, run, now, False, emit, notify
        )
        _write(state_file, state)
        return outcome


def _export(root, fleet, bot, state_dir, state, run, now, dry_run, emit, notify):
    emitted = duplicate = spooled = unexportable = 0
    acked: dict[str, int] = {}
    records = []
    state["last_tick"] = now.isoformat()
    entry = read_entrypoint(state_dir)
    if isinstance(entry, str):
        state["last_skip"] = entry
        state["consecutive_failures"] = 0
        state["failure_since"] = None
        return BotOutcome(bot, acked=acked, skipped=entry)
    state["last_skip"] = None
    failure = None
    phase = "export"
    try:
        response = run(
            export_argv(entry, state_dir),
            capture_output=True,
            text=True,
            timeout=EXPORT_TIMEOUT_S,
        )
        if response.returncode:
            raise ValueError("export door exited nonzero")
        envelope = _envelope(response.stdout)
        state["pending"] = len(envelope["items"])
        items_by_sid = {}
        for item in envelope["items"]:
            items_by_sid.setdefault(item["sid"], []).append(item)
        # next is ordered by the producer's session walk, not global segment order.
        for sid, through in envelope["next"].items():
            for item in items_by_sid.get(sid, ()):
                phase = "emit"
                try:
                    record, at = summary_record(item, fleet=fleet, bot=bot, now=now)
                    if dry_run:
                        records.append(record)
                        continue
                    event = _system_event(
                        "session_summary",
                        fleet=fleet,
                        bot=bot,
                        sid=sid,
                        seg=item["seg"],
                        occurred_at=at,
                        observed_at=now,
                        data=record,
                    )
                    outcome = emit(root, [event], require_commit=False)[0]
                except PLANE_CANNOT_WRITE:
                    raise
                except (
                    ContractViolation,
                    RuntimeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as original:
                    if dry_run:
                        raise
                    unexportable += 1
                    try:
                        record, at = unexportable_record(
                            item, fleet=fleet, bot=bot, now=now
                        )
                        fallback = _system_event(
                            "session_summary",
                            fleet=fleet,
                            bot=bot,
                            sid=sid,
                            seg=item["seg"],
                            occurred_at=at,
                            observed_at=now,
                            data=record,
                            fallback=True,
                        )
                        outcome = emit(root, [fallback], require_commit=False)[0]
                    except PLANE_CANNOT_WRITE:
                        raise
                    except (ContractViolation, RuntimeError) as refused:
                        state["last_unexportable"] = {
                            "sid": sid,
                            "seg": item["seg"],
                            "original": str(original)[:2000],
                            "fallback": str(refused)[:2000],
                        }
                        state["pending"] -= 1
                        continue  # Terminal deterministic refusal: still ack this session.
                if outcome.status not in LANDED:
                    raise OSError("event did not land")
                emitted += outcome.status == "committed"
                duplicate += outcome.status == "duplicate"
                spooled += outcome.status == "spooled"
                state["pending"] -= 1
            if dry_run:
                continue
            phase = "ack"
            response = run(
                ack_argv(entry, state_dir, sid, through),
                capture_output=True,
                text=True,
                timeout=EXPORT_TIMEOUT_S,
            )
            if response.returncode:
                raise ValueError("ack door exited nonzero")
            acked[sid] = through
            # Keep the observed progress even if a later session fails. The
            # failure streak belongs to the whole bot tick, not one session.
            state["last_ack_at"] = now.isoformat()
    except subprocess.TimeoutExpired:
        failure = f"{phase}_timeout"
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
        *PLANE_CANNOT_WRITE,
    ) as exc:
        failure = f"{phase}_failed"
        state["failure_detail"] = str(exc)[:2000]
    if failure and not dry_run:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["failure_since"] = state.get("failure_since") or now.isoformat()
        state["last_failure"] = failure
        counters = state.setdefault("failure_counts", {})
        counters[failure] = counters.get(failure, 0) + 1
        report_stall(root, fleet, bot, state, emit=emit, notify=notify)
    if not failure and not dry_run:
        state["consecutive_failures"] = 0
        state["failure_since"] = None
        state.pop("stall", None)
        state["last_failure"] = None
    return BotOutcome(
        bot,
        emitted,
        duplicate,
        spooled,
        unexportable,
        acked,
        failed=failure,
        records=tuple(records) if dry_run else (),
    )
