"""Stdlib current-update record and mechanical Git cutover.

The application owner supplies configuration consent and freshly observed native
quiet receipts. This module never saves/stops/starts bots, migrates SQL, or runs
commands supplied by a record. Its installed status entry survives src/env loss.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import importlib.util
import importlib
import types
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import uuid


PHASES = ("prepare", "quiesce", "git", "environment", "publish", "migrate", "commit", "restore", "done")
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class UpdateError(ValueError):
    """Current operation is unavailable, changed, or not admitted for cutover."""


class UpdateBusyError(UpdateError):
    """Another coordinator owns the process lock; read-only attachment may succeed."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _plain(path: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise UpdateError("update paths must be absolute and unredirected")
    return path


def _git(installation, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null",
                                 "-C", str(installation.source), *args], env=env,
                                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("Git observation or cutover did not complete; inspect actual HEAD") from exc
    if check and result.returncode:
        raise UpdateError("Git observation or cutover refused; local files were not discarded")
    return result


def _head(installation):
    try:
        return installation.preflight()
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc


def _fetch_prepared_target(installation, target):
    """Resolve only the preparation's trusted source, before freezing a new update."""
    before = _head(installation)
    path = _plain(installation.directory / "preparation.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError()
            content = stream.read(65537)
        receipt = json.loads(content) if len(content) <= 65536 else None
        if (not isinstance(receipt, dict) or set(receipt) != {
                "schema", "purpose", "installation", "data_root", "remote", "ref",
                "interpreter", "interpreter_sha256", "revision", "lock_sha256",
                "environment_owned", "complete"}
                or type(receipt["schema"]) is not int or receipt["schema"] != 1
                or receipt["purpose"] != "installation-preparation-only"
                or receipt["installation"] != str(installation.directory)
                or receipt["data_root"] != str(installation.data_root)
                or receipt["complete"] is not True or receipt["environment_owned"] is not True
                or any(not isinstance(receipt[key], str) for key in (
                    "remote", "ref", "interpreter", "interpreter_sha256", "revision", "lock_sha256"))
                or not _COMMIT.fullmatch(receipt["revision"])
                or not all(_SHA.fullmatch(receipt[key]) for key in ("interpreter_sha256", "lock_sha256"))):
            raise ValueError()
        interpreter = _plain(Path(receipt["interpreter"]))
        if interpreter.is_relative_to(installation.directory) or interpreter.is_relative_to(installation.data_root):
            raise ValueError()
        remote, ref = receipt["remote"], receipt["ref"]
        if (not remote or not remote.isprintable() or remote != remote.strip() or remote.startswith("-")
                or not ref or not ref.isprintable() or ref.startswith("-")
                or any(char.isspace() for char in ref) or any(char in ref for char in ":*")):
            raise ValueError()
        if Path(remote).is_absolute():
            _plain(Path(remote))
        elif not (re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", remote)
                  or re.fullmatch(r"(?:[^@/:\s]+@)?[^/:\s]+:[^\s]+", remote)):
            raise UpdateError("prepared remote must be an explicit URL or absolute path, not a remote alias; inspect host setup inputs")
        git_dir = installation.source / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise ValueError()
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("completed preparation receipt is unavailable, redirected or differs from this installation") from exc
    # Bootstrap provenance stays the initial revision after later updates.
    if (_git(installation, "merge-base", "--is-ancestor", receipt["revision"], before, check=False).returncode
            or _digest(_lock_bytes(installation, receipt["revision"], "requirements-runtime.lock")) != receipt["lock_sha256"]):
        raise UpdateError("prepared source history or original dependency lock differs")
    _git(installation, "fetch", "--no-tags", "--no-recurse-submodules", "--", remote, target or ref)
    resolved = _git(installation, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.decode().strip()
    if not _COMMIT.fullmatch(resolved) or (target is not None and resolved != target):
        raise UpdateError("trusted remote did not resolve the exact full update commit")
    if _head(installation) != before:
        raise UpdateError("managed checkout changed while fetching; no update was prepared")
    return resolved


def _lock_bytes(installation, revision, path):
    content = _git(installation, "show", f"{revision}:{path}").stdout
    validate_dependency_lock(content)
    return content


def validate_dependency_lock(content: bytes) -> None:
    """The pinned dependency grammar shared by setup and updates."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError("dependency lock must be UTF-8") from exc
    count = 0
    for raw in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        requirement = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?"
                             r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*(?:\s*;\s*[^\r\n]+)?", requirement)
        if (not match or requirement == line or any(token in requirement for token in ("--", "/", "\\", "@", "\0"))
                or re.sub(r"[-_.]+", "-", match[1]).lower() == "claudlobby"):
            raise UpdateError("dependency lock requires exact names and hashes, without URLs or pip options")
        count += 1
    if not count:
        raise UpdateError("dependency lock is empty")


def run_dependency_command(arguments) -> bytes:
    """Execute the fixed dependency tools without ambient pip/Python selectors."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PIP_", "PYTHON"))}
    environment["PIP_CONFIG_FILE"] = os.devnull
    try:
        result = subprocess.run([str(arg) for arg in arguments], env=environment,
                                capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("dependency preparation or rebuild did not complete") from exc
    if result.returncode:
        raise UpdateError("dependency preparation or rebuild refused; current progress retained")
    return result.stdout


def install_locked_dependencies(python: Path, lock: Path, wheels: Path, *, runner=None) -> None:
    """One offline dependency installation policy; never install Claudlobby itself."""
    run = run_dependency_command if runner is None else runner
    run([python, "-I", "-m", "pip", "--isolated", "install",
         "--disable-pip-version-check", "--no-index", "--find-links", wheels,
         "--only-binary=:all:", "--require-hashes", "--no-deps", "-r", lock])
    run([python, "-I", "-m", "pip", "--isolated", "check"])


def verify_locked_dependencies(python: Path, lock: Path, wheels: Path | None = None, *, runner=None) -> None:
    """Verify the final environment offline without resolving markers ourselves or installing."""
    run = run_dependency_command if runner is None else runner
    command = [python, "-I", "-B", "-m", "pip", "--isolated", "install", "--dry-run",
               "--disable-pip-version-check", "--no-index", "--require-hashes", "--no-deps",
               "--only-binary=:all:", "--no-cache-dir", "--quiet", "--report", "-", "-r", lock]
    if wheels is not None:
        command.extend(["--find-links", wheels])
    try:
        raw = run(command)
    except UpdateError as exc:
        raise UpdateError("offline dependency verification refused; pip dry-run/report support and installed locked dependencies are required") from exc
    try:
        report = json.loads(raw)
        if (not isinstance(report, dict) or report.get("version") != "1"
                or not isinstance(report.get("install"), list)):
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise UpdateError("offline dependency verification requires a supported pip installation report") from exc
    if report["install"]:
        raise UpdateError("installed dependencies differ from the frozen lock; explicit repair is required")
    run([python, "-I", "-B", "-m", "pip", "--isolated", "check"])


def _initialization_scope(value):
    """One explicit first-install scope, readable without the mutable application."""
    if (type(value) is not dict or set(value) != {"directory", "manifests"}
            or not isinstance(value["directory"], str) or type(value["manifests"]) is not list
            or not value["manifests"] or any(not isinstance(path, str) for path in value["manifests"])):
        raise UpdateError("initialization requires one native directory and exact authored manifests")
    paths = [value["directory"], *value["manifests"]]
    if any(not Path(path).is_absolute() or ".." in Path(path).parts or str(Path(path)) != path
           or any(ord(char) < 32 for char in path) for path in paths):
        raise UpdateError("initialization paths must be exact absolute paths")
    if (len(set(value["manifests"])) != len(value["manifests"])
            or any(Path(path).name != "fleet.yaml" for path in value["manifests"])):
        raise UpdateError("initialization manifest paths are duplicated or not fleet.yaml")
    return {"directory": value["directory"], "manifests": sorted(value["manifests"])}


def _check_initialization_intent(record, proposed):
    if proposed is not None and record.get("initialization_scope") != proposed:
        raise UpdateError("initialization differs from the original frozen operation")


def _validate(installation, record):
    try:
        if (type(record) is not dict or type(record["schema"]) is not int or record["schema"] != 1
                or record["installation"] != str(installation.directory)
                or record["data_root"] != str(installation.data_root)
                or record["phase"] not in PHASES or type(record["maintenance"]) is not bool
                or not all(_COMMIT.fullmatch(record[key]) for key in ("before", "target"))
                or (record.get("finalized", True) and not _SHA.fullmatch(record["config_fingerprint"]))
                or (not record.get("finalized", True) and (record["phase"] != "prepare"
                    or record["config_fingerprint"] is not None or record["scope"] != {}))
                or type(record["scope"]) is not dict or type(record["quiet"]) is not dict
                or type(record["git_complete"]) is not bool):
            raise ValueError()
        if ((record["maintenance"] and record["phase"] in {"prepare", "quiesce", "restore", "done"})
                or (not record["maintenance"] and record["phase"] in {"git", "environment", "publish", "migrate"})):
            raise ValueError()
        if type(record.get("finalized", True)) is not bool:
            raise ValueError()
        uuid.UUID(record["request_id"])
        initialization = record.get("initialization_scope")
        if initialization is not None and _initialization_scope(initialization) != initialization:
            raise ValueError()
        if ("initialize" in record and (type(record["initialize"]) is not bool
                or record["initialize"] != (initialization is not None))):
            raise ValueError()
        dispatch = record.get("job_dispatch")
        if dispatch is not None and (type(dispatch) is not dict or set(dispatch) != {"state", "attempt"}
                or dispatch["state"] not in {"pending", "entered", "finished"}
                or type(dispatch["attempt"]) is not int or dispatch["attempt"] < 1):
            raise ValueError()
        for name, identity in record["scope"].items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}", name) or not _SHA.fullmatch(identity):
                raise ValueError()
        dependencies = record["dependencies"]
        if (dependencies["path"] != "requirements-runtime.lock"
                or not all(_SHA.fullmatch(dependencies[key]) for key in ("before", "target"))
                or type(dependencies["prepared"]) is not bool):
            raise ValueError()
        receipt = dependencies.get("receipt_sha256")
        if receipt is not None and not _SHA.fullmatch(receipt):
            raise ValueError()
        if dependencies["prepared"] and dependencies["before"] != dependencies["target"] and receipt is None:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UpdateError("current update record is invalid or unsupported") from exc
    return record


