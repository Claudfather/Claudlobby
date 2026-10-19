"""The staged queue's health reading (S5a-02).

`state/plane/staged` is the only record a hook or timer has while the daemon
cannot answer, and only a serving daemon replays it. A doctor that counted the
spool but not this queue reported recording healthy while every event piled up
unrecorded. Side-effect-free: never creates, claims or replays a file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .capture_policy import STAGED_MAX_BATCHES, STAGED_MAX_BYTES
from .queue_paths import (STAGED_ORPHAN_AGE_S, scan_queue_dir, staged_dir, staged_orphan,
                          staged_payload)

#: A pending batch older than this, beside a serving daemon, means replay is
#: not keeping up or is paused (a replay tick is ~1 s; a paused one 30 s). An
#: orphaned stage waits STAGED_ORPHAN_AGE_S for its replay by design, and is
#: late only this long past that: measured, they leave within ~50 s (#2086).
STAGED_STALE_S = 300.0


@dataclass
class StagedScan:
    """state: "ok" (absent counts as empty) or "unreadable" (counts withheld:
    a number from a queue that could not be enumerated is a green-zero lie).
    The count, size and oldest cover every stage, as the client's bound does.
    `orphans` of them are temp stages (`staged_orphan`), replayed only at the
    hour, so their age is kept apart from the batches' (#2086)."""

    state: str
    count: int = 0
    size: int = 0
    oldest_mtime: float | None = None
    orphans: int = 0
    oldest_batch_mtime: float | None = None
    oldest_orphan_mtime: float | None = None

    @property
    def full(self) -> bool:
        return self.count >= STAGED_MAX_BATCHES or self.size >= STAGED_MAX_BYTES


def scan_staged(root: Path) -> StagedScan:
    from ..source_state import SOURCE_UNREADABLE

    probe, entries = scan_queue_dir(staged_dir(root))
    if probe.state == SOURCE_UNREADABLE:
        return StagedScan("unreadable")
    scan = StagedScan("ok")
    for entry in entries:
        if not staged_payload(entry):
            continue
        try:
            st = entry.lstat()
        except FileNotFoundError:
            continue                        # replayed since the listing
        except OSError:
            return StagedScan("unreadable")
        scan.count += 1
        scan.size += st.st_size
        if scan.oldest_mtime is None or st.st_mtime < scan.oldest_mtime:
            scan.oldest_mtime = st.st_mtime
        if staged_orphan(entry):
            scan.orphans += 1
            if scan.oldest_orphan_mtime is None or st.st_mtime < scan.oldest_orphan_mtime:
                scan.oldest_orphan_mtime = st.st_mtime
        elif scan.oldest_batch_mtime is None or st.st_mtime < scan.oldest_batch_mtime:
            scan.oldest_batch_mtime = st.st_mtime
    return scan


def staged_rung(scan: StagedScan, serving: bool) -> tuple[bool, str]:
    """(ok, detail) for `plane doctor`'s staged-depth rung. Non-empty is
    ATTENTION when no daemon serves (nothing will replay it), when the queue
    is at its bound (emits are being refused), when its oldest batch is
    stale beside a serving daemon, or when an orphaned stage is STAGED_STALE_S
    past its replay at the hour. Before that, its wait is the daemon's rule,
    not a stalled replay (#2086)."""
    if scan.state == "unreadable":
        return False, "UNREADABLE — cannot enumerate (a gap, not a zero)"
    if not scan.count:
        return True, "0 pending"
    now = time.time()
    age = int(max(0.0, now - (scan.oldest_mtime or now)))
    detail = (f"{scan.count} pending ({scan.size} bytes, oldest {age}s)"
              f" — bound {STAGED_MAX_BATCHES} batches / {STAGED_MAX_BYTES} bytes")
    orphan_age = int(max(0.0, now - (scan.oldest_orphan_mtime or now)))
    if scan.orphans:
        detail += (f"; {scan.orphans} of them orphaned stage(s) (a write never renamed),"
                   f" replayed once {STAGED_ORPHAN_AGE_S:.0f}s old (oldest {orphan_age}s)")
    if scan.full:
        return False, detail + (" — FULL: new emits are refused and counted in"
                                " .emit-losses until the daemon replays it")
    if not serving:
        return False, detail + (" — NOT recorded: only a serving plane daemon"
                                " replays this queue")
    batch_age = int(max(0.0, now - (scan.oldest_batch_mtime or now)))
    if batch_age > STAGED_STALE_S:
        return False, detail + (" — replay is not keeping up or is paused; see"
                                " the daemon log")
    if orphan_age > STAGED_ORPHAN_AGE_S + STAGED_STALE_S:
        return False, detail + (f" — an orphaned stage is {orphan_age - STAGED_ORPHAN_AGE_S:.0f}s"
                                " past its replay at the hour; see the daemon log")
    return True, detail + " — daemon replaying"


def staged_summary(root: Path) -> dict:
    """JSON-safe queue observation; unreadable counts are withheld."""
    scan = scan_staged(root)
    readable = scan.state == "ok"
    return {"state": scan.state, "pending": scan.count if readable else None,
            "bytes": scan.size if readable else None,
            "oldest_age_s": (int(max(0, time.time() - scan.oldest_mtime))
                             if readable and scan.oldest_mtime is not None else None),
            "full": scan.full if readable else None,
            # Of `pending`, the orphaned stages the daemon replays at the hour (#2086).
            "orphaned": scan.orphans if readable else None}


#: `.emit-losses` rows counted by the summary below: the last day, by each
#: row's own epoch (its writers rotate it on that window, with some slack).
EMIT_LOSS_WINDOW_S = 86400


def emit_losses_summary(root: Path, now: float | None = None) -> dict:
    """`state/plane/.emit-losses` read once for every surface that shows it (#2165):
    `plane doctor`, `plane status` and the brief, so they cannot disagree about
    the same file.

    Rows are `<epoch>\t<kind>\t<door>\t<detail>` (lib-common's plane_emit_loss,
    the socket client's refusals, the daemon's #2164 replay). `reap` is an emit
    whose fate is unknown; every other kind is a known loss, NOT recorded:
    `stage_empty`, `staged_full`, `stage_refused`, `stage_failed`. A row whose
    epoch cannot be read stays in the window: it cannot be shown to be old.

    Absent is zero: the file is created on the first loss. Unreadable withholds
    every count (None): a counter that cannot be read is a gap, not a zero.
    Never creates, rotates or rewrites the file.
    """
    now = time.time() if now is None else now
    path = root / "state" / "plane" / ".emit-losses"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        return {"state": "unreadable", "error": type(exc).__name__, "window_s": EMIT_LOSS_WINDOW_S,
                "reaped": None, "reap_doors": None, "not_recorded": None, "not_recorded_total": None}
    reaped, doors, kinds, total = 0, set(), {}, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        try:
            if int(fields[0]) < now - EMIT_LOSS_WINDOW_S:
                continue
        except ValueError:
            pass
        if len(fields) > 1 and fields[1] == "reap":
            reaped += 1
            if len(fields) > 2:
                doors.add(fields[2])
            continue
        total += 1
        if len(fields) > 1:
            kinds[fields[1]] = kinds.get(fields[1], 0) + 1
    return {"state": "ok", "window_s": EMIT_LOSS_WINDOW_S, "reaped": reaped,
            "reap_doors": sorted(doors), "not_recorded": dict(sorted(kinds.items())),
            "not_recorded_total": total}

