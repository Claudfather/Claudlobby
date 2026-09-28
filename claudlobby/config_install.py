"""Private configuration writes for the coordinated host activation owner.

Every caller MUST hold the host activation lock and quiesce config producers
for preparation, application, recovery and rollback. This is not an activation
door: it selects no release, calls no supervisor, and never restores a database.
Only ``state/activations/<explicit id>/config`` belongs to this backend.

Before bytes and frozen desired bytes live in a private, fsynced journal store.
Exact restoration uses the plan's bytes, modes and symbolic-link targets.
Replacement workspaces live beside each target (including external fleets), so
no rename crosses a filesystem. A crash before a workspace is journaled may
leave an unreferenced private workspace; it cannot have changed a live target
and is deliberately not guessed at or deleted by recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .config_plan import ConfigPlan, PlanError, path_state, read_plan


class ConfigInstallError(PlanError):
    """Unproven or changed configuration state; stop without overwriting it."""


@dataclass(frozen=True)
class ConfigInstall:
    directory: Path
    plan_id: str
    status: str
    progress: tuple[str, ...]
    retained_directories: tuple[Path, ...]


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _location(root: Path, activation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", activation_id):
        raise ConfigInstallError("supply an explicit simple activation id")
    root = Path(root).absolute().resolve()
    directory = root / "state/activations" / activation_id / "config"
    if directory.resolve() != directory:
        raise ConfigInstallError("configuration activation store is redirected")
    return directory


def _write_new(path: Path, content: bytes, mode: int = 0o600) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _save(directory: Path, journal: dict) -> None:
    data = {"journal": journal, "sha256": _digest(_json(journal))}
    fd, name = tempfile.mkstemp(prefix=".journal-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "journal.json")
        _sync(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _blob(directory: Path, digest: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ConfigInstallError("invalid configuration backup digest")
    path = directory / "blobs" / digest
    if (path.parent.is_symlink() or path.is_symlink() or not path.is_file()
            or path.resolve() != path):
        raise ConfigInstallError(f"missing configuration backup: {digest}")
    content = path.read_bytes()
    if _digest(content) != digest:
        raise ConfigInstallError(f"changed configuration backup: {digest}")
    return content


def _load(directory: Path) -> dict:
    path = directory / "journal.json"
    try:
        if directory.resolve() != directory or path.is_symlink():
            raise ConfigInstallError("configuration journal is redirected")
        envelope = json.loads(path.read_bytes())
        journal = envelope["journal"]
        if (_digest(_json(journal)) != envelope["sha256"]
                or journal["schema"] != 1
                or journal["directory"] != str(directory)
                or "p-" + _digest(_json(journal["plan"])) != journal["plan_id"]):
            raise ConfigInstallError("configuration journal identity changed")
        for digest in journal["blobs"]:
            _blob(directory, digest)
        return journal
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ConfigInstallError):
            raise
        raise ConfigInstallError(f"cannot read configuration journal: {exc}") from exc


def _result(directory: Path, journal: dict) -> ConfigInstall:
    return ConfigInstall(directory, journal["plan_id"], journal["status"],
                         tuple(row["status"] for row in journal["rows"]),
                         tuple(Path(row["target"]) for row in journal["rows"]
                               if row.get("retained")))


def read_config_install(data_root: Path, activation_id: str) -> ConfigInstall:
    """Read recovery metadata without importing the selected application release."""
    directory = _location(data_root, activation_id)
    return _result(directory, _load(directory))


def _desired(after: dict) -> dict:
    if after["kind"] != "tree":
        return {**after, **({"entries": {}} if after["kind"] == "directory" else {})}
    node = {"kind": "directory", "mode": after["mode"], "entries": {}}
    for relative, entry in after["files"].items():
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in (".", "..") for p in parts):
            raise ConfigInstallError("invalid frozen tree path")
        parent = node
        for part in parts[:-1]:
            parent = parent["entries"].setdefault(
                part, {"kind": "directory", "mode": 0o755, "entries": {}})
        parent["entries"][parts[-1]] = {"kind": "file", **entry}
    return node


def _collect(node: dict, read, contents: dict) -> None:
    if node["kind"] == "file":
        data = read(node["sha256"])
        if _digest(data) != node["sha256"]:
            raise ConfigInstallError("configuration changed while backing up")
        contents[node["sha256"]] = data
    elif node["kind"] == "directory":
        for child in node["entries"].values():
            _collect(child, read, contents)


def _backup(path: Path, node: dict, contents: dict) -> None:
    if node["kind"] == "file":
        _collect(node, lambda _: path.read_bytes(), contents)
    elif node["kind"] == "directory":
        for name, child in node["entries"].items():
            _backup(path / name, child, contents)


def prepare_config(plan: ConfigPlan, activation_id: str) -> ConfigInstall:
    """Back up a fresh plan before any target write; caller holds activation lock.

    An existing matching journal is returned for recovery. A different plan or
    incomplete published directory is refused, never overwritten or resealed.
    """
    directory = _location(plan.data_root, activation_id)
    if directory.exists():
        existing = read_config_install(plan.data_root, activation_id)
        if existing.plan_id != plan.plan_id:
            raise ConfigInstallError("activation already belongs to a different plan")
        return existing
    if read_plan(plan.data_root, plan.plan_id) != plan:
        raise ConfigInstallError("configuration plan changed before preparation")
    plan.check_fresh()
    rows, contents = {}, {}
    reserved = [plan.data_root / "state" / name
                for name in ("activations", "config-plans", "releases", "plane")]
    for change in plan.changes:
        target = Path(change.target)
        resolved = target.resolve()
        if any(resolved.is_relative_to(p) or p.is_relative_to(resolved) for p in reserved):
            raise ConfigInstallError(f"configuration output overlaps protected state: {target}")
        # Access.json and other file outputs may need parents not explicitly
        # emitted by the compositor. Record each ensure, including its rollback.
        parent = target.parent
        while not parent.exists():
            rows.setdefault(str(parent), {
                "target": str(parent), "before": path_state(parent),
                "after": {"kind": "directory", "mode": 0o755, "entries": {}},
                "ensure": True, "parent": str(parent.parent.resolve()), "status": "pending"})
            parent = parent.parent
        if not parent.is_dir():
            raise ConfigInstallError(f"configuration parent is not a directory: {parent}")
        rows[str(target)] = {
            "target": str(target), "before": change.before,
            "after": _desired(change.after), "ensure": change.after["kind"] == "directory",
            "parent": str(target.parent.resolve()), "status": "pending"}
        if rows[str(target)]["ensure"] and change.before["node"]["kind"] != "absent":
            raise ConfigInstallError("an ensured directory must have been absent in the plan")
        _backup(target, change.before["node"], contents)
        _collect(rows[str(target)]["after"], plan.blob, contents)
    journal = {"schema": 1, "directory": str(directory), "plan_id": plan.plan_id,
               "plan": plan.payload(), "status": "prepared", "blobs": sorted(contents),
               "rows": [rows[name] for name in sorted(rows)]}
    # No stale input or before snapshot may acquire a prepared journal.
    plan.check_fresh()
    _check_rows(journal)
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync(directory.parent.parent)
    _sync(directory.parent.parent.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".preparing-config-", dir=directory.parent))
    try:
        (temporary / "blobs").mkdir(mode=0o700)
        for digest, content in contents.items():
            _write_new(temporary / "blobs" / digest, content)
        _sync(temporary / "blobs")
        _save(temporary, journal)
        os.rename(temporary, directory)
        _sync(directory.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return read_config_install(plan.data_root, activation_id)


def _identity(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def _matches(row: dict, node: dict) -> bool:
    target = Path(row["target"])
    return str(target.parent.resolve()) == row["parent"] and path_state(target)["node"] == node


def _ensured(row: dict) -> bool:
    target = Path(row["target"])
    return (str(target.parent.resolve()) == row["parent"] and target.is_dir()
            and not target.is_symlink() and _identity(target) == row.get("identity")
            and target.stat().st_mode & 0o777 == row["after"]["mode"])


def _remaining(actual: dict, expected: dict) -> bool:
    """A cleanup may have removed entries, but may never change or add them."""
    if actual.get("kind") == expected.get("kind") == "directory":
        return (actual["mode"] in (expected["mode"], expected["mode"] | 0o700)
                and all(name in expected["entries"] and _remaining(child, expected["entries"][name])
                        for name, child in actual["entries"].items()))
    return actual == expected


def _workspace(row: dict) -> Path | None:
    work = row["work"]
    path = Path(work["path"])
    if (work.get("cleaning") and row["status"] in ("applied", "reverted")
            and path_state(path)["node"]["kind"] == "absent"):
        return None  # deletion completed before its final journal update
    if (path.parent != Path(row["target"]).parent or path.is_symlink()
            or not path.is_dir() or _identity(path) != work["identity"]):
        raise ConfigInstallError(f"replacement workspace changed: {path}")
    allowed = {"next": work["desired"], "previous": work["original"]}
    for child in path.iterdir():
        actual = path_state(child)["node"]
        if (child.name not in allowed
                or not (_remaining(actual, allowed[child.name]) if work.get("cleaning")
                        else actual == allowed[child.name])):
            raise ConfigInstallError(f"replacement workspace content changed: {child}")
    return path


def _check_rows(journal: dict) -> None:
    for row in journal["rows"]:
        status = row["status"]
        if status == "pending":
            valid = path_state(Path(row["target"])) == row["before"]
        elif status == "applied":
            valid = _ensured(row) if row["ensure"] else _matches(row, row["after"])
        elif status == "reverted":
            valid = _ensured(row) if row.get("retained") else _matches(row, row["before"]["node"])
        elif status == "removing_directory":
            valid = _ensured(row) or _matches(row, {"kind": "absent"})
        else:
            workspace = _workspace(row)
            work = row["work"]
            valid = (_matches(row, work["original"]) or _matches(row, work["desired"])
                     or (_matches(row, {"kind": "absent"})
                         and path_state(workspace / "previous")["node"] == work["original"]))
        if not valid:
            raise ConfigInstallError(f"foreign configuration change at {row['target']}")
        if "work" in row:
            _workspace(row)


def _materialize(path: Path, node: dict, directory: Path) -> None:
    kind = node["kind"]
    if kind == "file":
        _write_new(path, _blob(directory, node["sha256"]), node["mode"])
    elif kind == "symlink":
        path.symlink_to(node["target"])
    elif kind == "directory":
        path.mkdir(mode=0o700)
        for name, child in node["entries"].items():
            if Path(name).name != name or name in (".", ".."):
                raise ConfigInstallError("invalid backup child name")
            _materialize(path / name, child, directory)
        path.chmod(node["mode"])
        _sync(path)
    elif kind != "absent":
        raise ConfigInstallError(f"unsupported configuration kind: {kind}")


def _replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    _sync(source.parent)
    if target.parent != source.parent:
        _sync(target.parent)


def _begin(directory: Path, journal: dict, row: dict, *, rollback: bool) -> None:
    target = Path(row["target"])
    original = row["after"] if rollback else row["before"]["node"]
    desired = row["before"]["node"] if rollback else row["after"]
    if not _matches(row, original):
        raise ConfigInstallError(f"foreign configuration change at {target}")
    workspace = Path(tempfile.mkdtemp(prefix=f".claudlobby-config-{directory.parent.name}-",
                                     dir=target.parent))
    _materialize(workspace / "next", desired, directory)
    _sync(workspace)
    _sync(workspace.parent)
    row["work"] = {"path": str(workspace), "identity": _identity(workspace),
                   "original": original, "desired": desired}
    if row["ensure"] and not rollback:
        row["identity"] = _identity(workspace / "next")
    row["status"] = "reverting" if rollback else "installing"
    _save(directory, journal)  # intent durable BEFORE any live rename


def _finish(directory: Path, journal: dict, row: dict) -> None:
    workspace = _workspace(row)
    target = Path(row["target"])
    work = row["work"]
    desired, original = work["desired"], work["original"]
    at_desired = _matches(row, desired)
    if row["ensure"] and row["status"] == "installing":
        at_desired = at_desired and _ensured(row)
    if not at_desired:
        if _matches(row, original):
            if original["kind"] == "directory" or desired["kind"] in ("directory", "absent"):
                if original["kind"] != "absent":
                    _replace(target, workspace / "previous")
            # Files and symlinks replace atomically, keeping the exact before
            # bytes in the durable store. Nonempty trees need two renames.
        elif not (_matches(row, {"kind": "absent"})
                  and path_state(workspace / "previous")["node"] == original):
            raise ConfigInstallError(f"foreign configuration change at {target}")
        if desired["kind"] != "absent":
            if path_state(workspace / "next")["node"] != desired:
                raise ConfigInstallError(f"replacement content is missing: {target}")
            _replace(workspace / "next", target)
    # Recovery may observe a rename that reached the filesystem just before
    # the interrupted process could fsync its directory entries.
    _sync(target.parent)
    _sync(workspace)
    row["status"] = "reverted" if row["status"] == "reverting" else "applied"
    _save(directory, journal)
    _cleanup(directory, journal, row)


def _cleanup(directory: Path, journal: dict, row: dict) -> None:
    if "work" not in row:
        return
    workspace = _workspace(row)
    # Each remaining entry is verified against the recorded before/after, not
    # merely trusted because a directory happens to have our naming prefix.
    row["work"]["cleaning"] = True
    _save(directory, journal)
    if workspace is not None:
        # Read-only authored subdirectories are valid before snapshots. Make
        # only the verified private trash traversable/removable, after recording
        # cleanup intent. The backup still retains its exact original modes.
        for parent, _, _ in os.walk(workspace, followlinks=False):
            path = Path(parent)
            path.chmod(path.stat().st_mode & 0o7777 | 0o700)
        shutil.rmtree(workspace)
    _sync(Path(row["work"]["path"]).parent)
    del row["work"]
    _save(directory, journal)


def apply_config(data_root: Path, activation_id: str) -> ConfigInstall:
    """Apply/resume a prepared config under the caller's host activation lock."""
    directory = _location(data_root, activation_id)
    journal = _load(directory)
    if journal["status"] in ("rolling_back", "rolled_back"):
        raise ConfigInstallError("configuration rollback has begun; cannot resume apply")
    _check_rows(journal)
    journal["status"] = "applying"
    _save(directory, journal)
    for row in journal["rows"]:
        if row["status"] == "pending":
            _begin(directory, journal, row, rollback=False)
        if row["status"] == "installing":
            _finish(directory, journal, row)
        else:
            _cleanup(directory, journal, row)
    _check_rows(journal)
    journal["status"] = "applied"
    _save(directory, journal)
    return _result(directory, journal)