def _require_quiet(record):
    if set(record["quiet"]) != set(record["scope"]):
        raise UpdateError("exact frozen consumer scope lacks quiet receipts")
    for name, identity in record["scope"].items():
        receipt = record["quiet"][name]
        if (type(receipt) is not dict or receipt.get("identity") != identity
                or not isinstance(receipt.get("evidence_sha256"), str)
                or not _SHA.fullmatch(receipt["evidence_sha256"])):
            raise UpdateError("quiet receipt differs from its frozen consumer identity")


def read_update(installation):
    """Source/environment-independent metadata read; creates no files or locks."""
    state = _plain(installation.data_root / "state")
    path = state / "update.json"
    try:
        _plain(path)
        return _validate(installation, json.loads(path.read_bytes()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("current update record cannot be read") from exc


class UpdateStore:
    """One process lock and current operation; native proof belongs to its caller."""
    def __init__(self, installation, *, wait=False):
        self.wait = wait
        self.installation = installation
        self.root = installation.data_root
        self.state = _plain(installation.data_root / "state")
        self.work = _plain(self.state / "update-work")
        self._fd = None

    def __enter__(self):
        self.state.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.state / "update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except OSError as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if isinstance(exc, BlockingIOError):
                raise UpdateBusyError("another update is running and owns the process lock") from exc
            raise UpdateError("update process lock is unavailable") from exc
        return self

    def __exit__(self, *_):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def load(self):
        return read_update(self.installation)

    def _assert_locked(self):
        if self._fd is None:
            raise UpdateError("current update process lock is required")

    def save(self, record):
        """Persist application-owned progress; frozen consent cannot be replaced."""
        self._assert_locked()
        _validate(self.installation, record)
        current = self.load()
        if current is not None and current["phase"] == "done" and record["phase"] == "prepare" and current.get("health_errors"):
            raise UpdateError("prior consumer health/unknown-effect evidence must be reconciled")
        if current is not None and not (current["phase"] == "done" and record["phase"] == "prepare"):
            if (current.get("initialization_scope") != record.get("initialization_scope")
                    or "initialize" in current and current["initialize"] != record.get("initialize")):
                raise UpdateError("frozen initialization intent changed")
            for key in ("request_id", "before", "target"):
                if current[key] != record[key]:
                    raise UpdateError("frozen update intent changed")
            if current.get("finalized", True):
                if (not record.get("finalized", True) or any(current[key] != record[key]
                        for key in ("config_fingerprint", "scope"))):
                    raise UpdateError("frozen update consent changed")
            elif record.get("finalized", True) and (current["phase"] != "prepare" or record["phase"] != "prepare"):
                raise UpdateError("update consent can only finalize during preparation")
            for key in ("path", "before", "target"):
                if current["dependencies"][key] != record["dependencies"][key]:
                    raise UpdateError("frozen dependency intent changed")
        _write(self.state / "update.json", json.dumps(record, sort_keys=True, separators=(",", ":")).encode())

    def prepare(self, target_commit, *, config_fingerprint, scope, request_id,
                lock_path="requirements-runtime.lock"):
        """Freeze target then finalize exact render/scope; compatible existing API."""
        self.prepare_target(target_commit, request_id=request_id, lock_path=lock_path)
        return self.finalize_prepare(config_fingerprint=config_fingerprint, scope=scope)

    def prepare_target(self, target_commit, *, request_id, lock_path="requirements-runtime.lock",
                       initialization_scope=None):
        """Freeze Git/dependency inputs before candidate environment and rendering."""
        self._assert_locked()
        current = self.load()
        initialization_scope = (_initialization_scope(initialization_scope)
                                if initialization_scope is not None else None)
        if current is not None and initialization_scope is not None:
            _check_initialization_intent(current, initialization_scope)
        if current is not None and current["phase"] == "done" and current.get("health_errors"):
            raise UpdateError("previous committed update retains consumer health/unknown-effect evidence; reconcile it before another target")
        if current is not None and current["phase"] != "done":
            if (current["target"], current["request_id"]) != (target_commit, request_id):
                raise UpdateError("current operation differs; continue it before preparing another")
            return current
        if current is not None:
            cleanup_candidate(self)
        if not isinstance(target_commit, str) or not _COMMIT.fullmatch(target_commit):
            raise UpdateError("target must be a frozen full Git commit")
        before = _head(self.installation)
        if _git(self.installation, "rev-parse", "--verify", f"{target_commit}^{{commit}}").stdout.decode().strip() != target_commit:
            raise UpdateError("target Git commit differs")
        if _git(self.installation, "merge-base", "--is-ancestor", before, target_commit, check=False).returncode:
            raise UpdateError("target is not a fast-forward of the managed checkout")
        if lock_path != "requirements-runtime.lock":
            raise UpdateError("use the tracked requirements-runtime.lock")
        old_lock = _lock_bytes(self.installation, before, lock_path)
        new_lock = _lock_bytes(self.installation, target_commit, lock_path)
        record = {"schema": 1, "installation": str(self.installation.directory),
                  "data_root": str(self.installation.data_root), "request_id": request_id,
                  "before": before, "target": target_commit, "config_fingerprint": None,
                  "finalized": False, "scope": {}, "quiet": {}, "phase": "prepare", "maintenance": False,
                  "git_complete": False,
                  "dependencies": {"path": lock_path, "before": _digest(old_lock), "target": _digest(new_lock),
                                   "prepared": old_lock == new_lock, "receipt_sha256": None}}
        if initialization_scope is not None:
            record["initialization_scope"] = initialization_scope
        _validate(self.installation, record)
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(self.work / "dependencies-before.lock", old_lock)
        _write(self.work / "dependencies-target.lock", new_lock)
        self.save(record)
        return record

    def finalize_prepare(self, *, config_fingerprint, scope):
        """Bind reviewed render/scope once; dependencies alone never authorize stop."""
        self._assert_locked()
        record = self.load()
        if record is None:
            raise UpdateError("update target has not been prepared")
        if record.get("finalized", True):
            if (record["config_fingerprint"], record["scope"]) != (config_fingerprint, scope):
                raise UpdateError("current frozen update consent differs")
            return record
        if record["phase"] != "prepare" or not _SHA.fullmatch(config_fingerprint) or not isinstance(scope, dict):
            raise UpdateError("exact render/scope must finalize during preparation")
        record.update(config_fingerprint=config_fingerprint, scope=deepcopy(scope), finalized=True)
        self.save(record)
        return record

    @classmethod
    def from_parent(cls, installation, *, fd, parent_pid, request_id):
        """Use only the calling parent's inherited locked open description."""
        if os.getppid() != parent_pid or type(fd) is not int or fd < 3:
            raise UpdateError("application stage does not belong to the updater parent")
        store = cls(installation)
        _verify_inherited_lock(store.state / "update.lock", fd)
        store._fd = fd
        record = store.load()
        if record is None or record["request_id"] != request_id:
            raise UpdateError("application stage request differs from its parent operation")
        return store

    def _dependency_inputs(self, record):
        for name in ("before", "target"):
            path = _plain(self.work / f"dependencies-{name}.lock")
            if _digest(path.read_bytes()) != record["dependencies"][name]:
                raise UpdateError("frozen dependency input changed")

    def _dependency_run(self, arguments):
        return run_dependency_command(arguments)

    def _dependency_inventory(self, directory):
        directory = _plain(directory)
        inventory = {}
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
                raise UpdateError("dependency cache must contain only plain binary wheels")
            inventory[path.name] = _digest(path.read_bytes())
        if not inventory:
            raise UpdateError("dependency wheel cache is empty")
        return inventory

    def _dependency_binding(self, record, interpreter):
        interpreter = _plain(Path(interpreter))
        if (not interpreter.is_file() or not os.access(interpreter, os.X_OK)
                or interpreter.is_relative_to(self.installation.source)
                or interpreter.is_relative_to(self.installation.environment)):
            raise UpdateError("dependency rebuild requires an independent host Python")
        runtime = self._dependency_run([interpreter, "-I", "-c",
            "import json,sys,sysconfig; print(json.dumps([sys.version,sysconfig.get_platform(),sys.implementation.cache_tag]))"])
        return {"request_id": record["request_id"], "installation": record["installation"],
                "data_root": record["data_root"], "target": record["target"],
                "interpreter": str(interpreter), "python_sha256": _digest(interpreter.read_bytes()),
                "runtime_sha256": _digest(runtime)}

    def verify_dependencies(self):
        """Recheck request/runtime-bound immutable inputs before quieting consumers."""
        self._assert_locked()
        record = self.load()
        if record is None:
            raise UpdateError("current dependency operation is unavailable")
        self._dependency_inputs(record)
        dependencies = record["dependencies"]
        if dependencies["before"] == dependencies["target"]:
            return record
        proof = dependencies.get("proof")
        if not dependencies["prepared"] or type(proof) is not dict:
            raise UpdateError("changed dependencies lack installability proof")
        try:
            binding = self._dependency_binding(record, proof["binding"]["interpreter"])
            inventories = {name: self._dependency_inventory(self.work / f"wheels-{name}")
                           for name in ("before", "target")}
            expected = {"binding": binding, "wheels": inventories}
            if (proof != expected or dependencies["receipt_sha256"] !=
                    _digest(json.dumps(expected, sort_keys=True).encode())):
                raise UpdateError("dependency proof or cached immutable inputs changed")
        except (KeyError, TypeError, OSError) as exc:
            raise UpdateError("dependency proof or cache is unavailable") from exc
        return record

    def prepare_dependencies(self, *, interpreter):
        """Download hashed wheels and prove dependency installability before downtime.

        This proves dependencies only, never candidate application validation.
        Both frozen sets remain available for explicit compatible recovery.
        """
        self._assert_locked()
        record = self.load()
        if record is None or record["phase"] != "prepare" or record["maintenance"]:
            raise UpdateError("dependencies must be prepared before quiescence")
        self._dependency_inputs(record)
        dependencies = record["dependencies"]
        if dependencies["before"] == dependencies["target"]:
            return record  # No interpreter observation, download, or installation.
        if dependencies["prepared"]:
            return self.verify_dependencies()
        binding = self._dependency_binding(record, interpreter)
        python = Path(binding["interpreter"])
        inventories = {}
        for name in ("before", "target"):
            check = _plain(self.work / "dependency-check")
            self._dependency_run([python, "-I", "-m", "venv", "--clear", check])
            cache = _plain(self.work / f"wheels-{name}")
            cache.mkdir(mode=0o700, exist_ok=True)
            self._dependency_run([check / "bin/python", "-I", "-m", "pip", "--isolated", "download",
                "--disable-pip-version-check", "--only-binary=:all:", "--require-hashes",
                "--no-deps", "--dest", cache, "-r", self.work / f"dependencies-{name}.lock"])
            inventories[name] = self._dependency_inventory(cache)
            self._install_dependencies(check / "bin/python", name)
        dependencies["proof"] = {"binding": binding, "wheels": inventories}
        dependencies["receipt_sha256"] = _digest(json.dumps(dependencies["proof"], sort_keys=True).encode())
        dependencies["prepared"] = True
        self.save(record)
        return self.verify_dependencies()

    def _install_dependencies(self, python, name):
        install_locked_dependencies(python, self.work / f"dependencies-{name}.lock",
                                    self.work / f"wheels-{name}", runner=self._dependency_run)

    def cutover_environment(self):
        """Offline, repeatable rebuild at the final path; imports no application code."""
        self._assert_locked()
        record = self.load()
        if (record is None or record["phase"] != "environment" or not record["maintenance"]
                or not record["git_complete"]):
            raise UpdateError("environment rebuild requires completed Git cutover under maintenance")
        _require_quiet(record)
        record = self.verify_dependencies()
        if _head(self.installation) != record["target"]:
            raise UpdateError("environment rebuild requires the frozen target checkout")
        dependencies = record["dependencies"]
        if dependencies["before"] != dependencies["target"]:
            environment = _plain(self.installation.environment)
            python = dependencies["proof"]["binding"]["interpreter"]
            self._dependency_run([python, "-I", "-m", "venv", "--clear", environment])
            self._install_dependencies(environment / "bin/python", "target")
        dependencies["environment_complete"] = True
        self.save(record)

    def begin_cutover(self):
        """Called immediately after the native owner re-observes every frozen consumer quiet."""
        self._assert_locked()
        record = self.load()
        if record is None or record["phase"] not in {"quiesce", "git"}:
            raise UpdateError("cutover requires the current quiesce phase")
        if not record.get("finalized", True):
            raise UpdateError("update consent is not finalized")
        _require_quiet(record)
        if not record["dependencies"]["prepared"]:
            raise UpdateError("changed dependencies have not been prepared and validated")
        self.verify_dependencies()
        record.update(phase="git", maintenance=True)
        self.save(record)

    def cutover_git(self):
        """Resume by actual HEAD; never reset, stash, clean, or replay an unknown native request."""
        self._assert_locked()
        record = self.load()
        if record is None or record["phase"] != "git" or not record["maintenance"]:
            raise UpdateError("Git cutover lacks the current maintenance phase")
        _require_quiet(record)
        if not record["dependencies"]["prepared"]:
            raise UpdateError("changed dependencies have not been prepared and validated")
        self.verify_dependencies()
        actual = _head(self.installation)
        if actual == record["before"] and actual != record["target"]:
            _git(self.installation, "-c", "merge.autoStash=false", "-c", "submodule.recurse=false",
                 "merge", "--ff-only", "--no-edit", "--no-stat", record["target"])
            actual = _head(self.installation)
        if actual != record["target"]:
            raise UpdateError("actual Git HEAD differs from both frozen before and target; inspect without resetting")
        record["git_complete"] = True
        self.save(record)


def _verify_inherited_lock(path, fd):
    path = _plain(path)
    linked, opened = path.lstat(), os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
            or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)):
        raise UpdateError("inherited update/runtime lock differs from its recorded file")
    # A separate open description must not be able to take EX. Closing this
    # probe never unlocks the inherited parent's open description.
    probe = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise UpdateError("inherited lock is not held by its coordinator")
    finally:
        os.close(probe)
    os.set_inheritable(fd, False)


