"""Explicit SQLite migration backend for a quiesced host activation.

The caller owns ``locked_activation`` and records all quiescence steps before
calling ``apply_migration``. ``queues_classified`` must carry the SHA suffix of
the reviewed migration manifest ID. This backend owns ``backup_saved`` and
``migration_applied``; it does not start writers, select releases, or restore a
database. An interrupted apply is reconciled against its preserved backup and
the exact SQL, never merely against a matching user_version.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .activation_state import (
    ActivationError, ActivationStore, STEPS, _digest, _record_path, _sync,
    _write, read_activation,
)
from .migration_plan import (
    MigrationManifest, build_migration_manifest, verify_pending_queues,
)
from .plane.db import connect, connect_ro, db_file, db_path
from .plane.migrations import SCHEMA_USER_VERSION, _migration_files, migrate
from .releases import read_release


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

    A result may precede the activation's migration_applied completion if the
    process stopped between the two durable writes. Only apply reconciles it.
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


def _save(store: ActivationStore, activation_id: str, body: dict) -> None:
    store.assert_locked()
    path = _journal_path(store.root, activation_id)
    _regular(path)
    _write(path, {"record": body, "sha256": _digest(body)})


def _admit(store: ActivationStore, activation_id: str, manifest: MigrationManifest):
    store.assert_locked()
    record = read_activation(store.root, activation_id)
    body = record.body
    completed = tuple(body["completed"])
    first = STEPS.index("backup_saved")
    last = STEPS.index("migration_applied") + 1
    if (body["status"] != "activating" or not first <= len(completed) <= last
            or completed != STEPS[:len(completed)]
            or body["pending"] not in (None, STEPS[len(completed)] if len(completed) < last else None)):
        raise MigrationApplyError("migration requires recorded quiescence before any writer resumes")
    if (str(store.root) != manifest.data_root
            or body["intent"]["release_id"] != manifest.target["release_id"]
            or body["evidence"].get("queues_classified") != manifest.manifest_id[2:]):
        raise MigrationApplyError("activation does not bind this reviewed migration manifest")
    if body["intent"]["source_release_id"] != manifest.source["release_id"]:
        raise MigrationApplyError("migration source is not the recorded source release")
    if manifest.blockers:
        raise MigrationApplyError("migration remains blocked: " + "; ".join(manifest.blockers))
    recovery = read_release(store.root, body["intent"]["recovery_release_id"])
    if recovery.compatibility.blockers(manifest.rollback["after_sql_versions"]):
        raise MigrationApplyError("recorded recovery release cannot read the post-migration state")
    return record


def _scripts(manifest: MigrationManifest) -> list[tuple[int, str]]:
    """The existing runner must execute precisely the candidate's inventoried SQL."""
    goal = manifest.target["versions"]["schema"]["write"]
    scripts = _migration_files()
    if goal != SCHEMA_USER_VERSION or [n for n, _ in scripts] != list(range(1, goal + 1)):
        raise MigrationApplyError("candidate SQL version does not match this migration runner")
    actual = [(n, hashlib.sha256(sql.encode()).hexdigest(), len(sql.encode())) for n, sql in scripts]
    planned = [(item["version"], item["sha256"], item["bytes"]) for item in manifest.migrations]
    if actual != planned:
        raise MigrationApplyError("candidate SQL hashes do not match this migration runner")
    return scripts


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


def _save_backup(store, activation_id, manifest, body, backup_path):
    # Until a complete backup is journaled, source bytes/version must still
    # match the reviewed preview. No migration can have been admitted yet.
    source_release = read_release(store.root, manifest.source["release_id"])
    target_release = read_release(store.root, manifest.target["release_id"])
    fresh = build_migration_manifest(
        store.root, source_release, target_release,
        initialize_empty=manifest.database["initialize_empty"],
    )
    if fresh.manifest_id != manifest.manifest_id:
        raise MigrationApplyError("source database or migration inventory changed after review")
    _admit(store, activation_id, manifest)
    if body is None:
        body = {"schema": 1, "activation_id": activation_id, "root": str(store.root),
                "manifest_id": manifest.manifest_id, "manifest": json.loads(json.dumps(manifest.payload())),
                "source_version": manifest.database["user_version"],
                "target_version": SCHEMA_USER_VERSION, "backup": None, "result": None}
        if _regular(backup_path):
            raise MigrationApplyError("unjournaled backup already exists")
        store.begin(activation_id, "backup_saved")
        _save(store, activation_id, body)
    source = (sqlite3.connect(":memory:") if manifest.database["initialize_empty"]
              else connect_ro(db_file(store.root)))
    try:
        source.execute("BEGIN")
        version, digest = _version(source), _logical_digest(source)
        if version != (body["source_version"] or 0):
            raise MigrationApplyError("source user_version changed before backup")
        if not _regular(backup_path):
            store.assert_locked()
            _backup(source, backup_path)
        # Adopt an interrupted, already-published backup only when it still
        # contains the exact source snapshot; never overwrite a prior backup.
        saved = connect_ro(backup_path)
        try:
            if _version(saved) != version or _logical_digest(saved) != digest:
                raise MigrationApplyError("saved backup does not match the reviewed source")
        finally:
            saved.close()
    finally:
        source.close()
    body["backup"] = {"path": str(backup_path), "sha256": _file_digest(backup_path),
                      "logical_sha256": digest, "user_version": version}
    _save(store, activation_id, body)
    return body


