"""Explicit local attestation of the recorder source; reads never bind or repair.

The operator attests unattributed/imported history when binding. This is not a
cryptographic provenance claim against a same-UID host adversary. Each protected
query checks retained host facts and the marker in its own SQLite snapshot.
"""
from __future__ import annotations

import sqlite3
import os
import stat
import time
from pathlib import Path

from .db import connect_ro, db_file
from .ids import read_host_uid
from .ingest import CONSTRUCT_TABLES
from .owner_access import AccessDenied, AccessUnavailable
from .migrations import DowngradeError
from .schema_state import PendingMigrationError, require_current_schema


HOST_TABLES = (*dict.fromkeys(CONSTRUCT_TABLES.values()), "events")
_UNAVAILABLE = (sqlite3.Error, OSError, PendingMigrationError, DowngradeError)


class SourceUnavailable(AccessUnavailable):
    """Recorder source cannot be verified; never an authority/pairing failure."""


class SourceNeedsBinding(SourceUnavailable):
    """Missing operator-owned marker/indexes; local re-attestation may restore them."""


class SourceDenied(AccessDenied):
    """Foreign/mixed source; must never be rebound to the selected installation."""


def source_host_uid(root: Path) -> str:
    try:
        return read_host_uid(Path(root) / "state")
    except (ValueError, OSError) as exc:
        raise SourceUnavailable("owner source unavailable") from exc


def _marker(conn: sqlite3.Connection, host_uid: str, *, required: bool) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("owner_source_binding",)
    ).fetchone()
    if not exists:
        if required:
            raise SourceNeedsBinding("owner source unavailable")
        return False
    rows = conn.execute("SELECT singleton, host_uid FROM owner_source_binding LIMIT 2").fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], str):
        raise SourceUnavailable("owner source unavailable")
    if rows[0][1] != host_uid:
        raise SourceDenied("owner source denied")
    return True


def _indexes(conn: sqlite3.Connection) -> None:
    """Require the operator-created covering indexes without scanning payload rows."""
    for table in HOST_TABLES:
        name = f"owner_source_{table}_host"
        row = conn.execute("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                           (name,)).fetchone()
        if row is None:
            # A dropped table is corruption/schema drift, not a repairable index.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone() is None:
                raise SourceUnavailable("owner source unavailable")
            raise SourceNeedsBinding("owner source unavailable")
        if row[0] != table:
            raise SourceUnavailable("owner source unavailable")
        keys = [row for row in conn.execute(f"PRAGMA index_xinfo({name})") if row[5]]
        listed = [row for row in conn.execute(f"PRAGMA index_list({table})") if row[1] == name]
        if (len(keys) != 1 or keys[0][2] != "host_uid" or keys[0][4] != "BINARY"
                or len(listed) != 1 or listed[0][4]):
            raise SourceUnavailable("owner source unavailable")


def _retained(conn: sqlite3.Connection, host_uid: str) -> None:
    # No active/fleet filter: legacy, null-fleet and tombstone rows count too.
    # Separate extrema use the operator-created covering indexes. Equal
    # BINARY extrema imply every retained host equals the expected identity.
    # Bound pathological databases without fetching whole tables or holding an
    # arbitrarily long read snapshot. This budget applies only to admission.
    deadline = time.monotonic() + 2.0
    remaining = 20_000_000

    def progress():
        nonlocal remaining
        remaining -= 10_000
        return int(remaining <= 0 or time.monotonic() >= deadline)

    conn.set_progress_handler(progress, 10_000)
    try:
        for table in HOST_TABLES:
            for direction in ("ASC", "DESC"):
                row = conn.execute(
                    f"SELECT host_uid FROM {table}"
                    f" ORDER BY host_uid COLLATE BINARY {direction} LIMIT 1"
                ).fetchone()
                if row is not None and row[0] != host_uid:
                    raise SourceDenied("owner source denied")
        if conn.execute(
            "SELECT 1 FROM identity_registry WHERE kind='fleet'"
            " AND parent_uid IS NOT NULL AND parent_uid IS NOT ? LIMIT 1",
            (host_uid,),
        ).fetchone():
            raise SourceDenied("owner source denied")
    finally:
        conn.set_progress_handler(None, 0)


def admit_source(conn: sqlite3.Connection, host_uid: str) -> None:
    """Require binding and retained invariants in the caller's read transaction."""
    if not conn.in_transaction:
        raise SourceUnavailable("owner source unavailable")
    try:
        require_current_schema(conn)
        _marker(conn, host_uid, required=True)
        _indexes(conn)
        _retained(conn, host_uid)
    except _UNAVAILABLE as exc:
        raise SourceUnavailable("owner source unavailable") from exc


def inspect_source(root: Path) -> str:
    """Read-only startup check; query admission must still run per snapshot."""
    host_uid = source_host_uid(root)
    conn = None
    try:
        conn = connect_ro(db_file(root))
        conn.execute("BEGIN")
        admit_source(conn, host_uid)
        return host_uid
    except _UNAVAILABLE as exc:
        raise SourceUnavailable("owner source unavailable") from exc
    finally:
        if conn is not None:
            conn.close()


def bind_source(root: Path, *, expected_host_uid: str | None = None) -> str:
    """Operator-only explicit attestation, existing DB only, never foreign rebind.

    Authority/interactive confirmation belong to the local CLI caller. This
    narrow writer creates metadata and read indexes, with no schema-version migration.
    """
    host_uid = source_host_uid(root)
    if expected_host_uid is not None and host_uid != expected_host_uid:
        raise SourceDenied("owner source denied")
    path = db_file(root)
    conn = None
    try:
        if path.resolve() != path.absolute():
            raise SourceUnavailable("owner source unavailable")
        # Share the reader's regular-file check before SQLite can open a FIFO.
        probe = connect_ro(path)
        probe.close()
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True,
                               isolation_level=None, timeout=5.0)
        conn.execute("BEGIN IMMEDIATE")
        require_current_schema(conn)
        present = _marker(conn, host_uid, required=False)
        # Operator-authorized indexes only; a refusal rolls their DDL back.
        # Readers never create indexes, including on a retained older marker.
        for table in HOST_TABLES:
            conn.execute(f"CREATE INDEX IF NOT EXISTS owner_source_{table}_host"
                         f" ON {table}(host_uid COLLATE BINARY)")
        _indexes(conn)
        _retained(conn, host_uid)
        if not present:
            conn.execute("CREATE TABLE owner_source_binding ("
                         "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                         "host_uid TEXT NOT NULL)")
            conn.execute("INSERT INTO owner_source_binding VALUES (1, ?)", (host_uid,))
        # A concurrent installation identity change must not bind stale state.
        if source_host_uid(root) != host_uid:
            raise SourceUnavailable("owner source unavailable")
        conn.commit()
        return host_uid
    except _UNAVAILABLE as exc:
        raise SourceUnavailable("owner source unavailable") from exc
    finally:
        if conn is not None:
            conn.close()  # rolls back every refused binding


