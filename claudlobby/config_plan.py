"""Content-addressed configuration proposals; never an activation writer.

Renderers supply exact destinations and bytes. This store binds them to the
release, source inputs and current generated files, so reviewing a proposal
cannot change a running bot and applying a stale proposal can be refused.
Only the coordinated host activation owner may consume changes as writes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .runtime_versions import CONFIG_PLAN_VERSION


class PlanError(ValueError):
    """Incomplete, stale or changed configuration proposal."""


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _absolute(path: Path) -> Path:
    # Preserve the spelling so a changed parent symlink is visible in state.
    return Path(os.path.abspath(path))


def path_state(path: Path, *, source: bool = False) -> dict:
    """Fingerprint content/links, not volatile timestamps or secret values.

    Sources include linked content; generated destinations record the link
    itself, never traverse it as an output directory. A directory is an owned
    artifact tree only when the caller explicitly records that directory.
    """
    path = _absolute(path)

    def visit(node: Path, ancestors: frozenset[Path]) -> dict:
        try:
            info = node.lstat()
        except FileNotFoundError:
            return {"kind": "absent"}
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            state = {"kind": "symlink", "target": os.readlink(node)}
            if source:
                target = node.resolve()
                if target in ancestors:
                    raise PlanError(f"cyclic source link: {node}")
                state["content"] = visit(target, ancestors | {target})
            return state
        if stat.S_ISREG(info.st_mode):
            return {"kind": "file", "mode": mode,
                    "sha256": _digest(node.read_bytes())}
        if stat.S_ISDIR(info.st_mode):
            return {"kind": "directory", "mode": mode, "entries": {
                child.name: visit(child, ancestors | {node.resolve()})
                for child in sorted(node.iterdir())}}
        raise PlanError(f"unsupported configuration input/output: {node}")

    try:
        return {"resolved": str(path.resolve()), "node": visit(path, frozenset())}
    except (OSError, RuntimeError) as exc:
        raise PlanError(f"cannot inspect configuration path {path}: {exc}") from exc


@dataclass(frozen=True)
class ConfigChange:
    target: str
    before: dict
    after: dict


@dataclass(frozen=True)
class ConfigPlan:
    directory: Path
    data_root: Path
    release_id: str
    release_seal: str
    fleets: tuple[str, ...]
    inputs: dict[str, dict]
    changes: tuple[ConfigChange, ...]
    effects: dict

    def payload(self) -> dict:
        return {"schema": CONFIG_PLAN_VERSION, "data_root": str(self.data_root),
                "release_id": self.release_id, "release_seal": self.release_seal,
                "fleets": list(self.fleets), "inputs": self.inputs,
                "changes": [asdict(change) for change in self.changes],
                "effects": self.effects}

    @property
    def plan_id(self) -> str:
        return "p-" + _digest(_json(self.payload()))

    def content(self, change: ConfigChange) -> bytes:
        if change.after.get("kind") != "file":
            raise PlanError("only a file change has staged bytes")
        return self.blob(change.after["sha256"])

    def blob(self, digest: str) -> bytes:
        """Read one verified staged file, including a file in an owned tree."""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PlanError("invalid staged content digest")
        content_path = self.directory / "files" / digest
        if not content_path.is_file() or content_path.is_symlink():
            raise PlanError(f"missing staged content: {digest}")
        data = content_path.read_bytes()
        if _digest(data) != digest:
            raise PlanError(f"changed staged content: {digest}")
        return data

    def check_fresh(self) -> None:
        """Called under the activation lock, before the first live change."""
        from .releases import read_release

        release = read_release(self.data_root, self.release_id)
        if release.seal_sha256 != self.release_seal:
            raise PlanError("candidate release changed; create a new configuration plan")
        for path, recorded in self.inputs.items():
            if path_state(Path(path), source=recorded["follow_links"]) != recorded["state"]:
                raise PlanError(f"configuration input changed: {path}; create a new plan")
        for change in self.changes:
            if path_state(Path(change.target)) != change.before:
                raise PlanError(f"generated destination changed: {change.target}; create a new plan")


class ConfigPlanBuilder:
    """Accumulate one reviewed rendering; never write its target paths.

    The compositor remains responsible for ownership checks and desired bytes.
    Inputs are captured before rendering and rechecked on sealing. Directories
    may be replaced only when a renderer explicitly owns their entire content.
    """

    def __init__(self, data_root: Path, release_id: str, release_seal: str,
                 fleets: tuple[str, ...], *, effects: dict):
        self.root = _absolute(data_root).resolve()
        self.release_id = release_id
        self.release_seal = release_seal
        self.fleets = tuple(sorted(fleets))
        self.effects = effects
        self.inputs: dict[str, dict] = {}
        self.changes: dict[str, ConfigChange] = {}
        self.contents: dict[str, bytes] = {}

    def input(self, path: Path, *, follow_links: bool = True) -> None:
        target = str(_absolute(path))
        state = {"follow_links": follow_links, "state": path_state(path, source=follow_links)}
        if target in self.inputs and self.inputs[target] != state:
            raise PlanError(f"input changed during rendering: {target}")
        self.inputs[target] = state

    def _add(self, target: Path, after: dict) -> None:
        target = _absolute(target)
        key = str(target)
        previous = self.changes.get(key)
        if previous is not None:
            if previous.after != after:
                raise PlanError(f"two renderers disagree about {target}")
            return
        for existing in self.changes.values():
            other = Path(existing.target)
            if ((target.is_relative_to(other) and existing.after["kind"] != "directory")
                    or (other.is_relative_to(target) and after["kind"] != "directory")):
                raise PlanError(f"overlapping configuration outputs: {target} and {other}")
        self.changes[key] = ConfigChange(key, path_state(target), after)

    def file(self, target: Path, content: bytes, *, mode: int = 0o644) -> None:
        if not isinstance(content, bytes) or type(mode) is not int or mode & ~0o777:
            raise PlanError("file proposal needs bytes and ordinary permission bits")
        digest = _digest(content)
        self.contents[digest] = content
        self._add(target, {"kind": "file", "sha256": digest, "mode": mode})

    def symlink(self, target: Path, source: Path) -> None:
        self._add(target, {"kind": "symlink", "target": str(_absolute(source))})

    def tree(self, target: Path, files: dict[str, tuple[bytes, int]]) -> None:
        """Replace one compositor-owned tree, including removing stale entries.

        Skills are frozen bytes here, not links into mutable authoring overlays.
        Mutable bot data and projects must never be supplied as owned trees.
        """
        entries = {}
        for name, (content, mode) in sorted(files.items()):
            relative = Path(name)
            if (not name or name == "." or relative.is_absolute() or str(relative) != name
                    or ".." in relative.parts or "\\" in name
                    or not isinstance(content, bytes) or type(mode) is not int
                    or mode & ~0o777):
                raise PlanError(f"invalid staged tree file: {name!r}")
            for other in entries:
                if relative.is_relative_to(other) or Path(other).is_relative_to(relative):
                    raise PlanError(f"overlapping staged tree files: {name!r}")
            digest = _digest(content)
            self.contents[digest] = content
            entries[name] = {"sha256": digest, "mode": mode}
        self._add(target, {"kind": "tree", "files": entries, "mode": 0o755})

    def remove(self, target: Path) -> None:
        self._add(target, {"kind": "absent"})

    def directory(self, target: Path) -> None:
        """Ensure a directory without owning/removing its mutable children."""
        target = _absolute(target)
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise PlanError(f"generated directory is redirected or not a directory: {target}")
        if not target.exists():
            self._add(target, {"kind": "directory", "mode": 0o755})

    def seal(self) -> ConfigPlan:
        # No proposal may overlap its own store, including via a parent link.
        store = self.root / "state" / "config-plans"
        if store.resolve() != store:
            raise PlanError("configuration plan store is redirected")
        for change in self.changes.values():
            destination = Path(change.target).resolve()
            if destination.is_relative_to(store) or store.is_relative_to(destination):
                raise PlanError("configuration output overlaps its plan store")
        for path, recorded in self.inputs.items():
            if path_state(Path(path), source=recorded["follow_links"]) != recorded["state"]:
                raise PlanError(f"input changed during rendering: {path}")
        plan = ConfigPlan(store, self.root, self.release_id, self.release_seal,
                          self.fleets, self.inputs,
                          tuple(self.changes[key] for key in sorted(self.changes)),
                          self.effects)
        destination = store / plan.plan_id
        if destination.exists():
            return read_plan(self.root, plan.plan_id)
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".preparing-", dir=store))
        try:
            files = temporary / "files"
            files.mkdir(mode=0o700)
            for digest, content in self.contents.items():
                _write(files / digest, content)
            _write(temporary / "plan.json", _json(plan.payload()))
            for directory in (files, temporary):
                _sync(directory)
            # The plan ID binds all bytes; never update a published proposal.
            os.rename(temporary, destination)
            _sync(store)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return read_plan(self.root, plan.plan_id)


def _write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_plan(data_root: Path, plan_id: str) -> ConfigPlan:
    if not re.fullmatch(r"p-[0-9a-f]{64}", plan_id):
        raise PlanError("invalid configuration plan ID")
    root = _absolute(data_root).resolve()
    directory = root / "state" / "config-plans" / plan_id
    if directory.resolve() != directory:
        raise PlanError("configuration plan is redirected")
    try:
        payload = json.loads((directory / "plan.json").read_bytes())
        if payload["schema"] != CONFIG_PLAN_VERSION or payload["data_root"] != str(root):
            raise PlanError("unsupported or relocated configuration plan")
        plan = ConfigPlan(directory, root, payload["release_id"], payload["release_seal"],
                          tuple(payload["fleets"]), payload["inputs"],
                          tuple(ConfigChange(**change) for change in payload["changes"]),
                          payload["effects"])
        if plan.plan_id != plan_id or payload != plan.payload():
            raise PlanError("configuration plan manifest changed")
        for change in plan.changes:
            if change.after.get("kind") == "file":
                plan.content(change)
            elif change.after.get("kind") == "tree":
                for entry in change.after["files"].values():
                    plan.blob(entry["sha256"])
        return plan
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PlanError(f"cannot read configuration plan {plan_id}: {exc}") from exc
