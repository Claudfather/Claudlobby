"""Pure queue paths and inventory shared by runtime and migration readers.

Never create a queue, claim a file, replay data or import producer contracts.
"""

from dataclasses import dataclass
from pathlib import Path
import stat

from .db import db_file


def spool_path(root: Path) -> Path:
    return db_file(root).parent / "spool"


def staged_dir(root: Path) -> Path:
    """Raw shim batches; existence is the daemon's staging handshake."""
    return db_file(root).parent / "staged"


def staged_payload(path: Path) -> bool:
    """Pending stage names, including temp writes that may age into replay."""
    return path.name.endswith((".batch", ".tmp"))


@dataclass
class SpoolScan:
    """One state-bearing enumeration of the spool tree — THE definition the
    trust panel, `plane status`, and `plane doctor` all consume, so the
    three surfaces cannot disagree about the same directory (external round
    4, probed: doctor printed '[ok] spool depth — 0' at rc 0 for the exact
    tree /api/trust called unreadable). Side-effect-free by construction —
    pure path joins, never spool_dir()/quarantine_dir(), which mkdir for
    writer callers. States: "ok" (absent counts as ok — the spool is
    lazily created, so absence IS zero pending) or "unreadable" (the count
    lists are withheld: a number from a tree that could not be fully
    enumerated is the green-zero lie). Name filters mirror the glob
    patterns they replaced — parity verified externally on APFS and ext4."""

    spool_state: str
    pending: list[Path]
    inflight: list[Path]
    quarantine_state: str
    quarantined: list[Path]


def scan_queue_dir(path: Path):
    """A missing queue is empty; a wrong node or redirected queue is not.

    Generic source readers classify ENOTDIR as absence. Queue drain/migration
    cannot: an existing file at the queue path blocks enumeration and writes.
    Keep the existing materialized readdir owner after checking that boundary.
    """
    from ..source_state import SOURCE_ABSENT, SOURCE_UNREADABLE, SourceProbe, scan_dir
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return SourceProbe(SOURCE_ABSENT, path), []
    except OSError:
        return SourceProbe(SOURCE_UNREADABLE, path), []
    if not stat.S_ISDIR(mode):
        return SourceProbe(SOURCE_UNREADABLE, path), []
    return scan_dir(path)


def scan_spool(root: Path) -> SpoolScan:
    from ..source_state import SOURCE_OK, SOURCE_UNREADABLE

    sp = spool_path(root)
    probe, entries = scan_queue_dir(sp)
    if probe.state == SOURCE_UNREADABLE:
        spool_state, pending, inflight = "unreadable", [], []
    else:
        spool_state = "ok"
        names = entries if probe.state == SOURCE_OK else []
        pending = sorted(e for e in names if e.name.endswith(".json"))
        inflight = sorted(e for e in names if ".json.inflight." in e.name)
    qprobe, qentries = scan_queue_dir(sp / "quarantine")
    if qprobe.state == SOURCE_UNREADABLE:
        quarantine_state, quarantined = "unreadable", []
    else:
        quarantine_state = "ok"
        qnames = qentries if qprobe.state == SOURCE_OK else []
        quarantined = sorted(e for e in qnames if e.name.endswith(".json"))
    return SpoolScan(spool_state, pending, inflight,
                     quarantine_state, quarantined)