def rollback_config(data_root: Path, activation_id: str) -> ConfigInstall:
    """Restore config in reverse order; preserve children of ensured directories.

    A started per-target swap is completed before undoing it. Pending changes
    are never applied. New mutable children keep their ensured directory alive
    and are reported to the coordinator; they are never recursively removed.
    """
    directory = _location(data_root, activation_id)
    journal = _load(directory)
    _check_rows(journal)
    journal["status"] = "rolling_back"
    _save(directory, journal)
    for row in reversed(journal["rows"]):
        if row["status"] == "installing":
            _finish(directory, journal, row)
        if row["status"] == "pending":
            row["status"] = "reverted"
            _save(directory, journal)
        elif row["status"] == "applied":
            _cleanup(directory, journal, row)
            if row["ensure"]:
                row["status"] = "removing_directory"
                _save(directory, journal)
            else:
                _begin(directory, journal, row, rollback=True)
        if row["status"] == "removing_directory":
            target = Path(row["target"])
            if target.exists():
                if not _ensured(row):
                    raise ConfigInstallError(f"foreign configuration directory at {target}")
                if any(target.iterdir()):
                    row["retained"] = True
                else:
                    target.rmdir()
            _sync(target.parent)
            row["status"] = "reverted"
            _save(directory, journal)
        if row["status"] == "reverting":
            _finish(directory, journal, row)
        elif row["status"] == "reverted":
            _cleanup(directory, journal, row)
    _check_rows(journal)
    journal["status"] = "rolled_back"
    _save(directory, journal)
    return _result(directory, journal)
