"""Stdlib-only release identity and sealing; no install, selection or activation.

The directory ID describes assembly inputs, known before creating a venv at its
final path. The seal separately binds exact installed bytes, including absolute
shebangs; hashing those bytes into their own directory name would be circular.
Sealing detects later changes, not hostile writes by the same operating user.
Install without bytecode and run with bytecode writes disabled. Escaping links
are forbidden, so an interpreter must be copied or linked within the release.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile


MANIFEST = "release.json"
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"r-[0-9a-f]{64}")


class ReleaseError(ValueError):
    """Incomplete, changed or incorrectly located release; nothing selected."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()


def _sha(value: str) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ReleaseError("expected a lowercase SHA-256 digest")


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ReleaseError("release paths must be relative strings")
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts
            or str(path) != value or value == "." or "\\" in value):
        raise ReleaseError(f"invalid release-relative path: {value!r}")
    return path


@dataclass(frozen=True)
class InterpreterIdentity:
    implementation: str
    version: str
    platform: str
    sha256: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.implementation, self.version, self.platform)):
            raise ReleaseError("interpreter implementation/version/platform are required")
        _sha(self.sha256)


@dataclass(frozen=True)
class ReleaseInputs:
    source_revision: str
    artifact_id: str
    artifact_sha256: str
    wheel_sha256: str
    dependency_lock_sha256: str
    interpreter: InterpreterIdentity

    def __post_init__(self):
        if not isinstance(self.source_revision, str) or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", self.source_revision
        ):
            raise ReleaseError("a full source revision is required")
        if not isinstance(self.artifact_id, str) or not self.artifact_id.strip():
            raise ReleaseError("artifact identity is required")
        _sha(self.artifact_sha256)
        _sha(self.wheel_sha256)
        _sha(self.dependency_lock_sha256)
        if not isinstance(self.interpreter, InterpreterIdentity):
            raise ReleaseError("interpreter identity is required")

    @classmethod
    def from_lock(cls, *, dependency_lock: bytes, **inputs) -> ReleaseInputs:
        """Hash exact lock bytes, without newline or text normalization."""
        if not isinstance(dependency_lock, bytes) or not dependency_lock:
            raise ReleaseError("nonempty dependency lock bytes are required")
        return cls(dependency_lock_sha256=_digest(dependency_lock), **inputs)

    @property
    def release_id(self) -> str:
        return "r-" + _digest(_json(asdict(self)))


@dataclass(frozen=True)
class SupportedVersions:
    """An exact readable set and the artifact's target write version."""
    read: tuple[int | str, ...]
    write: int | str

    def __post_init__(self):
        kind = type(self.write)
        if (kind not in (int, str) or type(self.read) is not tuple or not self.read
                or any(type(value) is not kind for value in self.read)
                or (kind is int and min(self.read + (self.write,)) < 0)
                or (kind is str and any(not value.strip() for value in self.read + (self.write,)))
                or tuple(sorted(set(self.read))) != self.read or self.write not in self.read):
            raise ReleaseError("compatibility requires an exact sorted read set containing write")

    def supports(self, version: int | str) -> bool:
        return type(version) is type(self.write) and version in self.read


@dataclass(frozen=True)
class Compatibility:
    """Parse declarations as data; never execute a candidate's version owner."""
    schema: SupportedVersions
    envelope: SupportedVersions
    protocol: SupportedVersions
    pending_format: SupportedVersions
    receipt_format: SupportedVersions
    task_model: SupportedVersions
    config_plan: SupportedVersions

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if (not isinstance(value, SupportedVersions)
                    or type(value.write) is not (str if name == "envelope" else int)):
                raise ReleaseError(f"invalid compatibility declaration for {name}")

    @classmethod
    def from_dict(cls, value: dict) -> Compatibility:
        if (not isinstance(value, dict)
                or set(value) != {"declaration_version", *cls.__dataclass_fields__}
                or type(value["declaration_version"]) is not int
                or value["declaration_version"] != 1):
            raise ReleaseError("missing or unsupported runtime compatibility declaration")
        fields = {}
        for name in cls.__dataclass_fields__:
            item = value[name]
            if (not isinstance(item, dict) or set(item) != {"read", "write"}
                    or type(item["read"]) is not list):
                raise ReleaseError(f"invalid runtime compatibility declaration for {name}")
            fields[name] = SupportedVersions(tuple(item["read"]), item["write"])
        return cls(**fields)

    def to_dict(self) -> dict:
        return {"declaration_version": 1, **{
            name: {"read": list(getattr(self, name).read), "write": getattr(self, name).write}
            for name in self.__dataclass_fields__}}

    @property
    def write_versions(self) -> dict[str, int | str]:
        return {name: getattr(self, name).write for name in self.__dataclass_fields__}

    def blockers(self, state: dict[str, int | str]) -> tuple[str, ...]:
        """Compare a complete observed/target descriptor without filling gaps.

        This declares format readability only. It does not prove migrations,
        fleet configuration, consumers or queued-record semantics safe to apply.
        """
        if not isinstance(state, dict):
            raise ReleaseError("runtime state descriptor must be an object")
        issues = [f"unknown runtime format: {name}" for name in sorted(set(state) - self.__dataclass_fields__.keys())]
        for name in self.__dataclass_fields__:
            if name not in state:
                issues.append(f"missing runtime format: {name}")
            elif not getattr(self, name).supports(state[name]):
                issues.append(f"unsupported {name}: {state[name]!r}")
        return tuple(issues)


