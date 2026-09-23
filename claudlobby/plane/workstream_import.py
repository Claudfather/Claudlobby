"""One-shot import of a pre-cutover runtime/workstreams.json into the plane (#1635).

The workstream registry's read side and write side both moved to the plane
(F18 closure R1); the DATA did not. A fleet that opened a workstream before
its cutover still has a `runtime/workstreams.json` no code path opens, and
every door answers "No workstreams" — correctly about the store, falsely
about the fleet, silently in both directions, because an empty plane source
is a legitimate answer. The rows are UNREAD, not destroyed (F18 chose a
clean epoch), so this is a repair tool, not archaeology.

Two rules this module exists to honour, both from the naive-import failure
mode #1635 names explicitly as the wrong turn:

  * ORIGINAL INSTANTS. `lib/plane-readers.py::workstream_registry` RECOMPUTES
    a never-renewed lease as opened + WORKSTREAM_LEASE_DAYS, and re-derives
    it on every `progressed` event from THAT event's own `occurred_at` — so
    a constructs-only import re-leases every stale row from the import
    instant and silences `brief`'s stall flags for another fortnight. Every
    verb event here carries its own historical `occurred_at`, never "now".

  * INGEST ORDER, NOT WALL-CLOCK ORDER, DECIDES THE FINAL LEASE. The reader
    replays a workstream's events in `ingest_seq` order and each `progressed`
    or `renewed` event unconditionally OVERWRITES `lease_expires_ts` — live
    traffic gets this for free because a bot cannot report progress before
    it happens, so ingest order already equals chronological order. An
    importer reconstructing history has no such guarantee, so `plan()`
    explicitly sorts every row's verb events by their own `occurred_at`
    before handing them to the caller, mixing progressed/renewed/blocked/
    closed together rather than emitting them in a fixed verb-type order —
    a renewal that chronologically preceded a later progress report must
    ingest first, or the progress report's own (correct, later) derivation
    is silently overwritten by the earlier renewal's value.

Two hazards the R1 gauntlet already found in the WRITE door
(`lib/workstream-update.sh`), reproduced here rather than rediscovered:

  * Archived ids are invisible to the slug dedup. A construct id is unique
    per fleet ON THE PLANE FOREVER — pruning removes a row from the live
    registry but not from the fleet's id-space — so a dedup check that only
    consults the LIVE registry re-mints an archived id and ingest refuses
    it. `plan()`'s *existing* argument is expected to come from
    `workstream_registry(..., or_empty=True)`, whose `archived` list is
    exactly the writer's own carried-forward fix for this.

  * The registry must be materialized INSIDE the caller's lock, never
    before it. Two concurrent writers (a live `workstream-update.sh open`
    racing this import) must dedup against the plane as it is when their
    turn comes, not a snapshot taken before the wait. This module does not
    hold the lock itself — `import_workstreams` in `commands/plane.py` does,
    mirroring `with_lock "$(_ws_lock)" _open_ws` — but `plan()` is written
    pure specifically so the caller can materialize-then-plan atomically
    under the lock without this module reaching for a connection itself.

Idempotence rests on `event_id` being DERIVED from stable content (fleet,
workstream_id, verb, an ordinal, the verb's own occurred_at) — never from
`import_batch`, which is allowed to vary between attempts (this module
derives it from the source file's mtime, stable across a same-file re-run,
but nothing here requires that stability for correctness). `emit_batch`'s
`_finalize` mints a RANDOM event_id for any envelope that omits one, so an
envelope that forgot to set `event_id` would defeat dedup on every field
except this one, silently.

Two fields the file format carries and the import does NOT (#1748 review):
`refs` (issues/prs) and `task_ids`. Unlike `project_key` — dropped only on a
case mismatch, and recoverable by fixing the source value — these are
dropped unconditionally, for every row, because the plane's `Workstream`/
`WorkstreamEvent` wire contract (`plane/contracts.py`) has no field for
either. Widening that contract would affect the LIVE writer too and is out
of scope for a migration tool. `plan()` warns per row when either is
actually non-empty (most real rows carry neither), naming the count, so the
loss is visible rather than inferred from an absent mention.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .ids import derive_uid

EMITTER = "workstream-import"

#: Bounded wait to match the shell writer's own `WITH_LOCK_WAIT_S` default
#: (`lib-common.sh`'s `with_lock`) — a wedged concurrent writer must not hang
#: this one-shot tool silently forever.
LOCK_WAIT_S = 30.0


@contextmanager
def registry_lock(lock_path: Path, *, wait_s: float = LOCK_WAIT_S):
    """Exclusive lock on the SAME name `lib/workstream-update.sh` locks
    (`<fleet runtime>/workstreams.lock`, `_ws_lock`) — but WHICH mechanism
    depends on the host, and it must match the shell's choice or the two
    writers do not exclude each other at all (#1748 review).

    `with_lock` (`lib-common.sh`) resolves `flock` via `command -v flock`
    and, when that is empty, falls back to an mkdir spinlock on a
    DIFFERENT PATH (`<lockfile>.d`) — `_FLOCK_BIN`'s own comment: "resolved
    path to flock (empty on stock macOS)". `fcntl.flock` on `lock_path`
    only interoperates with the shell's FIRST branch: on a host taking the
    fallback, a real `flock(2)` on a file nothing else opens excludes
    nothing, silently, on exactly the platform the shell's own comment
    names. So this resolves `flock` in Python the same way the shell
    resolves it in bash, and takes whichever mechanism a shell writer on
    THIS host would take — never both, never a guess.

    One deliberate divergence, disclosed rather than silent: `with_lock`'s
    own fallback gives up after its budget and runs UNLOCKED ("a waiter
    that gives up runs unlocked" — measured by the R1 gauntlet as the
    correct choice for a small jq+mv critical section). This is a one-shot
    migration into an append-only ledger, where racing an unprotected
    write is worse than asking the operator to retry, so this raises
    TimeoutError instead of proceeding — a stricter EXIT, never a looser
    EXCLUSION.

    The caller is expected to materialize the existing registry AND emit
    its plan's events while holding this — see the module docstring's
    second R1-gauntlet hazard (materialize-before-lock)."""
    import shutil

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("flock"):
        yield from _flock_lock(lock_path, wait_s)
    else:
        yield from _mkdir_lock(lock_path, wait_s)


def _flock_lock(lock_path: Path, wait_s: float):
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire {lock_path} within {wait_s}s"
                        " -- another workstream-update.sh call may be running"
                    ) from None
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _mkdir_lock(lock_path: Path, wait_s: float):
    """The SAME fallback `with_lock` takes when no `flock` binary is on
    PATH: an atomic `mkdir` on `<lock_path>.d` (POSIX guarantees mkdir is
    atomic on every filesystem the shell targets) -- same suffix, same
    parent directory, so a shell writer's spinlock and this one contend
    for the SAME directory rather than two unrelated ones."""
    lockdir = Path(f"{lock_path}.d")
    deadline = time.monotonic() + wait_s
    while True:
        try:
            lockdir.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not acquire {lockdir} within {wait_s}s"
                    " -- another workstream-update.sh call may be running"
                    " (this host has no flock binary, so both writers use"
                    " the mkdir-spinlock fallback)"
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lockdir.rmdir()
        except OSError:
            pass


#: The four statuses the writer's own vocabulary recognises
#: (`lib/workstream-update.sh`'s `--status done|abandoned` on close, plus the
#: two states a row can otherwise be in). Anything else is unrecognised and
#: imported WITHOUT a status-changing event rather than guessed at.
_KNOWN_STATUSES = {"active", "blocked", "done", "abandoned"}


@dataclass
class SkippedRow:
    workstream_id: str
    reason: str


@dataclass
class RowWarning:
    workstream_id: str
    detail: str


@dataclass
class ImportPlan:
    fleet: str
    import_batch: str
    events: list = field(
        default_factory=list
    )  # ready-to-emit envelopes, in ingest order
    skipped: list = field(
        default_factory=list
    )  # SkippedRow: already live/archived on the plane
    warnings: list = field(
        default_factory=list
    )  # RowWarning: unrecognised status, missing opened_ts, etc.


def batch_id(source_mtime: float) -> str:
    """`ws-import-<file mtime as an int epoch>` — stable across a re-run of
    the SAME unmodified file (purely for traceability; `event_id` derivation
    never depends on this, so it carries no correctness weight)."""
    return f"ws-import-{int(source_mtime)}"


def _project_key(raw: Optional[str]) -> Optional[str]:
    """Mirror `lib/workstream-update.sh`'s own gate (`case "$PROJECT" in
    [a-z]*)`, `:274-276`) field for field: a project value that does not
    already match the contract's `^[a-z][a-z0-9-]*$` pattern is DROPPED,
    exactly as the live writer drops it — never normalized. The real file
    on this host carries `"project": "Claudlobby"` (display-cased), which
    the writer itself would also decline to turn into a project_key."""
    if raw and re.match(r"^[a-z][a-z0-9-]*$", raw):
        return raw
    return None


def _plus_days_iso(ts: str, days: int) -> str:
    """The SAME derivation `lib/plane-readers.py::_plus_days` uses on the
    read side. Both `progress` and `renew` compute their lease target as
    call-time + WORKSTREAM_LEASE_DAYS (`_lease_expiry_iso` in the shell
    writer) — the two verbs share one formula — so a `renewals[]` entry's
    `renewed_until` is derivable from its own `ts` alone; the file format
    never stored each renewal's target expiry separately, only the row's
    current top-level `lease_expires_ts`."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        (dt + timedelta(days=days))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _envelope(
    *,
    event_type: str,
    fleet: str,
    wid: str,
    occurred_at: str,
    import_batch: str,
    payload: dict,
    verb: str,
    ordinal: int,
) -> dict:
    # verb/ordinal are the DISCRIMINATOR for event_id, deliberately separate
    # from the wire event_type: "workstream_event" is the same wire name for
    # progressed/renewed/blocked/closed, so using it alone here would collide
    # every verb type on one row at the same instant onto one id.
    material = f"ws-import:{fleet}:{wid}:{verb}:{ordinal}:{occurred_at}"
    return {
        "event_type": event_type,
        "emitter": EMITTER,
        "fleet": fleet,
        "source_ref": f"workstreams:{wid}",
        "occurred_at": occurred_at,
        "origin": "legacy",
        "import_batch": import_batch,
        "event_id": derive_uid("ev", material),
        "payload": payload,
    }


def _row_events(
    wid: str,
    row: dict,
    *,
    fleet: str,
    import_batch: str,
    lease_days: int,
    doc_updated: Optional[str],
    warnings: list,
) -> list:
    opened_ts = row.get("opened_ts")
    if not opened_ts:
        warnings.append(
            RowWarning(
                wid, "no opened_ts — row skipped (nothing to anchor the construct on)"
            )
        )
        return []
    title = row.get("title") or wid
    next_text = row.get("next") or None
    owner_name = row.get("owner_bot") or None
    owner_alias = f"bot:{fleet}/{owner_name}" if owner_name else None
    # opened_by is REQUIRED on the wire and the file never recorded a
    # separate "who ran open" fact distinct from ownership (unlike a live
    # call, which stamps the CALLER's own alias here) — the best available
    # approximation is the row's own owner, falling back to a synthetic
    # import actor only when even that is absent.
    opened_by = owner_alias or f"bot:{fleet}/legacy-import"

    # refs (issues/prs) and task_ids are DROPPED, unconditionally, for every
    # row (#1748 review) -- unlike project_key, which is dropped only on a
    # case mismatch, this one is forced by the target schema: the Workstream
    # /WorkstreamEvent wire contract (contracts.py) has no field for either,
    # for any row, ever. Widening that contract is a live-writer-affecting
    # change and out of scope here. Warn only when there is something real
    # to lose -- most real rows carry empty refs/task_ids, and a warning on
    # every row would train an operator to stop reading them.
    refs = row.get("refs") or {}
    dropped = []
    if refs.get("issues"):
        dropped.append(f"refs.issues ({len(refs['issues'])})")
    if refs.get("prs"):
        dropped.append(f"refs.prs ({len(refs['prs'])})")
    if row.get("task_ids"):
        dropped.append(f"task_ids ({len(row['task_ids'])})")
    if dropped:
        warnings.append(
            RowWarning(
                wid,
                f"{', '.join(dropped)} present in the file but dropped --"
                " the plane's workstream contract has no field for either",
            )
        )

    events = [
        _envelope(
            event_type="workstream",
            fleet=fleet,
            wid=wid,
            occurred_at=opened_ts,
            import_batch=import_batch,
            verb="opened",
            ordinal=0,
            payload={
                "workstream_id": wid,
                "title": title,
                "opened_by": opened_by,
                **({"owner": owner_alias} if owner_alias else {}),
                **({"goal": next_text} if next_text else {}),
                **(
                    {"project_key": pk}
                    if (pk := _project_key(row.get("project")))
                    else {}
                ),
            },
        )
    ]

    # Every verb event this row implies, collected then sorted by its own
    # occurred_at so ingest order matches chronological order — see the
    # module docstring's ordering rule.
    verbs: list = []  # (occurred_at, verb, ordinal, payload)
    last_progress = row.get("last_progress_ts")
    if last_progress and last_progress != opened_ts:
        verbs.append(
            (
                last_progress,
                "progressed",
                0,
                {
                    "workstream_id": wid,
                    "event": "progressed",
                    **({"next_step": next_text} if next_text else {}),
                },
            )
        )

    for i, renewal in enumerate(row.get("renewals") or []):
        ts = renewal.get("ts")
        if not ts:
            warnings.append(RowWarning(wid, f"renewals[{i}] has no ts — skipped"))
            continue
        note = renewal.get("note") or None
        verbs.append(
            (
                ts,
                "renewed",
                i,
                {
                    "workstream_id": wid,
                    "event": "renewed",
                    "renewed_until": _plus_days_iso(ts, lease_days),
                    **({"note": note} if note else {}),
                },
            )
        )

    status = row.get("status")
    if status == "blocked":
        at = last_progress or doc_updated or opened_ts
        verbs.append(
            (
                at,
                "blocked",
                0,
                {
                    "workstream_id": wid,
                    "event": "blocked",
                    **({"note": next_text} if next_text else {}),
                },
            )
        )
    elif status in ("done", "abandoned"):
        at = row.get("closed_ts") or doc_updated or last_progress or opened_ts
        verbs.append(
            (
                at,
                "closed",
                0,
                {
                    "workstream_id": wid,
                    "event": "closed",
                    "disposition": status,
                },
            )
        )
    elif status not in _KNOWN_STATUSES and status is not None:
        warnings.append(
            RowWarning(
                wid,
                f"unrecognised status {status!r} — imported with no"
                " status-changing event (will render 'active')",
            )
        )

    verbs.sort(key=lambda t: t[0])
    for at, verb, ordinal, payload in verbs:
        events.append(
            _envelope(
                event_type="workstream_event",
                fleet=fleet,
                wid=wid,
                occurred_at=at,
                import_batch=import_batch,
                verb=verb,
                ordinal=ordinal,
                payload=payload,
            )
        )
    return events


def plan(
    file_doc: dict,
    existing: dict,
    *,
    fleet: str,
    import_batch: str,
    lease_days: int = 14,
) -> ImportPlan:
    """Pure — no db, no emit — so the plan is testable and `--dry-run`
    prints exactly what a real run would send.

    *file_doc* is the parsed `workstreams.json` (`{"updated": ..., "workstreams":
    {id: {...}}}`). *existing* is the plane's CURRENT registry for this
    fleet, already fetched with `or_empty=True` — a dict carrying
    `workstreams` (live ids) and `archived` (pruned ids); both count for
    dedup (R1 gauntlet hazard 1). The caller is responsible for fetching
    *existing* under the same lock this plan's events will be emitted
    under (R1 gauntlet hazard 2) — this function touches no connection
    itself, deliberately, so it stays a pure function of its two arguments.
    """
    known = set((existing or {}).get("workstreams", {})) | set(
        (existing or {}).get("archived", [])
    )
    out = ImportPlan(fleet=fleet, import_batch=import_batch)
    doc_updated = file_doc.get("updated") or None
    for wid, row in (file_doc.get("workstreams") or {}).items():
        if wid in known:
            out.skipped.append(
                SkippedRow(wid, "already on the plane (live or archived)")
            )
            continue
        out.events.extend(
            _row_events(
                wid,
                row,
                fleet=fleet,
                import_batch=import_batch,
                lease_days=lease_days,
                doc_updated=doc_updated,
                warnings=out.warnings,
            )
        )
    return out
