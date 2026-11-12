"""Historical migration receipts and the shared SQLite backup primitive.

This module cannot apply a sealed-release migration. Current update admission
keeps unsupported schema changes refused; old receipts remain read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .activation_state import ActivationError, _digest, _record_path, _sync


class MigrationApplyError(ActivationError):
    """Keep writers quiesced; the recorded migration cannot safely continue."""


def _regular(path: Path) -> bool:
    if path.resolve() != path:
        raise MigrationApplyError(f"migration path is redirected: {path}")
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise MigrationApplyError(f"migration path is not a regular file: {path}")
    except FileNotFoundError:
        return False
    return True


def _journal_path(root: Path, activation_id: str) -> Path:
    return _record_path(root, activation_id).with_name("migration.json")


def read_migration(data_root: Path, activation_id: str) -> dict | None:
    """Read recorded evidence, not a claim about today's DB or quiescence.

    A result may precede the historical activation completion if the process
    stopped between durable writes. Reading it does not reconcile that state.
    """
    root = Path(data_root).expanduser().resolve()
    path = _journal_path(root, activation_id)
    if not _regular(path):
        return None
    try:
        raw = json.loads(path.read_bytes())
        body = raw["record"]
        if (raw["sha256"] != _digest(body) or body["schema"] != 1
                or body["activation_id"] != activation_id or body["root"] != str(root)
                or body["manifest_id"] != "m-" + _digest(body["manifest"])):
            raise MigrationApplyError("migration journal identity changed")
        return body
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise MigrationApplyError(f"cannot read migration journal: {exc}") from exc


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _logical_digest(conn: sqlite3.Connection) -> str:
    # Backup/checkpoint changes page layout and WAL bytes. Bind schema, every
    # row (including IDs/cursors/sqlite_sequence), and user_version instead.
    digest = hashlib.sha256(f"user_version={_version(conn)}\n".encode())

    def add(row):
        # SQL quote()/iterdump truncates TEXT at embedded NUL. Hash typed
        # values directly so message bodies cannot disappear from the proof.
        encoded = [(type(value).__name__, value.hex() if isinstance(value, (bytes, float)) else value)
                   for value in row]
        digest.update(json.dumps(encoded, separators=(",", ":")).encode() + b"\n")

    schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    for row in schema:
        add(row)
    for kind, name, _, _ in schema:
        if kind != "table":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        columns = conn.execute(f"SELECT * FROM {quoted} LIMIT 0").description
        names = ['"' + column[0].replace('"', '""') + '"' for column in columns]
        order = ",".join(f"typeof({column}),{column}" for column in names)
        add(("table", name))
        for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
            add(row)
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup(source: sqlite3.Connection, path: Path) -> None:
    """Publish only a complete SQLite backup; WAL is read through the API."""
    fd, name = tempfile.mkstemp(prefix=".plane-backup-", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        dest = sqlite3.connect(temporary)
        try:
            source.backup(dest)
            # A standalone backup has no required WAL sidecar.
            dest.execute("PRAGMA journal_mode=DELETE")
            if dest.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise MigrationApplyError("SQLite backup failed its integrity check")
        finally:
            dest.close()
        _sync(temporary)
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
