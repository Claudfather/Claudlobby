"""Each bot's Claude Code compactions, recorded on the plane once each (#2206).

A compaction leaves one ``compact_boundary`` row in the bot's session
transcript (``transcript_usage.compaction_row``): its trigger, the context
before it and, once it ends, after it. Nothing else records one. A pass reads
only what each declared bot's session transcripts appended since the cursor
kept in the fleet's state, and emits each boundary row it finds as one
``compaction`` system event, anchored on the bot's actor at the compaction's
own time, with the ``fleet-events:`` provenance that ``event list`` reads. The
pulse runs a pass each tick through ``claudlobby fleet compactions record``.

Once each, twice over: a cursor moves past a row only once the plane has taken
its event (committed, duplicate or spooled), and the event id is derived from
the row (fleet, bot, session, row uuid), so a row read again is a duplicate the
plane stores once. Two passes at once are harmless for the same reason.

What a pass cannot read it names, and records nothing for: an unresolved or
missing transcript directory, or an unreadable transcript, is that bot's
``reason``, and its cursor stays where it was, so the rows wait for a pass that
reads. Before a bot's first pass only its newest session is read, from at most
FIRST_READ bytes before its end. Bytes appended past READ_CAP in one pass are
skipped and counted (``skipped_bytes``): a boundary row in them is not recorded.
Cost per pass: one directory listing per bot and the bytes appended since the
last pass, never more than READ_CAP per transcript.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .plane.ids import derive_uid
from .transcript_usage import _loads, compaction_row, session_transcripts, transcript_source

CURSOR_FILE = "compaction-cursor.json"
EVENT = "compaction"
EMITTER = "fleet-compactions"
READ_CAP = 8 * 1024 * 1024
FIRST_READ = 8 * 1024 * 1024
# A followed transcript untouched this long leaves the cursor; if its session
# resumes, it is read again from FIRST_READ and its rows are duplicates.
_FOLLOW_S = 7 * 86400
_ACCEPTED = {"committed", "duplicate", "spooled"}


def compaction_event(fleet: str, bot: str, row: dict, session: str, observed_at: str) -> dict:
    material = "\x1f".join((EVENT, fleet, bot, session, row["uuid"] or row["at"]))
    return {"event_id": derive_uid("ev", material), "event_type": "system",
            "emitter": EMITTER, "fleet": fleet, "occurred_at": row["at"],
            "observed_at": observed_at,
            "source_ref": "fleet-events:sha:" + sha256(material.encode("utf-8")).hexdigest(),
            "payload": {"event": EVENT, "subject_kind": "actor", "subject": f"bot:{fleet}/{bot}",
                        "data": {"source": "compactions", "legacy_ts": row["at"],
                                 "data": {"trigger": row["trigger"],
                                          "pre_tokens": row["pre_tokens"],
                                          "post_tokens": row["post_tokens"],
                                          "session": session}}}}


def _boundaries(path: Path, start: int, end: int, aligned: bool) -> tuple[list, int]:
    """The compaction rows in bytes [start, end) of one transcript, and the
    offset just past its last complete line: a line still being written waits
    for the next pass. Unless ``start`` is a line boundary (``aligned``), the
    first line began before the read and is dropped."""
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read(end - start)
    if len(data) != end - start:
        raise OSError("transcript shrank during the read")
    last = data.rfind(b"\n")
    if last < 0:
        return [], start
    complete = data[:last + 1]
    if not aligned:
        complete = complete[complete.find(b"\n") + 1:]
    rows = []
    for line in complete.split(b"\n"):
        if b'"compact_boundary"' in line:
            row = compaction_row(_loads(line))
            if row is not None:
                rows.append(row)
    return rows, start + last + 1


def _load(path: Path) -> tuple[dict, bool]:
    """The cursor, and whether an unreadable one was set aside."""
    try:
        cursor = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    bots = cursor.get("bots") if isinstance(cursor, dict) and cursor.get("v") == 1 else None
    return (bots, False) if isinstance(bots, dict) else ({}, True)


def _save(path: Path, bots: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "bots": bots}, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _read_bot(paths, fleet, bot: str, state: dict, started_ns: int, observed_at: str,
              read_cap: int, first_read: int) -> tuple[dict, list, dict | None]:
    """One bot's pass: its result row, its events, and its next cursor state
    (None keeps the old one whole)."""
    out = {"bot": bot, "rows": 0, "recorded": 0, "duplicates": 0,
           "bytes_read": 0, "skipped_bytes": 0, "reason": None}
    source = transcript_source(paths, fleet, bot)
    if source.directory is None:
        return {**out, "reason": source.issue}, [], None
    sessions, issue = session_transcripts(source.directory)
    if issue:
        return {**out, "reason": issue}, [], None
    followed = state.get("files") if isinstance(state.get("files"), dict) else {}
    last_pass = state.get("pass_ns") if type(state.get("pass_ns")) is int else None
    newest = max(sessions, key=lambda entry: (entry[2], entry[0]))[0] if sessions else None
    keep_after = time.time_ns() - _FOLLOW_S * 10**9
    events, files = [], {}
    for name, size, mtime_ns in sorted(sessions):
        offset = followed.get(name)
        if type(offset) is int and 0 <= offset <= size:
            if offset == size:
                # Nothing appended: not opened, and dropped once long untouched.
                if mtime_ns >= keep_after:
                    files[name] = offset
                continue
            start, aligned = offset, True
        elif (name != newest if last_pass is None else mtime_ns <= last_pass):
            continue    # history from before the bot was followed
        else:
            start = max(0, size - first_read)
            aligned = start == 0
        if size - start > read_cap:
            out["skipped_bytes"] += size - start - read_cap
            start, aligned = size - read_cap, False
        try:
            rows, files[name] = _boundaries(source.directory / name, start, size, aligned)
        except OSError:
            return {**out, "reason": "unreadable_transcript_file"}, [], None
        out["bytes_read"] += size - start
        out["rows"] += len(rows)
        session = name[:-len(".jsonl")]
        events.extend(compaction_event(fleet.name, bot, row, row["session"] or session,
                                       observed_at) for row in rows)
    return out, events, {"pass_ns": started_ns, "files": files}


def record_compactions(paths, fleet, *, emit, read_cap: int = READ_CAP,
                       first_read: int = FIRST_READ) -> dict:
    """One pass over the selected fleet's declared bots. ``emit`` takes a list
    of raw requests and returns one outcome (``.status``) for each."""
    started_ns = time.time_ns()
    observed_at = datetime.now(timezone.utc).isoformat()
    cursor_path = Path(paths.fleet_state) / CURSOR_FILE
    cursor, reset = _load(cursor_path)
    rows, events, nexts = [], [], {}
    for bot in sorted(fleet.bots):
        state = cursor.get(bot) if isinstance(cursor.get(bot), dict) else {}
        row, found, nxt = _read_bot(paths, fleet, bot, state, started_ns, observed_at,
                                    read_cap, first_read)
        rows.append(row)
        events.extend(found)
        if nxt is not None:
            nexts[bot] = nxt
    # The same row in two transcripts (a session copied on resume) is one event:
    # a batch carrying an id twice is refused whole.
    unique = list({event["event_id"]: event for event in events}.values())
    error = None
    if unique:
        try:
            outcomes = emit(unique)
            statuses = {event["event_id"]: outcome.status
                        for event, outcome in zip(unique, outcomes)}
            if len(statuses) != len(unique) or set(statuses.values()) - _ACCEPTED:
                error = "the plane did not take every event: " + ", ".join(sorted(
                    set(statuses.values()) - _ACCEPTED) or ["missing outcomes"])
        except Exception as exc:  # noqa: BLE001 - reported, and no cursor moves
            error = f"{type(exc).__name__}: {exc}"
    owner = {event["event_id"]: event["payload"]["subject"].rsplit("/", 1)[1] for event in unique}
    if error is None:
        by_bot = {row["bot"]: row for row in rows}
        for event_id, bot in owner.items():
            by_bot[bot]["duplicates" if statuses[event_id] == "duplicate" else "recorded"] += 1
        cursor.update(nexts)
    else:
        # Bots whose rows the plane did not take keep their cursor; the rest advance.
        refused = set(owner.values())
        cursor.update({bot: nxt for bot, nxt in nexts.items() if bot not in refused})
    cursor = {bot: state for bot, state in cursor.items() if bot in fleet.bots}
    _save(cursor_path, cursor)
    return {"fleet": fleet.name, "error": error, "cursor_reset": reset,
            "recorded": sum(row["recorded"] for row in rows),
            "cursor": str(cursor_path), "bots": rows}