@dataclass(frozen=True)
class ReleasePaths:
    """Explicit selections relative to the canonical release directory."""
    interpreter: str
    cli: str
    native: str
    artifact: str
    dependency_lock: str
    wheel: str

    def __post_init__(self):
        for value in asdict(self).values():
            _relative(value)


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    kind: str
    mode: int
    sha256: str = ""
    target: str = ""


@dataclass(frozen=True)
class ReleaseManifest:
    directory: Path
    inputs: ReleaseInputs
    paths: ReleasePaths
    compatibility: Compatibility
    inventory: tuple[InventoryEntry, ...]
    runtime_sha256: str

    @property
    def release_id(self) -> str:
        return self.inputs.release_id

    @property
    def cli_path(self) -> Path:
        return self.directory / self.paths.cli

    @property
    def native_path(self) -> Path:
        return self.directory / self.paths.native

    @property
    def seal_sha256(self) -> str:
        """Digest of the canonical sealed payload, excluding the digest itself."""
        return _digest(_json(self._payload()))

    def _payload(self) -> dict:
        return {"schema": 1, "release_id": self.release_id,
                "directory": str(self.directory),
                "inputs": asdict(self.inputs), "paths": asdict(self.paths),
                "compatibility": self.compatibility.to_dict(),
                "inventory": [asdict(entry) for entry in self.inventory],
                "runtime_sha256": self.runtime_sha256}


def release_path(data_root: Path, release_id: str) -> Path:
    """Reserve this final spelling before installation; do not relocate a venv.

    This is a pure path check: it neither creates nor selects the directory.
    """
    if not isinstance(release_id, str) or not _ID.fullmatch(release_id):
        raise ReleaseError("invalid release ID")
    path = Path(data_root).expanduser().resolve() / "state" / "releases" / release_id
    if path.resolve() != path:
        raise ReleaseError("release store or directory is redirected by a symlink")
    return path


def inventory_release(directory: Path) -> tuple[InventoryEntry, ...]:
    """Inventory every entry except the seal; never follow directory symlinks."""
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseError("release directory is missing or redirected")
    root = directory.resolve()
    entries = []

    def scan_error(error):
        raise ReleaseError(f"cannot inventory release: {error}") from error

    for parent, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
        for name in sorted(dirs + files):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST:
                continue
            if Path(parent) == root and name.startswith(".release-"):
                raise ReleaseError("release contains an incomplete seal temporary file")
            if name == "__pycache__" or path.suffix in {".pyc", ".pyo"}:
                raise ReleaseError(f"bytecode is not an immutable install input: {relative}")
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(root):
                    raise ReleaseError(f"release symlink escapes its directory: {relative}")
                entry = InventoryEntry(relative, "symlink", mode, target=target)
            elif stat.S_ISREG(metadata.st_mode):
                entry = InventoryEntry(relative, "file", mode, _digest(path.read_bytes()))
            elif stat.S_ISDIR(metadata.st_mode):
                entry = InventoryEntry(relative, "directory", mode)
            else:
                raise ReleaseError(f"unsupported release entry: {relative}")
            entries.append(entry)
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _inventory_digest(inventory: tuple[InventoryEntry, ...]) -> str:
    return _digest(_json([asdict(entry) for entry in inventory]))