_STAGE_IMPORT = """import pathlib,sys
source=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(source))
import claudlobby
if pathlib.Path(claudlobby.__file__).resolve()!=source/'claudlobby/__init__.py':
    raise SystemExit('application stage package differs from explicit source')
from claudlobby import update_entry
if pathlib.Path(update_entry.__file__).resolve()!=source/'claudlobby/update_entry.py':
    raise SystemExit('application stage entry differs from explicit source')
raise SystemExit(update_entry.main(sys.argv[2:]))
"""


def _application_stage(store, stage, *, source=None, python=None, runtime_fd=None, initialization=None):
    """One fixed private stage in a fresh verified application process."""
    store._assert_locked()
    if stage not in {"authorize", "retire-render", "render", "prepare", "quiesce", "quiet", "publish", "restore"}:
        raise UpdateError("unknown application update stage")
    record = store.load()
    source = _plain(source if source is not None else store.installation.source)
    python = python if python is not None else store.installation.environment / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise UpdateError("application stage interpreter is unavailable")
    inherited = [store._fd]
    args = [str(store.installation.directory), stage, str(store._fd), str(os.getpid()), record["request_id"] if record is not None else "-"]
    if runtime_fd is not None:
        _verify_inherited_lock(store.state / "runtime-use.lock", runtime_fd)
        inherited.append(runtime_fd)
        args.append(str(runtime_fd))
    if initialization is not None:
        if stage != "authorize":
            raise UpdateError("initialization consent is accepted only at operator authorization")
        args.extend(["--initialization-scope", json.dumps(_initialization_scope(initialization), sort_keys=True)])
    clean = {key: value for key, value in os.environ.items()
             if not key.startswith(("PYTHON", "GIT_", "BASH_FUNC_")) and key not in {"BASH_ENV", "ENV"}}
    clean.update(CLAUDLOBBY_INSTALLATION=str(store.installation.directory),
                 CLAUDLOBBY_ROOT=str(store.root), PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run([str(python), "-I", "-B", "-c", _STAGE_IMPORT, str(source), *args],
                                cwd=store.root, env=clean, pass_fds=tuple(inherited),
                                capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"application stage {stage} interrupted; continue the same operation") from exc
    if result.returncode:
        record = store.load()
        if record is not None:
            record["stage_error"] = {"stage": stage, "detail": result.stderr[-2000:]}
            store.save(record)
        raise UpdateError(f"application stage {stage} refused; continue the retained operation")
    if stage == "authorize":
        try:
            authorized = json.loads(result.stdout)
            authorized["interpreter"] = str(Path(sys.executable).resolve())
            return authorized
        except (ValueError, TypeError) as exc:
            raise UpdateError("operator authorization returned no bound installation context") from exc
    return store.load()


def _candidate_revision(store, source):
    result = _git(store.installation, "-C", source, "rev-parse", "--verify", "HEAD^{commit}")
    if result.stdout.decode().strip() != store.load()["target"]:
        raise UpdateError("candidate checkout differs from frozen target")
    dirty = _git(store.installation, "-C", source, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty.stdout:
        raise UpdateError("candidate checkout has changed; leave its files intact")
    top = _git(store.installation, "-C", source, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if top != str(source):
        raise UpdateError("candidate source is not the exact owned checkout")


def prepare_candidate_render(store, *, interpreter):
    """Prepare a detached target and dependency environment before rendering."""
    store._assert_locked()
    record = store.load()
    if record is None or record["phase"] != "prepare" or record.get("finalized", True):
        raise UpdateError("candidate render requires the unfinalized frozen target")
    if _head(store.installation) != record["before"]:
        raise UpdateError("candidate preparation requires the unchanged current checkout")
    store.prepare_dependencies(interpreter=interpreter)
    record = store.load()
    source = _plain(store.work / ("candidate-" + record["request_id"]))
    if record.get("candidate_source") is None:
        if source.exists():
            raise UpdateError("candidate source path already exists without ownership")
        record["candidate_source"] = str(source)
        store.save(record)
    elif record["candidate_source"] != str(source):
        raise UpdateError("candidate source ownership changed")
    if not source.exists():
        _git(store.installation, "worktree", "add", "--detach", str(source), record["target"])
    _candidate_revision(store, source)
    python = store.installation.environment / "bin/python"
    if record["dependencies"]["before"] != record["dependencies"]["target"]:
        python = store.work / "dependency-check/bin/python"
    record = store.load()
    record["candidate_python"] = str(python)
    store.save(record)
    return _application_stage(store, "render", source=source, python=python)


def prepare_application_operation(store, *, install_directory, interpreter):
    """Finalize the reviewed candidate render through the current application."""
    record = store.load()
    if record is None or record["phase"] != "prepare" or not record.get("candidate_render"):
        raise UpdateError("candidate render is not prepared")
    if _head(store.installation) != record["before"]:
        raise UpdateError("application preparation requires the unchanged current checkout")
    if (record.get("initialization_scope") is not None
            and record["initialization_scope"]["directory"] != str(install_directory)):
        raise UpdateError("native directory differs from original initialization scope")
    record["native_install_directory"] = str(_plain(Path(install_directory)))
    record["host_interpreter"] = str(_plain(Path(interpreter)))
    store.save(record)
    return _application_stage(store, "prepare")


def _prepare_quiet_support(store):
    record = store.load()
    if record.get("quiet_support") is not None:
        return
    if record["phase"] != "quiesce" or _head(store.installation) != record["before"]:
        raise UpdateError("resume lacks retained native quiet support; no cutover attempted")
    files = {"supervisor.sh": store.installation.source / "claudlobby/_runtime_scripts/supervisor.sh",
             "supervision_runtime.py": store.installation.source / "claudlobby/supervision_runtime.py"}
    directory = _plain(store.work / ("quiet-" + record["request_id"]))
    directory.mkdir(mode=0o700, exist_ok=True)
    hashes = {}
    for name, source in files.items():
        if source.is_symlink() or not source.is_file():
            raise UpdateError("current native quiet implementation is unavailable")
        content = source.read_bytes()
        destination = _plain(directory / name)
        if destination.exists() and destination.read_bytes() != content:
            raise UpdateError("retained native quiet support changed")
        _write(destination, content)
        hashes[name] = _digest(content)
    if _head(store.installation) != record["before"]:
        raise UpdateError("source changed while retaining native quiet support")
    record["quiet_support"] = hashes
    store.save(record)


def _independent_quiet(store):
    """Reobserve quiet even with no usable mutable source/environment."""
    record = store.load()
    consumers = record.get("consumers")
    if not isinstance(consumers, dict) or set(consumers) != set(record["scope"]):
        raise UpdateError("retained consumer set differs from original consent")
    for key, progress in consumers.items():
        row = progress.get("intent") if isinstance(progress, dict) else None
        if (not isinstance(row, dict) or row.get("target") != key
                or _digest(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()) != record["scope"][key]):
            raise UpdateError("retained consumer intent differs from original consent")
    directory = _plain(store.work / ("quiet-" + record["request_id"]))
    support = record.get("quiet_support", {})
    if set(support) != {"supervisor.sh", "supervision_runtime.py"}:
        raise UpdateError("retained native quiet implementation is unavailable")
    for name, digest in support.items():
        path = _plain(directory / name)
        if _digest(path.read_bytes()) != digest:
            raise UpdateError("retained native quiet implementation changed")
    specification = importlib.util.spec_from_file_location("_update_quiet_runtime", directory / "supervision_runtime.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    class QuietAdapter:
        def call(self, function, *args, timeout=30):
            if function not in {"svc_activation_quiet", "svc_inventory_catalog", "svc_inventory_state"}:
                raise UpdateError("retained native adapter only observes quiet or absence")
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"BASH_ENV", "ENV"} and not key.startswith("BASH_FUNC_")}
            environment["LC_ALL"] = "C"
            return subprocess.run(["/bin/bash", "-c",
                '_SUPERVISOR_LIB_DIR="$1"; shift; _OS=$(uname -s) || exit 3; . "$_SUPERVISOR_LIB_DIR/supervisor.sh" || exit 3; "$@"',
                "update-quiet", str(directory), function, *(str(arg) for arg in args)],
                env=environment, capture_output=True, text=True, timeout=timeout)
    for key, progress in record["consumers"].items():
        row = progress["intent"]
        binding = row["bot_binding"]
        socket = Path(binding[2]) / f"tmux-{os.getuid()}" / binding[1] if binding else None
        if row.get("kind") == "added":
            evidence = module.assert_native_absent(QuietAdapter(), manager=row["manager"],
                domain=row["domain"], target=key, installed_file=Path(row["destination"]),
                search_directories=row["search_directories"], socket_path=socket).details
        elif not row["installed"] or "@." in key:
            evidence = {"uninstalled_or_template": True}
        else:
            evidence = module.assert_quiescent(QuietAdapter(), installed_file=Path(row["installed"][0]["path"]),
                                               target=key, socket_path=socket).details
        record["quiet"][key] = {"identity": record["scope"][key],
                                "evidence_sha256": _digest(json.dumps(evidence, sort_keys=True).encode())}
    store.save(record)


def run_prepared_update(store):
    """Run one finalized operation; no mutable application survives cutover."""
    store._assert_locked()
    record = store.load()
    if record is None or not record.get("finalized", True) or record["phase"] == "prepare":
        raise UpdateError("finalized application preparation is required")
    if record["phase"] == "done":
        if not record.get("health_errors"):
            return cleanup_candidate(store)
        if _head(store.installation) != record["target"]:
            raise UpdateError("health reconciliation requires exact target checkout")
        _application_stage(store, "restore")
        return cleanup_candidate(store)
    actual = _head(store.installation)
    if record["phase"] == "quiesce":
        if actual != record["before"]:
            raise UpdateError("quiescence requires the exact previous checkout")
        # Phase=quiesce fences new native starts. Drain those admitted before
        # that transition before trusting the frozen bot disposition. Release
        # the lock again so existing bots can record their handoff via the CLI.
        with _exclusive_runtime(store):
            pass
        _prepare_quiet_support(store)
        _application_stage(store, "quiesce")
    record = store.load()
    if record["phase"] in {"quiesce", "git", "environment", "publish", "commit"}:
        with _exclusive_runtime(store) as fd:
            record = store.load()
            actual = _head(store.installation)
            if record["phase"] == "quiesce":
                if actual != record["before"]:
                    raise UpdateError("final quiet requires the exact previous checkout")
                _application_stage(store, "quiet", runtime_fd=fd)
                store.begin_cutover()
            # Every continuation re-observes quiet under this EX, including an
            # interrupted target checkout or partially rebuilt environment.
            _independent_quiet(store)
            if store.load()["phase"] == "git":
                if actual not in {record["before"], record["target"]}:
                    raise UpdateError("Git cutover differs from frozen before/target")
                store.cutover_git()
                record = store.load()
                record["phase"] = "environment"
                store.save(record)
            if store.load()["phase"] == "environment":
                if _head(store.installation) != store.load()["target"]:
                    raise UpdateError("environment cutover requires exact target checkout")
                store.cutover_environment()
            if _head(store.installation) != store.load()["target"]:
                raise UpdateError("publication requires exact target checkout")
            _application_stage(store, "publish", runtime_fd=fd)
    record = store.load()
    if record["phase"] == "restore":
        if _head(store.installation) != record["target"]:
            raise UpdateError("restoration requires exact target checkout")
        _application_stage(store, "restore")
    return cleanup_candidate(store)


@contextmanager
def _exclusive_runtime(store):
    lock = _plain(store.state / "runtime-use.lock")
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        linked, opened = lock.lstat(), os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)):
            raise UpdateError("runtime exclusion file changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("runtime is still in use; continue after admitted readers finish") from exc
        yield fd
    finally:
        os.close(fd)


def cleanup_candidate(store):
    """Remove only this completed operation's clean registered Git checkout."""
    store._assert_locked()
    record = store.load()
    if record is None or record["phase"] != "done" or record.get("health_errors"):
        return record
    candidate = record.get("candidate_source")
    if candidate is None or record.get("candidate_cleaned"):
        return record
    source = _plain(store.work / ("candidate-" + record["request_id"]))
    if candidate != str(source):
        raise UpdateError("candidate cleanup ownership differs; files retained")
    if source.exists():
        _candidate_revision(store, source)
        _git(store.installation, "worktree", "remove", str(source))
    # Absence covers an interrupted successful removal; never prune unrelated
    # worktrees or force removal of a dirty/unrecognized checkout.
    record["candidate_cleaned"] = True
    store.save(record)
    return record



_JOB_SUPPORT = ("update_job.py", "supervision_inventory.py", "resources.py", "installation.py",
                "supervisor-caller.py")


def _prepare_job_support(store):
    """Retain canonical stdlib observation/membership owners before native effects."""
    _prepare_quiet_support(store)
    record = store.load()
    if record.get("job_support") is not None:
        return _retained_job(store)
    if _head(store.installation) != record["before"]:
        raise UpdateError("independent job support requires the unchanged previous checkout")
    directory = _plain(store.work / ("quiet-" + record["request_id"]))
    hashes = {}
    for name in _JOB_SUPPORT:
        source = store.installation.source / "claudlobby" / name
        if name == "supervisor-caller.py":
            source = store.installation.source / "claudlobby/_runtime_scripts" / name
        if source.is_symlink() or not source.is_file():
            raise UpdateError("canonical independent updater support is unavailable")
        content = source.read_bytes()
        destination = _plain(directory / name)
        if destination.exists() and destination.read_bytes() != content:
            raise UpdateError("retained updater support changed")
        _write(destination, content)
        hashes[name] = _digest(content)
    if _head(store.installation) != record["before"]:
        raise UpdateError("source changed while retaining updater support")
    record["job_support"] = hashes
    store.save(record)
    return _retained_job(store)


def _retained_job(store):
    """Load a fixed verified stdlib support package, never the mutable application."""
    record = store.load()
    directory = _plain(store.work / ("quiet-" + record["request_id"]))
    hashes = record.get("job_support", {})
    quiet = record.get("quiet_support", {})
    if set(hashes) != set(_JOB_SUPPORT) or set(quiet) != {"supervisor.sh", "supervision_runtime.py"}:
        raise UpdateError("independent updater support is incomplete")
    for name, digest in {**hashes, **quiet}.items():
        path = _plain(directory / name)
        if not path.is_file() or _digest(path.read_bytes()) != digest:
            raise UpdateError("retained independent updater support changed")
    # Import the unchanged canonical modules under a private package so their
    # relative imports remain intact. No activation or application policy copy.
    name = "_claudlobby_update_" + record["request_id"].replace("-", "")
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
    sys.modules[name + ".git_update"] = sys.modules[__name__]
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        jobs = importlib.import_module(name + ".update_job")
        inventory = importlib.import_module(name + ".supervision_inventory")
        locations = importlib.import_module(name + ".installation")
    finally:
        sys.dont_write_bytecode = previous
    installation = locations.read_installation(store.installation.directory)
    adapter = inventory.Adapter(types.SimpleNamespace(native=directory))
    return jobs, installation, adapter



def _authorize_retained_operator(store, adapter):
    """Continue from retained native bindings when src/env are unavailable."""
    if any(key in os.environ for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE")):
        raise UpdateError("host update requires an operator outside generated bot context")
    record = store.load()
    checked = 0
    for key, progress in record["consumers"].items():
        row = progress["intent"]
        if not row["installed"] or "@." in key:
            continue
        checked += 1
        verdict = adapter.call("svc_activation_assert_external", row["installed"][0]["path"],
                               key, str(os.getpid()), timeout=30)
        if verdict.returncode != 0:
            raise UpdateError("operator ancestry is managed or cannot be verified")
    binding = record.get("updater_job")
    if binding is not None:
        checked += 1
        verdict = adapter.call("svc_activation_assert_external", binding["installed"], binding["target"],
                               str(os.getpid()), timeout=30)
        if verdict.returncode != 0:
            raise UpdateError("operator ancestry belongs to the updater or cannot be verified")
    if not checked:
        raise UpdateError("current host has no retained native ancestry evidence")


def _check_bound_job(store, jobs, installation, adapter):
    """Keep the original native target/domain; a caller context never relocates it."""
    record = store.load()
    binding = record.get("updater_job")
    if binding is None:
        raise UpdateError("independent native job has no retained binding")
    actual = jobs._definition(installation, adapter, Path(record["native_install_directory"]),
                              native_target=binding["target"])
    if (binding["manager"] != actual.manager or binding["target"] != actual.target
            or binding["source"] != str(actual.source) or binding["installed"] != str(actual.installed)
            or binding["sha256"] != _digest(actual.content)):
        raise UpdateError("native updater differs from retained operation binding/domain")
    return actual


def _dispatch_update_job(store):
    """Freeze each coordinator dispatch; native observation decides attach/resume."""
    jobs, installation, adapter = _retained_job(store)
    record = store.load()
    directory = Path(record["native_install_directory"])
    _check_bound_job(store, jobs, installation, adapter)
    observed = jobs.observe_update_job(installation, adapter, directory,
                                       native_target=record["updater_job"]["target"])
    prior = record.get("job_dispatch")
    if observed["state"] in {"running", "starting"}:
        return {"request_id": record["request_id"], **observed, "dispatch": "attached"}, None
    if observed["state"] not in {"inactive", "failed"}:
        raise UpdateError("independent updater job is not enrolled and inactive")
    record["job_dispatch"] = {"state": "pending", "attempt": 1 if prior is None else prior["attempt"] + 1}
    store.save(record)
    return None, (jobs, installation, adapter, directory, record["updater_job"]["target"])


def _require_expected_request(record, expected_request_id):
    """An attachment or continuation cannot adopt a replacement operation."""
    if expected_request_id is None:
        return
    try:
        if str(uuid.UUID(expected_request_id)) != expected_request_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise UpdateError("expected request must be one canonical UUID") from exc
    if record is None or record["request_id"] != expected_request_id:
        raise UpdateError(f"original update request {expected_request_id} is no longer current")


def _prepare_update_locked(store, *, target=None, initialization=None, prepare_only=False, expected_request_id=None, from_prepared_remote=False):
    """Use the held update lock for canonical preparation; never dispatch native work."""
    store._assert_locked()
    installation = store.installation
    proposed = _initialization_scope(initialization) if initialization is not None else None
    record = store.load()
    _require_expected_request(record, expected_request_id)
    pending = record is not None and (record["phase"] != "done" or record.get("health_errors"))
    if record is not None and proposed is not None:
        _check_initialization_intent(record, proposed)
    if not pending and proposed is not None and (record is not None or target is None):
        raise UpdateError("initialization requires a fresh operation and one frozen target")
    initial_scope = record.get("initialization_scope") if pending else proposed
    if prepare_only and (initial_scope is None or (record is not None and (
            record["phase"] not in {"prepare", "quiesce"}
            or record.get("job_support") is not None
            or record.get("updater_job") is not None
            or record.get("job_dispatch") is not None))):
        raise UpdateError("prepare-only requires an undispatched first-install operation")
    if pending and target is not None and target != record["target"]:
        raise UpdateError("continue the existing frozen target before requesting another")
    if pending and record["phase"] != "prepare" and record.get("job_support") is not None:
        jobs, independent, adapter = _retained_job(store)
        _authorize_retained_operator(store, adapter)
    else:
        # The current application owns operator authorization and committed
        # placement. No candidate/dependency/native effect precedes this.
        stable = runpy.run_path(str(installation.cli), run_name="_update_operator_admission")
        runtime = stable["_runtime_lock"](installation)
        try:
            stable["_check_maintenance"](installation)
            authorized = (_application_stage(store, "authorize", initialization=initial_scope)
                          if initial_scope is not None else _application_stage(store, "authorize"))
        finally:
            os.close(runtime)
        if not pending:
            if from_prepared_remote:
                target = _fetch_prepared_target(installation, target)
            if target is None:
                return record
            if record is not None:
                _application_stage(store, "retire-render")
            intent = {"initialization_scope": initial_scope} if initial_scope is not None else {}
            store.prepare_target(target, request_id=str(uuid.uuid4()), **intent)
        record = store.load()
        if record["phase"] == "prepare":
            if not record.get("candidate_render"):
                prepare_candidate_render(store, interpreter=Path(authorized["interpreter"]))
            prepare_application_operation(store, install_directory=Path(authorized["directory"]),
                                          interpreter=Path(authorized["interpreter"]))
    record = store.load()
    if prepare_only and (record["phase"] != "quiesce" or not record.get("finalized", True)):
        raise UpdateError("first-install preparation has not finalized its configuration")
    return record


def _apply_or_continue(installation, *, target=None, initialization=None, prepare_only=False, expected_request_id=None, from_prepared_remote=False):
    """The single operator route; always continue the original frozen operation."""
    dispatch = None
    with UpdateStore(installation) as store:
        record = _prepare_update_locked(store, target=target, initialization=initialization,
                                         prepare_only=prepare_only, expected_request_id=expected_request_id,
                                         from_prepared_remote=from_prepared_remote)
        if record is None or (target is None and record["phase"] == "done" and not record.get("health_errors")):
            return {"update": record, "dispatch": "nothing_pending"}
        if prepare_only:
            return {"update": record, "dispatch": "prepared"}
        if record.get("updater_job") is None:
            jobs, independent, adapter = _prepare_job_support(store)
            directory = Path(record["native_install_directory"])
            jobs.prepare_update_job(independent, adapter, directory)
            jobs.bind_update_job(store, adapter, directory)
        observed, dispatch = _dispatch_update_job(store)
        dispatched_request_id = record["request_id"]
        if dispatch is None:
            return {"update": store.load(), "job": observed}
    # The native job may acquire update.lock immediately. Never hold it while
    # starting or waiting for the manager's acknowledgement.
    jobs, independent, adapter, directory, native_target = dispatch
    outcome = jobs.run_update_job(independent, adapter, directory, native_target=native_target)
    latest = read_update(installation)
    _require_expected_request(latest, dispatched_request_id)
    return {"update": latest, "job": outcome}



def apply_or_continue(installation, *, target=None, initialization=None, prepare_only=False, expected_request_id=None, from_prepared_remote=False):
    """Prepare/dispatch once, or attach read-only to the active original job."""
    if type(prepare_only) is not bool:
        raise UpdateError("prepare-only must be an explicit boolean")
    if type(from_prepared_remote) is not bool:
        raise UpdateError("prepared-remote resolution must be an explicit boolean")
    if from_prepared_remote and (initialization is not None or prepare_only
            or target is not None and (not isinstance(target, str) or not _COMMIT.fullmatch(target))):
        raise UpdateError("remote update apply takes only an optional full Git commit")
    try:
        return _apply_or_continue(installation, target=target, initialization=initialization,
                                  prepare_only=prepare_only, expected_request_id=expected_request_id,
                                  from_prepared_remote=from_prepared_remote)
    except UpdateBusyError:
        if prepare_only:
            raise UpdateBusyError("another update is active; inspect its original operation") from None
        store = UpdateStore(installation)
        record = store.load()
        _require_expected_request(record, expected_request_id)
        if record is None or record.get("updater_job") is None:
            raise UpdateBusyError("another update is preparing; inspect its current operation") from None
        if initialization is not None:
            _check_initialization_intent(record, _initialization_scope(initialization))
        if target is not None and target != record["target"]:
            raise UpdateError("another frozen target is running; continue its original operation") from None
        jobs, independent, adapter = _retained_job(store)
        _authorize_retained_operator(store, adapter)
        _check_bound_job(store, jobs, independent, adapter)
        observed = jobs.observe_update_job(independent, adapter, Path(record["native_install_directory"]),
                                           native_target=record["updater_job"]["target"])
        latest = store.load()
        _require_expected_request(latest, record["request_id"])
        if (observed["state"] not in {"running", "starting"}
                or latest["request_id"] != record["request_id"]):
            raise UpdateBusyError("update lock is occupied; native attachment is not established") from None
        return {"update": latest, "job": {"request_id": record["request_id"], **observed, "dispatch": "attached"}}

def _run_native_update(installation):
    """Fixed job entry: kernel membership, then the one resumable coordinator."""
    with UpdateStore(installation, wait=True) as store:
        jobs, independent, adapter = _retained_job(store)
        record = store.load()
        binding = record.get("updater_job")
        if binding is None:
            raise UpdateError("independent native job has no retained binding")
        jobs.verify_update_job_runner(independent, adapter, Path(record["native_install_directory"]),
                                      native_target=binding["target"])
        _check_bound_job(store, jobs, independent, adapter)
        dispatch = record.get("job_dispatch")
        if dispatch is None or dispatch["state"] not in {"pending", "entered", "finished"}:
            raise UpdateError("native updater lacks its original dispatch intent")
        record["job_dispatch"] = {**dispatch, "state": "entered"}
        store.save(record)
        try:
            return run_prepared_update(store)
        finally:
            record = store.load()
            record["job_dispatch"] = {**record["job_dispatch"], "state": "finished"}
            store.save(record)

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    directory = Path(__file__).absolute().parent.parent
    namespace = runpy.run_path(str(directory / "bin/claudlobby"), run_name="_stable_installation")
    installation = namespace["read_installation"](directory)
    if argv == ["_run"]:
        _run_native_update(installation)
        return 0
    parser = argparse.ArgumentParser(description="Current Git installation update")
    parser.add_argument("--root", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fleet")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("host", choices=["host"])
    parser.add_argument("update", choices=["update"])
    parser.add_argument("action", nargs="?", choices=["status", "recovery", "apply", "continue"], default="status")
    parser.add_argument("target", nargs="?")
    args = parser.parse_args(argv)
    if Path(args.root) != installation.data_root:
        raise UpdateError("updater data root differs from the recorded installation")
    if args.action in {"status", "recovery"}:
        if args.target is not None:
            raise UpdateError("read-only update inspection takes no target")
        result = {"update": read_update(installation), "read_only": True}
    else:
        if args.fleet or args.seed:
            raise UpdateError("host update cannot select a fleet")
        if args.action == "apply" and args.target is not None and not _COMMIT.fullmatch(args.target):
            raise UpdateError("apply accepts only an optional full Git commit")
        if args.action == "continue" and args.target is not None:
            raise UpdateError("continue uses the existing frozen target")
        result = apply_or_continue(installation, target=args.target,
                                   from_prepared_remote=args.action == "apply")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpdateError, OSError, ValueError) as exc:
        print(f"claudlobby update: {exc}", file=sys.stderr)
        sys.exit(6)
