"""Filesystem spool — the valve that must not depend on the db it protects
(design v2 §10). Plain JSON files, atomic tmp+rename, drained by plane
status/doctor or any caller; deletion only after committed ingest; poison
records quarantined with their reason, never silently dropped, never
retried forever.

Concurrency: a drainer CLAIMS an entry by renaming it to a per-pid inflight
name before reading it — two drainers partition the set instead of racing
read-then-unlink. A crash mid-claim leaves an inflight file whose pid is
dead; the next drain recovers it (rename back if the original is absent,
drop it if a retry rewrite already landed a newer original). Replay is
idempotent, so reprocessing a recovered entry classifies as duplicate.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ContractViolation, validate_request
from .ingest import ingest_many  # patched in tests; keep module-level name

MAX_ATTEMPTS = 5
HISTORY_LIMIT = 5


class SpoolWriteError(RuntimeError):
    """db failed AND the spool write failed — total emit failure (exit 3)."""


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_fsynced(p: Path, mode: int | None) -> None:
    """Create one directory level durably: a newly created child is a dirty
    entry in its PARENT, so the parent gets the fsync — fsyncing only the
    leaf leaves the first-failure spool tree itself un-durable (a receipt
    that can evaporate with the directory that holds it)."""
    try:
        p.mkdir()
        created = True
    except FileExistsError:
        created = False
    if mode is not None:
        os.chmod(p, mode)
    if created:
        _fsync_dir(p.parent)


def spool_dir(root: Path) -> Path:
    state = Path(root) / "state"
    plane = state / "plane"
    spool = plane / "spool"
    _mkdir_fsynced(state, None)
    _mkdir_fsynced(plane, 0o700)
    _mkdir_fsynced(spool, 0o700)
    return spool


def quarantine_dir(root: Path) -> Path:
    q = spool_dir(root) / "quarantine"
    _mkdir_fsynced(q, 0o700)
    return q


def _write_all(fd: int, data: bytes) -> None:
    """os.write may return a SHORT count (signal, pipe pressure, quota edge);
    a single call that succeeds partially is a truncated file wearing a
    durable-success receipt. Loop over a memoryview until every byte is down."""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except InterruptedError:
            continue
        if n <= 0:
            raise OSError(f"os.write returned {n} with {len(view)} bytes left")
        view = view[n:]


def _write_bytes_secure(directory: Path, name: str, data: bytes) -> Path:
    """THE one spool byte-writer (round-3/4 F6): 0600, atomic tmp+rename,
    fsync file AND directory — entries and reason sidecars alike. The tmp
    name is unique per write and opened O_EXCL|O_NOFOLLOW with an fchmod:
    a predictable shared tmp path let a pre-existing 0644 file (or symlink)
    be adopted and published at ITS mode."""
    target = directory / name
    tmp = directory / f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)      # enforce regardless of umask
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.replace(tmp, target)
    _fsync_dir(directory)
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


# The CONTENTION subset of the retryable classes: a lock another writer holds
# right now, which a short in-process pause resolves. Every other retryable
# class (readonly, I/O, full, cannot-open) is an infrastructure fault and
# spools at once — a pause would only delay the record.
TRANSIENT_LOCK_CODES = frozenset({5, 6})          # SQLITE_BUSY, SQLITE_LOCKED
_TRANSIENT_LOCK_MESSAGES = ("database is locked", "database table is locked")


def is_transient_lock(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in TRANSIENT_LOCK_CODES
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_LOCK_MESSAGES)


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
    """Persist an already-finalized batch (event_ids + occurred_at +
    schema_version set by emit BEFORE the first db attempt — F6, §10). fsync
    file AND directory before returning: a spool 'success' that evaporates on
    power loss is a lost event wearing a receipt (round-2 F6)."""
    lead = finalized_requests[0]["event_id"]
    entry = {
        "event_ids": [r["event_id"] for r in finalized_requests],
        "spooled_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "attempts": 0,
        "history": [],
        "requests": finalized_requests,
    }
    try:
        return _write_entry_file(spool_dir(root), f"{lead}.json", entry)
    except OSError as exc:
        raise SpoolWriteError(f"db failed ({error}) AND spool failed ({exc})") from exc


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


def scan_spool(root: Path) -> SpoolScan:
    from ..source_state import SOURCE_OK, SOURCE_UNREADABLE, scan_dir

    sp = Path(root) / "state" / "plane" / "spool"
    probe, entries = scan_dir(sp)
    if probe.state == SOURCE_UNREADABLE:
        spool_state, pending, inflight = "unreadable", [], []
    else:
        spool_state = "ok"
        names = entries if probe.state == SOURCE_OK else []
        pending = sorted(e for e in names if e.name.endswith(".json"))
        inflight = sorted(e for e in names if ".json.inflight." in e.name)
    qprobe, qentries = scan_dir(sp / "quarantine")
    if qprobe.state == SOURCE_UNREADABLE:
        quarantine_state, quarantined = "unreadable", []
    else:
        quarantine_state = "ok"
        qnames = qentries if qprobe.state == SOURCE_OK else []
        quarantined = sorted(e for e in qnames if e.name.endswith(".json"))
    return SpoolScan(spool_state, pending, inflight,
                     quarantine_state, quarantined)


def oldest_spooled_at(paths: list[Path]) -> str | None:
    """Oldest `spooled_at` across pending entries — the one reader of that
    field, shared by the trust panel and the CLI surfaces."""
    oldest = None
    for f in paths:
        try:
            at = json.loads(f.read_text()).get("spooled_at")
        except (OSError, json.JSONDecodeError):
            continue
        if at and (oldest is None or at < oldest):
            oldest = at
    return oldest


def spool_entries(root: Path) -> list[dict]:
    out = []
    for f in sorted(spool_dir(root).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            data = None
        if not isinstance(data, dict):
            data = {"event_ids": None, "spooled_at": None, "attempts": None}
        data["_file"] = f.name
        out.append(data)
    return out


def quarantine_entry(root: Path, f: Path, reason: str, as_name: str | None = None) -> None:
    """THE quarantine door — drain and the operator CLI both use it.
    Round-5 F6: a cross-directory rename dirties BOTH directories; fsync
    source AND destination, or a crash can resurrect the entry in spool
    (double-processing) or lose it from quarantine. `as_name` restores the
    original basename when the source is a claimed (inflight-renamed) file."""
    q = quarantine_dir(root)
    name = as_name or f.name
    _write_bytes_secure(q, name + ".reason", (reason + "\n").encode())
    os.chmod(f, 0o600)          # a malformed file arrived at ITS creator's mode
    os.replace(f, q / name)
    _fsync_dir(q)
    _fsync_dir(f.parent)


@dataclass(frozen=True)
class DrainReport:
    ingested: int = 0
    duplicates: int = 0
    quarantined: int = 0
    remaining: int | None = 0
    processed: int = 0
    payload_bytes: int = 0
    budget_exhausted: bool = False
    more_work: bool = False
    release_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrainBudget:
    max_entries: int = 64
    max_bytes: int = 8 * 1024 * 1024
    max_elapsed_s: float = 0.1

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.max_entries, self.max_bytes)):
            raise ValueError("drain entry and byte budgets must be positive integers")
        if (isinstance(self.max_elapsed_s, bool)
                or not isinstance(self.max_elapsed_s, (int, float))
                or not math.isfinite(self.max_elapsed_s) or self.max_elapsed_s <= 0):
            raise ValueError("drain time budget must be finite and positive")


class RecoveryCursor:
    """Transient directory position: skipped live claims cannot starve later files.

    Reuse across bounded passes; no queue authority or durable state is added.
    A pass which releases admitted files rewinds so those names are revisited.
    Retry eligibility lasts until the caller's next normal recovery interval,
    independently of rewinds for healthy backlog. Failed claim releases remain
    owned by this cursor; closing an iterator must never forget them.
    """
    def __init__(self):
        self.iterator = None
        self.directory = None
        self.deferred = None
        self.deferred_retries: set[Path] = set()
        self.pending_releases: dict[Path, Path] = {}

    def close(self):
        if self.iterator is not None:
            self.iterator.close()
        self.iterator = self.directory = self.deferred = None

    def next(self, directory: Path) -> Path | None:
        if self.directory != directory:
            self.close()
            self.directory = directory
            self.iterator = os.scandir(directory)
        if self.deferred is not None:
            value, self.deferred = self.deferred, None
            return value
        try:
            return Path(next(self.iterator).path)
        except StopIteration:
            self.close()
            return None

    def defer(self, path: Path):
        self.deferred = path

    def clear_deferred_retries(self):
        """Start a normal retry interval, never a fast backlog continuation."""
        self.deferred_retries.clear()
        self.close()

    def release_claims(self) -> tuple[bool, tuple[str, ...]]:
        """Try EVERY owned release, retaining failed ones for the next pass."""
        released = False
        errors = []
        for claimed, original in list(self.pending_releases.items()):
            try:
                # Ingest/quarantine may already have consumed this claim.
                # stat, unlike exists(), does not turn an I/O error into absence.
                try:
                    claimed.stat()
                except FileNotFoundError:
                    del self.pending_releases[claimed]
                    continue
                if original.exists():
                    claimed.unlink()  # the retry rewrite published newer content
                else:
                    os.rename(claimed, original)
                del self.pending_releases[claimed]
                released = True
            except OSError as exc:
                message = f"{claimed.name}: {exc}"
                errors.append(message)
                print(f"plane recovery: claim release failed: {message}", file=sys.stderr)
        if released:
            self.close()
        return released, tuple(errors)


class SpoolReleaseError(OSError):
    """A caller without a reusable cursor must retain this one to retry cleanup."""
    def __init__(self, cursor: RecoveryCursor, errors: tuple[str, ...]):
        super().__init__("spool claim release failed: " + "; ".join(errors))
        self.cursor = cursor


def _read_claimed_json(path: Path, size: int):
    # A checked finite read detects growth after admission. The first envelope
    # may exceed the byte target: largest-envelope memory/latency is NOT capped,
    # even for malformed JSON. A fixed ceiling needs a separate input contract.
    with path.open("rb") as stream:
        body = stream.read(size + 1)
    if len(body) > size:
        raise ValueError("recovery file grew after size admission")
    return json.loads(body)


def _spool_envelope_problem(data) -> str | None:
    """Shape-check the spool envelope BEFORE any field access: a non-object
    top level crashed the whole drain, and an empty requests list read as a
    vacuous all-duplicates and was silently deleted."""
    if not isinstance(data, dict):
        return f"top level must be an object, got {type(data).__name__}"
    reqs = data.get("requests")
    if not isinstance(reqs, list):
        return f"requests must be a list, got {type(reqs).__name__}"
    if not reqs:
        return "requests is empty — nothing this entry could replay"
    bad = next((r for r in reqs if not isinstance(r, dict)), None)
    if bad is not None:
        return f"request members must be objects, got {type(bad).__name__}"
    return None


_INFLIGHT_RE = re.compile(r"\.inflight\.(\d+)\.[0-9a-f]+$")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim(f: Path) -> Path | None:
    """Rename-to-claim: exactly one drainer wins; the loser sees the original
    gone and skips instead of dying on read/unlink of a vanished path."""
    claimed = f.with_name(f"{f.name}.inflight.{os.getpid()}.{os.urandom(4).hex()}")
    try:
        os.rename(f, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _recover_stale_inflight(sd: Path) -> None:
    """A drainer that died mid-claim leaves an inflight file. Only DEAD-pid
    claims are touched — a live drainer's claim is its property. If the
    original name exists again, a retry rewrite landed newer content and the
    stale claim is the older copy (drop it); otherwise rename back and let
    idempotent replay classify it."""
    for f in sd.glob("*.json.inflight.*"):
        m = _INFLIGHT_RE.search(f.name)
        if not m or _pid_alive(int(m.group(1))):
            continue
        orig = sd / f.name[: f.name.index(".inflight.")]
        try:
            if orig.exists():
                f.unlink()
            else:
                os.rename(f, orig)
        except OSError:
            continue
    _fsync_dir(sd)


def _ingest_claim(root, conn, host_uid, orig_name, claimed, entry):
    """The shared complete-envelope transaction and retry policy."""
    sd = spool_dir(root)
    try:
        items = [validate_request(r) for r in entry["requests"]]
    except ContractViolation as exc:
        quarantine_entry(root, claimed, f"contract violation on drain: {exc}", as_name=orig_name)
        return "quarantined"
    try:
        results = ingest_many(conn, items, host_uid=host_uid)
    except sqlite3.OperationalError as exc:
        if not is_retryable(exc):
            # Missing table / SQL typo are OperationalError too — bugs,
            # not infrastructure (round-4 F6).
            quarantine_entry(root, claimed, f"non-retryable operational: {exc}", as_name=orig_name)
            return "quarantined"
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["error"] = str(exc)
        history = entry.get("history")
        if not isinstance(history, list):
            history = []
        history.append({"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)})
        entry["history"] = history[-HISTORY_LIMIT:]
        if entry["attempts"] >= MAX_ATTEMPTS:
            _write_entry_file(sd, claimed.name, entry)
            quarantine_entry(root, sd / claimed.name, f"retries exhausted: {exc}", as_name=orig_name)
            return "quarantined"
        else:
            _write_entry_file(sd, orig_name, entry)
            claimed.unlink()
        return "retry"
    except Exception as exc:  # noqa: BLE001 — integrity/programming: poison
        quarantine_entry(root, claimed, f"non-retryable on drain: {exc}", as_name=orig_name)
        return "quarantined"
    if all(r.duplicate for r in results):
        verdict = "duplicates"
    else:
        verdict = "ingested"
    claimed.unlink()  # only after committed ingestion
    return verdict


def drain(root: Path, conn: sqlite3.Connection, host_uid: str) -> DrainReport:
    sd = spool_dir(root)
    _recover_stale_inflight(sd)
    ingested = duplicates = quarantined = 0
    claims: list[tuple[str, Path]] = []
    for f in sorted(sd.glob("*.json")):
        claimed = _claim(f)
        if claimed is not None:
            claims.append((f.name, claimed))
    entries = []
    for orig_name, claimed in claims:
        try:
            data = json.loads(claimed.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            quarantine_entry(root, claimed, f"malformed spool file: {exc}", as_name=orig_name)
            quarantined += 1
            continue
        problem = _spool_envelope_problem(data)
        if problem is not None:
            quarantine_entry(root, claimed, f"malformed spool entry: {problem}", as_name=orig_name)
            quarantined += 1
            continue
        entries.append((data.get("spooled_at") or "", orig_name, claimed, data))
    for _, orig_name, claimed, entry in sorted(entries, key=lambda e: (e[0], e[1])):
        verdict = _ingest_claim(root, conn, host_uid, orig_name, claimed, entry)
        ingested += verdict == "ingested"
        duplicates += verdict == "duplicates"
        quarantined += verdict == "quarantined"
    remaining = len(list(sd.glob("*.json")))
    return DrainReport(ingested, duplicates, quarantined, remaining)


def drain_pass(root: Path, conn: sqlite3.Connection, host_uid: str, *,
               budget: DrainBudget = DrainBudget(), cursor: RecoveryCursor | None = None) -> DrainReport:
    """Bound admission and yield between complete envelopes.

    Spool order is spooled_at within the admitted batch, not global FIFO.
    Byte/time targets allow one envelope of overrun. Metadata enumeration is
    bounded too; remaining is unknown (None) until this traversal reaches its
    end. Reuse a cursor across passes so live claims do not hide later work.
    The old drain() is the explicit unbounded, single-snapshot operator path.
    """
    sd = spool_dir(root)
    owned_cursor = cursor is None
    cursor = cursor if cursor is not None else RecoveryCursor()
    _, release_errors = cursor.release_claims()
    if release_errors:
        # Do not admit another batch while our earlier live-PID claims remain.
        return DrainReport(remaining=None, more_work=True, release_errors=release_errors)
    started = time.monotonic()
    entries, claims = [], []
    visited = payload_bytes = processed = quarantined = ingested = duplicates = 0
    exhausted = False
    released = retried = False
    try:
        while visited < budget.max_entries and (visited == 0 or time.monotonic() - started < budget.max_elapsed_s):
            path = cursor.next(sd)
            if path is None:
                exhausted = True
                break
            visited += 1
            match = _INFLIGHT_RE.search(path.name)
            if match and ".json.inflight." in path.name:
                if _pid_alive(int(match.group(1))):
                    continue
                original = sd / path.name[:path.name.index(".inflight.")]
                try:
                    if original.exists():
                        path.unlink()
                    else:
                        os.rename(path, original)
                    path = original
                except OSError:
                    continue
            if (not path.name.endswith(".json") or path in cursor.deferred_retries
                    or not path.is_file()):
                continue
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if claims and payload_bytes + size > budget.max_bytes:
                cursor.defer(path)
                break
            claimed = _claim(path)
            if claimed is None:
                continue
            claims.append((path.name, claimed))
            cursor.pending_releases[claimed] = path
            payload_bytes += size
            try:
                data = _read_claimed_json(claimed, size)
                problem = _spool_envelope_problem(data)
                if problem:
                    raise ValueError(problem)
            except (ValueError, OSError, UnicodeError) as exc:
                quarantine_entry(root, claimed, f"malformed spool file: {exc}", as_name=path.name)
                quarantined += 1
                processed += 1
                continue
            entries.append((data.get("spooled_at") or "", path.name, claimed, data))
            if payload_bytes >= budget.max_bytes:
                break
        for _, name, claimed, entry in sorted(entries, key=lambda e: (e[0], e[1])):
            if processed and time.monotonic() - started >= budget.max_elapsed_s:
                break
            verdict = _ingest_claim(root, conn, host_uid, name, claimed, entry)
            processed += 1
            ingested += verdict == "ingested"
            duplicates += verdict == "duplicates"
            quarantined += verdict == "quarantined"
            retried |= verdict == "retry"
            if verdict == "retry":
                cursor.deferred_retries.add(sd / name)
    finally:
        # Best effort per claim: one failed release must not strand its siblings.
        released, release_errors = cursor.release_claims()
        if owned_cursor:
            cursor.close()
        try:
            _fsync_dir(sd)
        finally:
            if release_errors and owned_cursor:
                # Preserve a one-shot caller's cleanup handle even when the
                # original operation also raised (its exception is chained).
                raise SpoolReleaseError(cursor, release_errors)
    more = not exhausted or released or retried or bool(cursor.deferred_retries) or bool(release_errors)
    return DrainReport(ingested, duplicates, quarantined, None if more else 0,
                       processed, payload_bytes,
                       (not exhausted or released) and not release_errors, more, release_errors)