def _verify_inputs(directory: Path, inputs: ReleaseInputs, paths: ReleasePaths) -> Compatibility:
    selected = {key: directory / value for key, value in asdict(paths).items()}
    for key, path in selected.items():
        if not path.resolve(strict=True).is_relative_to(directory):
            raise ReleaseError(f"selected {key} escapes the release")
        if key == "native":
            if not path.is_dir() or not any(path.iterdir()):
                raise ReleaseError("selected native resource directory is empty or missing")
        elif not path.is_file():
            raise ReleaseError(f"selected {key} is not a file")
    for key in ("interpreter", "cli"):
        if not os.access(selected[key], os.X_OK):
            raise ReleaseError(f"selected {key} is not executable")
    lock = selected["dependency_lock"].read_bytes()
    if not lock or _digest(lock) != inputs.dependency_lock_sha256:
        raise ReleaseError("dependency lock digest mismatch")
    if _digest(selected["wheel"].read_bytes()) != inputs.wheel_sha256:
        raise ReleaseError("wheel digest mismatch")
    if _digest(selected["interpreter"].read_bytes()) != inputs.interpreter.sha256:
        raise ReleaseError("interpreter digest mismatch")
    artifact = json.loads(selected["artifact"].read_bytes())
    if (artifact.get("schema") != 1 or artifact.get("artifact_id") != inputs.artifact_id
            or artifact.get("source_revision") != inputs.source_revision
            or artifact.get("content_sha256") != inputs.artifact_sha256):
        raise ReleaseError("package artifact identity mismatch")
    return Compatibility.from_dict(artifact.get("compatibility"))


def seal_release(data_root: Path, inputs: ReleaseInputs, paths: ReleasePaths) -> ReleaseManifest:
    """Verify a final-location install, then publish its seal once, atomically."""
    directory = release_path(data_root, inputs.release_id)
    target = directory / MANIFEST
    if target.exists() or target.is_symlink():
        raise ReleaseError("release already has a seal; it cannot be resealed")
    try:
        compatibility = _verify_inputs(directory, inputs, paths)
        inventory = inventory_release(directory)
    except (OSError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError(f"cannot verify installed release: {exc}") from exc
    manifest = ReleaseManifest(directory, inputs, paths, compatibility, inventory,
                               _inventory_digest(inventory))
    payload = manifest._payload()
    payload["seal_sha256"] = manifest.seal_sha256
    fd, name = tempfile.mkstemp(prefix=".release-", dir=directory)
    published = False
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(_json(payload) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        # Hard-link publication is atomic and refuses a competing existing seal.
        os.link(name, target)
        published = True
        os.unlink(name)
        if inventory_release(directory) != inventory:
            raise ReleaseError("runtime changed while sealing")
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if published:
            target.unlink()
        raise
    finally:
        Path(name).unlink(missing_ok=True)
    return manifest


def read_release(data_root: Path, release_id: str, *, verify_files: bool = True) -> ReleaseManifest:
    """Read a sealed release without importing or executing its code.

    Lifecycle boundaries verify all files. Ordinary command admission may read
    the sealed identity only: hashing an entire venv per call is not a runtime
    integrity policy. Same-user callers are trusted; activation owns validation.
    """
    directory = release_path(data_root, release_id)
    target = directory / MANIFEST
    try:
        if not target.is_file() or target.is_symlink():
            raise ReleaseError("release has no regular sealed manifest")
        raw = json.loads(target.read_bytes())
        if (set(raw) != {"schema", "release_id", "directory", "inputs", "paths",
                        "compatibility", "inventory", "runtime_sha256", "seal_sha256"}
                or type(raw["schema"]) is not int or raw["schema"] != 1):
            raise ReleaseError("incomplete or unsupported release manifest")
        checksum = raw.pop("seal_sha256")
        if checksum != _digest(_json(raw)):
            raise ReleaseError("release manifest digest mismatch")
        input_values = dict(raw["inputs"])
        input_values["interpreter"] = InterpreterIdentity(**input_values["interpreter"])
        inputs = ReleaseInputs(**input_values)
        if (raw["release_id"] != release_id or inputs.release_id != release_id
                or raw["directory"] != str(directory)):
            raise ReleaseError("release directory and identity mismatch")
        paths = ReleasePaths(**raw["paths"])
        compatibility = Compatibility.from_dict(raw["compatibility"])
        expected = tuple(InventoryEntry(**value) for value in raw["inventory"])
        actual = inventory_release(directory) if verify_files else expected
        if expected != actual or raw["runtime_sha256"] != _inventory_digest(actual):
            raise ReleaseError("installed runtime inventory digest mismatch")
        if verify_files and _verify_inputs(directory, inputs, paths) != compatibility:
            raise ReleaseError("artifact and release manifest compatibility mismatch")
        return ReleaseManifest(directory, inputs, paths, compatibility, actual,
                               raw["runtime_sha256"])
    except (OSError, RuntimeError, TypeError, KeyError, AttributeError, ValueError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError(f"cannot verify release {release_id}: {exc}") from exc
