"""Stdlib current-update record and mechanical Git cutover.

The application owner supplies configuration consent and freshly observed native
quiet receipts. This module never saves/stops/starts bots, migrates SQL, or runs
commands supplied by a record. Its installed status entry survives src/env loss.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile
import uuid


PHASES = ("prepare", "quiesce", "git", "environment", "publish", "migrate", "commit", "restore", "done")
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class UpdateError(ValueError):
    """Current operation is unavailable, changed, or not admitted for cutover."""


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


def _lock_bytes(installation, revision, path):
    content = _git(installation, "show", f"{revision}:{path}").stdout
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
    return content


def _validate(installation, record):
    try:
        if (type(record) is not dict or type(record["schema"]) is not int or record["schema"] != 1
                or record["installation"] != str(installation.directory)
                or record["data_root"] != str(installation.data_root)
                or record["phase"] not in PHASES or type(record["maintenance"]) is not bool
                or not all(_COMMIT.fullmatch(record[key]) for key in ("before", "target"))
                or not _SHA.fullmatch(record["config_fingerprint"])
                or type(record["scope"]) is not dict or type(record["quiet"]) is not dict
                or type(record["git_complete"]) is not bool):
            raise ValueError()
        if ((record["maintenance"] and record["phase"] in {"prepare", "quiesce", "restore", "done"})
                or (not record["maintenance"] and record["phase"] in {"git", "environment", "publish", "migrate"})):
            raise ValueError()
        uuid.UUID(record["request_id"])
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
    def __init__(self, installation):
        self.installation = installation
        self.root = installation.data_root
        self.state = _plain(installation.data_root / "state")
        self.work = _plain(self.state / "update-work")
        self._fd = None

    def __enter__(self):
        self.state.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.state / "update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise UpdateError("another update is running or its lock is unavailable") from exc
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
        if current is not None and not (current["phase"] == "done" and record["phase"] == "prepare"):
            for key in ("request_id", "before", "target", "config_fingerprint", "scope"):
                if current[key] != record[key]:
                    raise UpdateError("frozen update intent changed")
            for key in ("path", "before", "target"):
                if current["dependencies"][key] != record["dependencies"][key]:
                    raise UpdateError("frozen dependency intent changed")
        _write(self.state / "update.json", json.dumps(record, sort_keys=True, separators=(",", ":")).encode())

    def prepare(self, target_commit, *, config_fingerprint, scope, request_id,
                lock_path="requirements-runtime.lock"):
        """Freeze already-resolved trusted Git inputs. Never fetch or resolve a moving ref."""
        self._assert_locked()
        current = self.load()
        if current is not None and current["phase"] != "done":
            if (current["target"], current["config_fingerprint"], current["scope"], current["request_id"]) != (
                    target_commit, config_fingerprint, scope, request_id):
                raise UpdateError("current operation differs; continue it before preparing another")
            return current
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
                  "before": before, "target": target_commit, "config_fingerprint": config_fingerprint,
                  "scope": deepcopy(scope), "quiet": {}, "phase": "prepare", "maintenance": False,
                  "git_complete": False,
                  "dependencies": {"path": lock_path, "before": _digest(old_lock), "target": _digest(new_lock),
                                   "prepared": old_lock == new_lock, "receipt_sha256": None}}
        _validate(self.installation, record)
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(self.work / "dependencies-before.lock", old_lock)
        _write(self.work / "dependencies-target.lock", new_lock)
        self.save(record)
        return record

    def _dependency_inputs(self, record):
        for name in ("before", "target"):
            path = _plain(self.work / f"dependencies-{name}.lock")
            if _digest(path.read_bytes()) != record["dependencies"][name]:
                raise UpdateError("frozen dependency input changed")

    def _dependency_run(self, arguments):
        # Neither ambient pip configuration nor Python import paths select inputs.
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
        self._dependency_run([python, "-I", "-m", "pip", "--isolated", "install",
            "--disable-pip-version-check", "--no-index", "--find-links", self.work / f"wheels-{name}",
            "--only-binary=:all:", "--require-hashes", "--no-deps", "-r",
            self.work / f"dependencies-{name}.lock"])
        self._dependency_run([python, "-I", "-m", "pip", "--isolated", "check"])

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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent current-update inspection")
    parser.add_argument("--root", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fleet")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("host", choices=["host"])
    parser.add_argument("update", choices=["update"])
    parser.add_argument("action", nargs="?", choices=["status", "recovery"])
    args = parser.parse_args(argv)
    directory = Path(__file__).absolute().parent.parent
    namespace = runpy.run_path(str(directory / "bin/claudlobby"), run_name="_stable_installation")
    installation = namespace["read_installation"](directory)
    if Path(args.root) != installation.data_root:
        raise UpdateError("updater data root differs from the recorded installation")
    if args.action is None:
        raise UpdateError("application update owner is not bound; no effects attempted")
    print(json.dumps({"update": read_update(installation), "read_only": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpdateError, OSError, ValueError) as exc:
        print(f"claudlobby update: {exc}", file=sys.stderr)
        sys.exit(6)
