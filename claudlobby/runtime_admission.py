"""Release selection and activation admission for ordinary public mutations.

Hold the shared host lock through the operation. This prevents selection from
changing between validation and effects; the coordinator requires its exclusive
side. No lock/state provisioning, application import, supervisor, or migration
occurs here. Read-only diagnosis and activation itself do not use this door.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path

from .activation_state import ActivationError, read_activation, read_selection
from .releases import ReleaseManifest, read_release


class ReleaseMismatch(ActivationError):
    """Wrong executable or generated context; no operation may begin."""

    code = "release_mismatch"
    exit_code = 7

    def __init__(self, root: Path, expected: str, actual: str):
        self.root, self.expected, self.actual = root, expected, actual
        self.hint = (f"Use the selected release under {root}; inspect host releases "
                     "and recompose stale bot configuration through host activation")
        super().__init__(f"release mismatch: expected {expected}, found {actual}; no mutation performed")


@dataclass(frozen=True)
class RuntimeIdentity:
    """Actual executable/package paths, never a caller-supplied release claim."""

    cli: Path
    native: Path
    artifact_id: str

    @classmethod
    def current(cls) -> RuntimeIdentity:
        from .resources import get_resources, selected_cli
        package = get_resources()
        return cls(selected_cli(), package.native, package.artifact_id)


def _selected_active(root: Path, identity: RuntimeIdentity,
                     expected_release: str | None) -> ReleaseManifest:
    selected = read_selection(root)
    if selected is None:
        raise ActivationError(f"no active release under {root}; host setup/activation is required")
    actual = selected["release_id"]
    if expected_release is not None and expected_release != actual:
        raise ReleaseMismatch(root, expected_release, actual)
    record = read_activation(root, selected["activation_id"])
    if (record.status != "active" or record.body["intent"]["release_id"] != actual
            or record.body["intent"]["plan_id"] != selected["plan_id"]):
        raise ActivationError("selected release activation is incomplete; recover it before mutations")
    # A cutover can have paused producers before replacing the selector. Checking
    # only the selected activation would admit old commands after an interruption.
    for path in (root / "state/activations").iterdir():
        journal = path / "activation.json"
        if journal.exists() or journal.is_symlink():
            pending = read_activation(root, path.name)
            if pending.status not in {"active", "rolled_back"}:
                raise ActivationError(f"unfinished activation {path.name}; recover it before mutations")
    release = read_release(root, actual, verify_files=False)
    if (identity.cli.absolute() != release.cli_path
            or identity.native.absolute() != release.native_path
            or identity.artifact_id != release.inputs.artifact_id):
        raise ReleaseMismatch(root, actual, f"{identity.artifact_id} at {identity.cli}")
    return release


@contextmanager
def mutation_admission(root: Path, *, identity: RuntimeIdentity | None = None,
                       expected_release: str | None = None):
    """Admit one operation under stable host selection, or refuse before effects.

    Generated callers supply their bound expected release. Bare operator calls
    omit it but must still run the selected executable and installed resources.
    This guard is not authentication between trusted local callers.
    """
    root = Path(root).expanduser().resolve()
    state = root / "state"
    if state.resolve() != state:
        raise ActivationError("activation state directory is redirected")
    lock = state / "activation.lock"
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ActivationError(f"host activation lock unavailable under {root}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActivationError("host activation is running; no mutation performed") from exc
        info, linked = os.fstat(fd), lock.lstat()
        if (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino):
            raise ActivationError("host activation lock was replaced")
        yield _selected_active(root, identity or RuntimeIdentity.current(), expected_release)
    finally:
        os.close(fd)
