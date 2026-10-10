"""Passive pre-activation facts; never open Plane or execute discovered tools."""

from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import shutil
import stat
import sys

from . import resources


RUNTIME_EXECUTABLES = ("tmux", "claude", "jq")


class ObservationError(ValueError):
    """Fixed classifications only; no path or operating-system exception text."""

    def __init__(self, area, state):
        self.area, self.state = area, state
        super().__init__(f"{area} is {state}")


def directory_state(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unavailable"
    if stat.S_ISLNK(mode):
        return "redirected"
    return "directory" if stat.S_ISDIR(mode) else "invalid"


def status_root(value):
    """Inspect only a root the existing owner reported absent; never create it."""
    try:
        supplied = Path(value).expanduser()
        if not supplied.is_absolute():
            raise ObservationError("argument", "invalid")
        supplied_state = directory_state(supplied)
        if supplied_state == "invalid":
            return supplied.resolve(), "invalid"
        if supplied_state == "redirected":
            raise ObservationError("root", "redirected")
        # Match the established owner's normalization, including /tmp on macOS.
        absolute = supplied.resolve()
        for parent in reversed((absolute, *absolute.parents)):
            state = directory_state(parent)
            if state not in {"absent", "directory"}:
                raise ObservationError("root", state)
            if state == "absent":
                return absolute, "absent"
        state = directory_state(absolute / "state")
        if state not in {"absent", "directory"}:
            raise ObservationError("state", state)
        return absolute, "directory"
    except (ValueError, RuntimeError) as exc:
        if isinstance(exc, ObservationError):
            raise
        raise ObservationError("argument", "invalid") from exc


def _plane_storage(root):
    # Recheck the parent before traversal; no readonly SQLite connection/sidecar.
    for path in (root, root / "state", root / "state" / "plane"):
        state = directory_state(path)
        if state != "directory":
            return state
    try:
        with os.scandir(root / "state" / "plane") as entries:
            return "empty" if next(entries, None) is None else "nonempty"
    except OSError:
        return "unavailable"


def observe(root, root_state):
    """Sequential local observations, not a snapshot or activation authority."""
    try:
        package = resources.get_resources()
        identity = {name: getattr(package, name) for name in (
            "artifact_id", "source_revision", "content_sha256")}
        if (any(not isinstance(identity[name], str) or not identity[name]
                for name in ("artifact_id", "content_sha256"))
                or identity["source_revision"] is not None
                and not isinstance(identity["source_revision"], str)):
            raise ValueError("invalid package identity")
        package_data = {"state": "present", **identity}
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError):
        package_data = {"state": "unavailable", "artifact_id": None,
                        "source_revision": None, "content_sha256": None}
    executables = {}
    for name in RUNTIME_EXECUTABLES:
        try:
            executables[name] = "present" if shutil.which(name) is not None else "missing"
        except (OSError, ValueError):
            executables[name] = "unavailable"
    return {
        "version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "python": platform.python_version()},
        "limitations": [] if sys.platform in {"linux", "darwin"} else ["platform_unsupported"],
        "package": package_data, "executables": executables,
        "root": root_state, "plane_storage": _plane_storage(root),
        "not_checked": ["native_manager_session", "provider_login", "repository_access",
                        "browser_pairing", "runtime_readiness", "bootstrap_eligibility"],
    }
