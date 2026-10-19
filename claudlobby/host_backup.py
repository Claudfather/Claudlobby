"""Owner-managed recovery bundles, without credentials or restored authority.

The SQLite owner makes the consistent copy; activation owns starting it. A
bundle is a private directory with a last-written manifest, suitable for an
owner's existing off-host copy tool. Nothing is uploaded or scheduled here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile

from .activation_state import ActivationRefusal, locked_activation, read_activation, read_selection
from .config_plan import path_state, read_plan
from .migration_apply import _backup, _file_digest
from .plane.db import connect_ro, db_file
from .plane.ids import read_host_uid
from .releases import read_release


EXCLUSIONS = (
    "owner grants, browser sessions and workspace bindings",
    "credential stores, .env files and provider logins",
    "runtime bot memory, data, working copies and referenced artifacts",
    "Claudron knowledge and other external files",
    "uncommitted ingest queues, delivery receipt files, sockets and locks",
    "installed releases, generated units and activation journals",
)
MARKER = "state/restored-backup.json"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _private_file(path):
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ActivationRefusal("backup requires private regular files owned by the operator")


def _write(path, content, *, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        os.chmod(path, mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(staging, target):
    # Reserve the name exclusively; rename replaces only our empty directory.
    # Interruption leaves an empty reservation, never a partly published root.
    for directory in [*(p for p in staging.rglob("*") if p.is_dir()), staging]:
        os.chmod(directory, 0o700)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    target.mkdir(mode=0o700)
    staging.rename(target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _configuration_path(name):
    """Only authored inputs may be restored; never accept an archive state path."""
    parts = PurePosixPath(name).parts
    if name == "host-system.yaml":
        return True
    if parts and parts[0] == "local":
        # A local fleet is either local/F or local/system/F.
        for depth in (2, 3):
            if (len(parts) > depth and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", p)
                                         for p in parts[1:depth])
                    and _configuration_path("/".join(parts[depth:]))):
                return True
        return False
    return (name in ("fleet.yaml", "projects.yaml") or len(parts) > 1
            and parts[0] in ("library", "voices", "templates")
            and not any(p.startswith(".env") for p in parts))


def _source_files(plan):
    """Frozen manifests, plus unchanged reviewed overlays; never crawl state/HOME."""
    from .config import host_override_path

    files = {}
    sources = plan.effects.get("fleet_sources")
    if not isinstance(sources, dict) or set(sources) != set(plan.fleets):
        raise ActivationRefusal("backup requires a plan with retained fleet configuration")

    def capture(path, destination):
        recorded = plan.inputs.get(str(path))
        if recorded is None or path_state(path, source=True) != recorded["state"]:
            raise ActivationRefusal("backup configuration changed since activation; review and activate it first")
        node = recorded["state"]["node"]
        if node["kind"] == "absent":
            return
        # Do not dereference a link into an unrelated credential or vault tree.
        def walk(source, target):
            info = source.lstat()
            if stat.S_ISDIR(info.st_mode):
                for child in sorted(source.iterdir()):
                    walk(child, target / child.name)
            elif stat.S_ISREG(info.st_mode) and not source.name.startswith(".env"):
                files[str(target)] = (source.read_bytes(), 0o700 if info.st_mode & 0o111 else 0o600)
            else:
                raise ActivationRefusal("backup refuses linked, special or credential files in configuration overlays")
        walk(path, destination)
        if path_state(path, source=True) != recorded["state"]:
            raise ActivationRefusal("backup configuration changed while being copied")

    for name, item in sorted(sources.items()):
        manifest, content = plan.frozen_input(item["fleet"], required=True)
        projects, project_content = plan.frozen_input(item["projects"])
        for path in (manifest, projects):
            recorded = plan.inputs[str(path)]["state"]
            if recorded["node"]["kind"] == "symlink" or recorded["resolved"] != str(path):
                raise ActivationRefusal("backup refuses linked fleet or project configuration")
        try:
            relative = manifest.relative_to(plan.data_root)
        except ValueError as exc:
            raise ActivationRefusal("backup currently requires fleet configuration inside the host data root") from exc
        if projects != manifest.parent / "projects.yaml":
            raise ActivationRefusal("backup fleet source locations disagree")
        files[str(relative)] = (content, 0o600)
        if project_content is not None:
            files[str(relative.parent / "projects.yaml")] = (project_content, 0o600)
        # voices/ is the pre-#2150 voice folder, a plan input only while it is
        # read: drop it here with #2202, or every backup refuses.
        for folder in ("library", "voices", "templates"):
            capture(manifest.parent / folder, relative.parent / folder)
    capture(host_override_path(), Path("host-system.yaml"))
    return files


def create_backup(root: Path, destination: Path) -> dict:
    """Export accepted database state and the selected configuration, without stopping bots."""
    root, destination = Path(root).resolve(), Path(destination).absolute()
    if destination.resolve() != destination or destination.is_relative_to(root):
        raise ActivationRefusal("backup destination must be outside the host root and contain no links")
    # Serialize against activation, not normal SQLite writers. The backup API
    # reads one transaction while the selected plan remains fixed.
    with locked_activation(root):
        selected = read_selection(root)
        if selected is None:
            raise ActivationRefusal("backup requires an active selected host")
        active = read_activation(root, selected["activation_id"])
        if (active.status != "active" or any(active.body["intent"][key] != selected[key]
                for key in ("plan_id", "release_id"))):
            raise ActivationRefusal("backup refuses an incomplete activation")
        for path in (root / "state/activations").glob("*/activation.json"):
            if read_activation(root, path.parent.name).status not in ("active", "rolled_back"):
                raise ActivationRefusal("backup refuses an unfinished activation")
        plan = read_plan(root, selected["plan_id"])
        release = read_release(root, selected["release_id"])
        if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
            raise ActivationRefusal("backup selected configuration and release disagree")
        sources = _source_files(plan)
        host_uid = read_host_uid(root / "state")
        with tempfile.TemporaryDirectory(prefix=".claudlobby-backup-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "bundle"
            staging.mkdir(mode=0o700)
            database = staging / "plane.db"
            conn = connect_ro(db_file(root))
            try:
                conn.execute("BEGIN")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != release.compatibility.schema.write:
                    raise ActivationRefusal("backup database differs from the selected release schema")
                _backup(conn, database)
            finally:
                conn.close()
            for name, (content, mode) in sources.items():
                if not _configuration_path(name):
                    raise ActivationRefusal("backup configuration is outside supported root/local fleet paths")
                _write(staging / "configuration" / name, content, mode=mode)
            paths = [database, *(staging / "configuration" / name for name in sources)]
            manifest = {"schema": 1, "source_root": str(root), "host_uid": host_uid,
                        "release_id": release.release_id, "plan_id": plan.plan_id,
                        "database_schema": version, "exclusions": list(EXCLUSIONS),
                        "files": {str(p.relative_to(staging)): {
                            "sha256": _file_digest(p), "mode": stat.S_IMODE(p.stat().st_mode)} for p in paths}}
            _write(staging / "manifest.json", _json(manifest))
            # Never replace a previous backup. The destination is owner-selected.
            _publish(staging, destination)
            return manifest


def inspect_backup(bundle: Path) -> dict:
    """Verify exactly the manifest's regular files; no archive extraction or SQL writes."""
    bundle = Path(bundle).absolute()
    _private_file(bundle / "manifest.json")
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    if (not isinstance(manifest, dict) or set(manifest) != {
            "schema", "source_root", "host_uid", "release_id", "plan_id", "database_schema", "exclusions", "files"}
            or manifest.get("schema") != 1
            or not isinstance(manifest.get("release_id"), str)
            or not re.fullmatch(r"r-[0-9a-f]{64}", manifest["release_id"])
            or not isinstance(manifest.get("host_uid"), str)
            or not re.fullmatch(r"host_[0-9a-f]{32}", manifest["host_uid"])
            or type(manifest.get("database_schema")) is not int
            or not isinstance(manifest.get("files"), dict) or "plane.db" not in manifest["files"]):
        raise ActivationRefusal("backup manifest is incomplete or unsupported")
    expected = {"manifest.json"}
    for name, recorded in manifest["files"].items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or str(relative) != name
                or name != "plane.db" and (len(relative.parts) < 2 or relative.parts[0] != "configuration"
                    or not _configuration_path(name.removeprefix("configuration/")))):
            raise ActivationRefusal("backup contains an unsupported file path")
        if (not isinstance(recorded, dict) or set(recorded) != {"sha256", "mode"}
                or not isinstance(recorded["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", recorded["sha256"])
                or recorded["mode"] not in (0o600, 0o700)):
            raise ActivationRefusal("backup file metadata is unsupported")
        path = bundle / name
        _private_file(path)
        if _file_digest(path) != recorded["sha256"]:
            raise ActivationRefusal("backup file differs from its recorded digest")
        expected.add(name)
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob("*") if not p.is_dir()}
    if actual != expected:
        raise ActivationRefusal("backup contains unlisted files")
    return manifest