def authority_only_plane(root: Path, *, allow_absent: bool = False) -> None:
    """Validate only existing owner storage, never recorder/queue/socket content.

    Bootstrap may call this after its journal begins. It is not setup admission
    and deliberately does not treat a missing recorder on an old host as fresh.
    """
    from .owner_access import OwnerAccess

    root = Path(root).absolute()
    directory = db_file(root).parent
    try:
        if root.resolve() != root or (root / "state").resolve() != root / "state":
            raise SourceUnavailable("fresh owner storage unavailable")
        if not directory.exists() and not directory.is_symlink():
            if allow_absent:
                return
            raise SourceUnavailable("fresh owner storage unavailable")
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise SourceUnavailable("fresh owner storage unavailable")
        entries = list(directory.iterdir())
        if not entries and allow_absent:
            return
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SourceUnavailable("fresh owner storage unavailable")
        names = {path.name for path in entries}
        if ("owner-access.db" not in names or names - {
                "owner-access.db", "owner-access.db-wal", "owner-access.db-shm", "owner-access.db-journal"}):
            raise SourceUnavailable("fresh owner storage unavailable")
        for path in entries:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise SourceUnavailable("fresh owner storage unavailable")
        OwnerAccess(root).validate_storage()
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SourceUnavailable("fresh owner storage unavailable") from exc


def fresh_owner_setup(root: Path, *, allow_absent: bool = False) -> None:
    """No selection, activation or runtime; owner preparation grants no launch."""
    from ..activation_state import ActivationError, read_selection
    from ..paths import _iter_fleet_dirs

    root = Path(root).absolute()
    try:
        if not root.is_dir() or read_selection(root) is not None:
            raise SourceUnavailable("fresh owner setup unavailable")
        runtimes = [root / "runtime", *(overlay / "runtime" for overlay in _iter_fleet_dirs(root / "local"))]
        for path in (*runtimes, root / "state/activations"):
            if path.resolve() != path or path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
                raise SourceUnavailable("fresh owner setup unavailable")
        authority_only_plane(root, allow_absent=allow_absent)
    except (OSError, ValueError, ActivationError) as exc:
        raise SourceUnavailable("fresh owner setup unavailable") from exc


def inspect_owner_setup(root: Path) -> str:
    """Setup-only admission; private reads/actions always use inspect_source."""
    if os.path.lexists(db_file(root)):
        return inspect_source(root)  # Existing/unbound/foreign recorder never falls back.
    fresh_owner_setup(root)
    return source_host_uid(root)
