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
from .queue_paths import scan_queue_dir, staged_dir, staged_payload

#: A pending batch older than this, beside a serving daemon, means replay is
#: not keeping up or is paused (a replay tick is ~1 s; a paused one 30 s).
STAGED_STALE_S = 300.0


@dataclass
class StagedScan:
    """state: "ok" (absent counts as empty) or "unreadable" (counts withheld:
    a number from a queue that could not be enumerated is a green-zero lie)."""

    state: str
    count: int = 0
    size: int = 0
    oldest_mtime: float | None = None

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
    return scan


def staged_rung(scan: StagedScan, serving: bool) -> tuple[bool, str]:
    """(ok, detail) for `plane doctor`'s staged-depth rung. Non-empty is
    ATTENTION when no daemon serves (nothing will replay it), when the queue
    is at its bound (emits are being refused), or when its oldest batch is
    stale beside a serving daemon."""
    if scan.state == "unreadable":
        return False, "UNREADABLE — cannot enumerate (a gap, not a zero)"
    if not scan.count:
        return True, "0 pending"
    age = int(max(0.0, time.time() - (scan.oldest_mtime or time.time())))
    detail = (f"{scan.count} pending ({scan.size} bytes, oldest {age}s)"
              f" — bound {STAGED_MAX_BATCHES} batches / {STAGED_MAX_BYTES} bytes")
    if scan.full:
        return False, detail + (" — FULL: new emits are refused and counted in"
                                " .emit-losses until the daemon replays it")
    if not serving:
        return False, detail + (" — NOT recorded: only a serving plane daemon"
                                " replays this queue")
    if age > STAGED_STALE_S:
        return False, detail + (" — replay is not keeping up or is paused; see"
                                " the daemon log")
    return True, detail + " — daemon replaying"


def staged_summary(root: Path) -> dict:
    """JSON-safe queue observation; unreadable counts are withheld."""
    scan = scan_staged(root)
    readable = scan.state == "ok"
    return {"state": scan.state, "pending": scan.count if readable else None,
            "bytes": scan.size if readable else None,
            "oldest_age_s": (int(max(0, time.time() - scan.oldest_mtime))
                             if readable and scan.oldest_mtime is not None else None),
            "full": scan.full if readable else None}
