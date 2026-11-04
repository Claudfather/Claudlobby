"""Current Git runtime admission for operational mutations and unit generation.

The copied stdlib launcher owns native startup and the runtime-use fence.
Historical release/activation records never authorize execution through this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING

from .activation_state import ActivationError
from .installation import unit_systemd_command

if TYPE_CHECKING:
    from .installation import Installation

RESIDENT_UNIT_PHASES = {"plane-daemon": "ingest"}
UNIT_STARTED_ENV = "CLAUDLOBBY_UNIT_STARTED"


def wrap_unit_argv(environment: dict[str, str], *, unit: str, phase: str,
                   mode: str, argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Bind every generated native command to the stable Git admission entry."""
    from .installation import parse_git_unit_argv
    if (not environment.get("CLAUDLOBBY_INSTALLATION")
            or any(key in environment for key in ("CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID"))):
        raise ActivationError("native unit generation requires a managed Git installation")
    result = (environment["CLAUDLOBBY_CLI"], "--root", environment["CLAUDLOBBY_ROOT"],
              "_native", "--unit", unit, "--phase", phase, "--mode", mode, "--", *argv)
    parse_git_unit_argv(result)
    return result



def unit_systemd_environment(key: str, value: str) -> str:
    """One literal Environment= assignment; $ has no expansion in this field.

    systemd.exec(5) requires quoting the whole assignment, not just its value.
    Percent specifiers expand even inside quotes. Keep simple assignments bare
    so existing source-only units retain their spelling.
    """
    if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str) or not value.isprintable() and value):
        raise ActivationError("native environment requires printable literal assignments")
    escaped = value.replace("%", "%%")
    assignment = f"{key}={escaped}"
    if not re.fullmatch(r"[A-Za-z0-9_./:@+,-]*", escaped):
        assignment = '"' + assignment.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "Environment=" + assignment



class ReleaseMismatch(ActivationError):
    """Wrong executable or generated context; no operation may begin."""

    code = "release_mismatch"
    exit_code = 7

    def __init__(self, root: Path, expected: str, actual: str):
        self.root, self.expected, self.actual = root, expected, actual
        self.hint = (f"Use the stable managed Git CLI for {root}; inspect host update status "
                     "and preserve the original operation when configuration is stale")
        super().__init__(f"release mismatch: expected {expected}, found {actual}; no mutation performed")



@dataclass(frozen=True)
class RuntimeIdentity:
    """Actual executable/package paths, never a caller-supplied release claim."""

    cli: Path
    native: Path
    artifact_id: str
    installation: Installation | None = None
    source_revision: str | None = None

    @classmethod
    def current(cls) -> RuntimeIdentity:
        from .resources import get_resources, selected_cli
        package = get_resources()
        installation = package.installation
        if installation is not None:
            if (Path(__file__).resolve() != installation.source / "claudlobby/runtime_admission.py"
                    or Path(sys.executable).absolute() != installation.environment / "bin/python"
                    or package.native != installation.native or not package.source_revision):
                raise ActivationError("executing Git runtime differs from its recorded installation")
        if installation is None:
            raise ActivationError("executing runtime requires a managed Git installation")
        return cls(selected_cli(), package.native, package.artifact_id, installation, package.source_revision)



@dataclass(frozen=True)
class GitCurrentRuntime:
    """Committed Git runtime provenance; never a release or action grant."""

    installation: Installation
    revision: str
    config_fingerprint: str
    snapshot: dict



def bound_runtime_release(package, *, generated):
    """Require current Git execution; historical release IDs grant no admission."""
    from .command_result import CommandFailure
    if package.installation is None or any(key in os.environ for key in (
            "CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID")):
        raise CommandFailure("release_mismatch", "caller requires managed Git runtime provenance")
    return None



def runtime_release_id(current):
    """Git admission has no sealed release identity."""
    return None



def runtime_provenance(value):
    """Add Git provenance to presentation without changing legacy payloads."""
    revision = getattr(value, "revision", getattr(value, "source_revision", None))
    return ({"source_revision": revision, "config_fingerprint": value.config_fingerprint}
            if revision is not None else {})



def runtime_route_matches(current, route, *, package=None):
    """Compare admitted provenance; caller/actor grants stay with the operation."""
    package = package if package is not None else route.selected.paths.package
    revision = getattr(route, "revision", getattr(route, "source_revision", None))
    fingerprint = getattr(route, "config_fingerprint", None)
    if isinstance(current, GitCurrentRuntime):
        return (route.release_id is None and getattr(route, "activation_id", None) is None
                and getattr(route, "plan_id", None) is None
                and revision == current.revision and fingerprint == current.config_fingerprint
                and package.installation == current.installation
                and package.source_revision == current.revision)
    return False



@contextmanager
def _git_mutation_admission(root: Path, identity: RuntimeIdentity | None, expected_release):
    from .active_config import read_current_config
    from .config_plan import PlanError
    from .installation import InstallationError, read_installation, _runtime_lock, _check_maintenance

    if expected_release is not None or any(key in os.environ for key in ("CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID")):
        raise ActivationError("Git runtime cannot admit a sealed release expectation")
    fd = None
    try:
        selected = os.environ.get("CLAUDLOBBY_INSTALLATION")
        if selected is None and identity is not None and identity.installation is not None:
            selected = str(identity.installation.directory)
        if selected is None:
            raise ActivationError("Git mutation requires its recorded installation")
        installation = read_installation(Path(selected))
        if installation.data_root != root:
            raise ActivationError("Git mutation data root differs from its installation")
        # A separate nonblocking SH description nests under the launcher's SH.
        # Closing only our description never releases that inherited lifetime lock.
        fd = _runtime_lock(installation)
        _check_maintenance(installation)
        actual = RuntimeIdentity.current()
        if (actual.installation != installation or actual.source_revision is None
                or (identity is not None and identity != actual)):
            raise ActivationError("Git runtime identity differs from the executing installation")
        snapshot = read_current_config(root, installation=installation, revision=actual.source_revision)
        _check_maintenance(installation)
        current = GitCurrentRuntime(installation, actual.source_revision, snapshot["fingerprint"], snapshot)
    except (InstallationError, PlanError, OSError, RuntimeError, ValueError) as exc:
        if fd is not None:
            os.close(fd)
        if isinstance(exc, ActivationError):
            raise
        raise ActivationError(f"Git mutation admission refused: {exc}") from exc
    try:
        yield current
    finally:
        os.close(fd)



@contextmanager
def mutation_admission(root: Path, *, identity: RuntimeIdentity | None = None,
                       expected_release: str | None = None):
    """Admit only the executing managed Git runtime and committed configuration."""
    root = Path(root).expanduser().resolve()
    identity = identity if identity is not None else RuntimeIdentity.current()
    if identity.installation is None:
        raise ActivationError("mutation requires a managed Git installation")
    with _git_mutation_admission(root, identity, expected_release) as current:
        yield current



if __name__ == "__main__":
    raise SystemExit("native startup requires the stable managed Git installation CLI")
