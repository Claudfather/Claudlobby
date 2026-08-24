"""Filesystem spool — the valve that must not depend on the db it protects
(design v2 §10). Plain JSON files, atomic tmp+rename, drained by plane
status/doctor or any caller; deletion only after committed ingest; poison
records quarantined with their reason, never silently dropped, never
retried forever.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ContractViolation, validate_request
from .ingest import ingest_many  # patched in tests; keep module-level name

MAX_ATTEMPTS = 5


class SpoolWriteError(RuntimeError):
    """db failed AND the spool write failed — total emit failure (exit 3)."""


def spool_dir(root: Path) -> Path:
    p = Path(root) / "state" / "plane" / "spool"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def quarantine_dir(root: Path) -> Path:
    p = spool_dir(root) / "quarantine"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _write_bytes_secure(directory: Path, name: str, data: bytes) -> Path:
    """THE one spool byte-writer (round-3/4 F6): 0600, atomic tmp+rename,
    fsync file AND directory — entries and reason sidecars alike."""
    target = directory / name
    tmp = directory / (name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return target


RETRYABLE_SQLITE_CODES = frozenset({
    5,   # SQLITE_BUSY
    6,   # SQLITE_LOCKED
    8,   # SQLITE_READONLY  (transient perms; also the e2e test's class)
    10,  # SQLITE_IOERR
    13,  # SQLITE_FULL
    14,  # SQLITE_CANTOPEN
})


_RETRYABLE_MESSAGES = (
    # The code-less fallback (Python 3.10, synthetic exceptions): match the
    # KNOWN infrastructure classes; anything else is presumed a bug and
    # quarantines loudly (round-5 F6 — retry-everything blessed SQL bugs).
    "database is locked",
    "database table is locked",
    "database or disk is full",
    "disk i/o error",
    "unable to open database",
    "attempt to write a readonly database",
)


def is_retryable(exc: sqlite3.OperationalError) -> bool:
    """Whitelist by SQLite primary error code; when no code exists (3.10 or a
    synthetic exception), fall back to message-matching the known infra
    classes — never retry-by-default."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in RETRYABLE_SQLITE_CODES
    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_MESSAGES)


def _write_entry_file(directory: Path, name: str, entry: dict) -> Path:
    return _write_bytes_secure(
        directory, name, (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode()
    )


def spool_write(root: Path, finalized_requests: list[dict], error: str) -> Path:
    """Persist an already-finalized batch (event_ids + occurred_at set by emit
    BEFORE the first db attempt — F6). fsync file AND directory before
    returning: a spool 'success' that evaporates on power loss is a lost
    event wearing a receipt (round-2 F6)."""
    lead = finalized_requests[0]["event_id"]
    entry = {
        "event_ids": [r["event_id"] for r in finalized_requests],
        "spooled_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "attempts": 0,
        "requests": finalized_requests,
    }
    try:
        return _write_entry_file(spool_dir(root), f"{lead}.json", entry)
    except OSError as exc:
        raise SpoolWriteError(f"db failed ({error}) AND spool failed ({exc})") from exc


def spool_entries(root: Path) -> list[dict]:
    out = []
    for f in sorted(spool_dir(root).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            data = {"event_ids": None, "spooled_at": None, "attempts": None}
        data["_file"] = f.name
        out.append(data)
    return out


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def quarantine_entry(root: Path, f: Path, reason: str) -> None:
    """THE quarantine door — drain and the operator CLI both use it.
    Round-5 F6: a cross-directory rename dirties BOTH directories; fsync
    source AND destination, or a crash can resurrect the entry in spool
    (double-processing) or lose it from quarantine."""
    q = quarantine_dir(root)
    _write_bytes_secure(q, f.name + ".reason", (reason + "\n").encode())
    os.chmod(f, 0o600)          # a malformed file arrived at ITS creator's mode
    os.replace(f, q / f.name)
    _fsync_dir(q)
    _fsync_dir(f.parent)


def _quarantine(root: Path, f: Path, reason: str) -> None:
    quarantine_entry(root, f, reason)


@dataclass(frozen=True)
class DrainReport:
    ingested: int = 0
    duplicates: int = 0
    quarantined: int = 0
    remaining: int = 0


def drain(root: Path, conn: sqlite3.Connection, host_uid: str) -> DrainReport:
    ingested = duplicates = quarantined = 0
    entries = []
    for f in spool_dir(root).glob("*.json"):
        try:
            data = json.loads(f.read_text())
            entries.append((data.get("spooled_at") or "", f, data))
        except (json.JSONDecodeError, OSError) as exc:
            _quarantine(root, f, f"malformed spool file: {exc}")
            quarantined += 1
    for _, f, entry in sorted(entries, key=lambda e: (e[0], e[1].name)):
        try:
            raws = entry["requests"]
        except (KeyError, TypeError) as exc:
            _quarantine(root, f, f"malformed spool entry: {exc}")
            quarantined += 1
            continue
        try:
            items = [validate_request(r) for r in raws]
        except ContractViolation as exc:
            _quarantine(root, f, f"contract violation on drain: {exc}")
            quarantined += 1
            continue
        try:
            results = ingest_many(conn, items, host_uid=host_uid)
        except sqlite3.OperationalError as exc:
            if not is_retryable(exc):
                # Missing table / SQL typo are OperationalError too — bugs,
                # not infrastructure (round-4 F6).
                _quarantine(root, f, f"non-retryable operational: {exc}")
                quarantined += 1
                continue
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["error"] = str(exc)
            if entry["attempts"] >= MAX_ATTEMPTS:
                _quarantine_with(root, f, entry, f"retries exhausted: {exc}")
                quarantined += 1
            else:
                _write_entry_file(spool_dir(root), f.name, entry)
            continue
        except Exception as exc:  # noqa: BLE001 — integrity/programming: poison
            _quarantine(root, f, f"non-retryable on drain: {exc}")
            quarantined += 1
            continue
        if all(r.duplicate for r in results):
            duplicates += 1
        else:
            ingested += 1
        f.unlink()  # only after committed ingestion
    remaining = len(list(spool_dir(root).glob("*.json")))
    return DrainReport(ingested, duplicates, quarantined, remaining)


def _quarantine_with(root: Path, f: Path, entry: dict, reason: str) -> None:
    _write_entry_file(spool_dir(root), f.name, entry)
    _quarantine(root, f, reason)