def restore_status(root: Path) -> dict | None:
    """Read the retained recovery provenance without claiming operational readiness."""
    path = root / MARKER
    if not os.path.lexists(path):
        return None
    _private_file(path)
    marker = json.loads(path.read_bytes())
    if (not isinstance(marker, dict) or marker.get("schema") != 1
            or marker.get("replaces_retired_host") is not True
            or marker.get("host_uid") != read_host_uid(root / "state")
            or not isinstance(marker.get("release_id"), str)
            or not re.fullmatch(r"r-[0-9a-f]{64}", marker["release_id"])):
        raise ActivationRefusal("restored backup marker is invalid")
    return {"release_id": marker["release_id"], "host_uid": marker["host_uid"],
            "access_restored": False, "external_work_reconciliation": "operator_required"}


def restore_backup(bundle: Path, root: Path, *, replacing_retired_host: bool) -> dict:
    """Restore to a new, inactive root. The operator must retire the original host."""
    if not replacing_retired_host:
        raise ActivationRefusal("restore requires explicit replacement of a retired host")
    root, bundle = Path(root).absolute(), Path(bundle).absolute()
    if root.resolve() != root or os.path.lexists(root):
        raise ActivationRefusal("restore requires a new absolute data root without links")
    manifest = inspect_backup(bundle)
    with tempfile.TemporaryDirectory(prefix=".claudlobby-restore-", dir=root.parent) as temporary:
        staging = Path(temporary) / "host"
        staging.mkdir(mode=0o700)
        for name, recorded in manifest["files"].items():
            target = db_file(staging) if name == "plane.db" else staging / name.removeprefix("configuration/")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (bundle / name).open("rb") as source, target.open("xb") as output:
                os.chmod(target, recorded["mode"])
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            if _file_digest(target) != recorded["sha256"]:
                raise ActivationRefusal("backup changed during restore")
        _write(staging / "state/host-uid", (manifest["host_uid"] + "\n").encode())
        host_uid = read_host_uid(staging / "state")
        conn = sqlite3.connect(db_file(staging))
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            if (conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or conn.execute("PRAGMA user_version").fetchone()[0] != manifest["database_schema"]):
                raise ActivationRefusal("restored database integrity or version check failed")
            from .plane.owner_source import _retained
            _retained(conn, host_uid)
            # This attestation is local authority, not historical work. Rebind
            # explicitly alongside fresh owner pairing on the replacement.
            conn.execute("DROP TABLE IF EXISTS owner_source_binding")
            conn.commit()
        finally:
            conn.close()
        marker = {"schema": 1, "host_uid": host_uid, "release_id": manifest["release_id"],
                  "database_sha256": _file_digest(db_file(staging)),
                  "backup_sha256": hashlib.sha256(_json(manifest)).hexdigest(),
                  "replaces_retired_host": True, "exclusions": list(EXCLUSIONS)}
        _write(staging / MARKER, _json(marker))
        _publish(staging, root)
        return marker


def restored_bootstrap(root: Path, release) -> bool:
    """Admit only the untouched restored database into ordinary bootstrap/migration."""
    marker_path = root / MARKER
    if not os.path.lexists(marker_path):
        return False
    restore_status(root)
    marker = json.loads(marker_path.read_bytes())
    if (marker.get("schema") != 1 or marker.get("replaces_retired_host") is not True
            or marker.get("host_uid") != read_host_uid(root / "state")
            or marker.get("release_id") != release.release_id):
        raise ActivationRefusal("restore requires its original release and explicit host identity continuity")
    database = db_file(root)
    _private_file(database)
    if _file_digest(database) != marker.get("database_sha256"):
        raise ActivationRefusal("restored database changed before activation")
    for path in database.parent.iterdir():
        if path == database:
            continue
        _private_file(path)
        if path.name not in ("plane.db-wal", "plane.db-shm") or path.name.endswith("-wal") and path.stat().st_size:
            raise ActivationRefusal("restore activation requires no live owner, pending queues or new writes")
    return True