def _expectations(backup_path, backup, scripts, observed_version):
    # Keep a potentially large Plane off the Python heap. The private scratch
    # copy is disposable; the durable backup is never used as a write target.
    with tempfile.NamedTemporaryFile(prefix=".sql-rehearsal-", dir=backup_path.parent) as scratch:
        saved = connect_ro(backup_path)
        expected = sqlite3.connect(scratch.name, isolation_level=None)
        try:
            saved.backup(expected)
            if (_version(expected) != backup["user_version"]
                    or _logical_digest(expected) != backup["logical_sha256"]):
                raise MigrationApplyError("saved backup logical contents changed")
            if not _version(expected) <= observed_version <= SCHEMA_USER_VERSION:
                raise MigrationApplyError("database version is outside this recorded migration")
            expected.execute("PRAGMA foreign_keys=ON")
            # This prefix runs only on the disposable copy, to recognize a
            # script boundary left by an interrupted forward-only runner.
            for number, sql in scripts:
                if _version(expected) < number <= observed_version:
                    expected.executescript(sql)
                    if _version(expected) != number:
                        raise MigrationApplyError("candidate SQL did not stamp its declared version")
            observed = _logical_digest(expected)
            migrate(expected)
            return observed, _logical_digest(expected)
        finally:
            expected.close()
            saved.close()


def apply_migration(store: ActivationStore, activation_id: str,
                    manifest: MigrationManifest) -> dict:
    """Back up and apply only this reviewed manifest, under recorded quiescence.

    Repeated calls are allowed through migration_applied, before selection or
    writer restart. They verify retained queues and exact data again. A stopped
    call leaves its backup and evidence available; rollback never restores it.
    """
    record = _admit(store, activation_id, manifest)
    scripts = _scripts(manifest)
    verify_pending_queues(store.root, manifest)
    path = db_file(store.root)
    _regular(path)
    _regular(Path(str(path) + "-wal"))
    _regular(Path(str(path) + "-shm"))
    backup_path = _journal_path(store.root, activation_id).with_name("plane-before.sqlite3")
    for suffix in ("-wal", "-journal", "-shm"):
        if _regular(Path(str(backup_path) + suffix)):
            raise MigrationApplyError("standalone migration backup has unexpected sidecars")
    body = read_migration(store.root, activation_id)
    if body is not None and (body["manifest_id"] != manifest.manifest_id
                            or body["source_version"] != manifest.database["user_version"]
                            or body["target_version"] != SCHEMA_USER_VERSION):
        raise MigrationApplyError("activation already owns a different migration manifest")
    if body is None or body["backup"] is None:
        if "backup_saved" in record.body["completed"]:
            raise MigrationApplyError("recorded backup evidence is missing")
        body = _save_backup(store, activation_id, manifest, body, backup_path)
    backup = body["backup"]
    if (backup["path"] != str(backup_path) or not _regular(backup_path)
            or _file_digest(backup_path) != backup["sha256"]):
        raise MigrationApplyError("saved backup bytes changed or are missing")
    exists = _regular(path)
    if not exists and not manifest.database["initialize_empty"]:
        raise MigrationApplyError("the backed-up database disappeared")
    source = connect_ro(path) if exists else sqlite3.connect(":memory:")
    try:
        source.execute("BEGIN")
        version, actual = _version(source), _logical_digest(source)
    finally:
        source.close()
    expected, target_digest = _expectations(backup_path, backup, scripts, version)
    if actual != expected:
        raise MigrationApplyError("database contents differ from the recorded SQL result; keep writers paused")
    result = {"user_version": SCHEMA_USER_VERSION, "logical_sha256": target_digest}
    if body["result"] not in (None, result):
        raise MigrationApplyError("recorded migration result changed")
    if body["result"] is not None and version != SCHEMA_USER_VERSION:
        raise MigrationApplyError("completed migration database was rewound")
    verify_pending_queues(store.root, manifest)
    record = _admit(store, activation_id, manifest)
    if "backup_saved" not in record.body["completed"]:
        record = store.complete(activation_id, "backup_saved", evidence_digest=_digest(backup))
    elif record.body["evidence"]["backup_saved"] != _digest(backup):
        raise MigrationApplyError("activation backup evidence changed")
    if "migration_applied" in record.body["completed"]:
        if body["result"] != result or record.body["evidence"]["migration_applied"] != _digest(result):
            raise MigrationApplyError("activation migration evidence changed")
        return body
    store.begin(activation_id, "migration_applied")
    if version != SCHEMA_USER_VERSION:
        store.assert_locked()
        writer = connect(db_path(store.root), synchronous="FULL")
        try:
            if _version(writer) != version or _logical_digest(writer) != expected:
                raise MigrationApplyError("database changed before SQL apply")
            migrate(writer)
            if _version(writer) != SCHEMA_USER_VERSION or _logical_digest(writer) != target_digest:
                raise MigrationApplyError("migration did not produce the exact expected database")
        finally:
            writer.close()
        _sync(path)
        _sync(path.parent)
    verify_pending_queues(store.root, manifest)
    body["result"] = result
    _save(store, activation_id, body)
    store.complete(activation_id, "migration_applied", evidence_digest=_digest(result))
    return body
