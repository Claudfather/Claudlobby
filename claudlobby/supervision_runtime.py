"""Exact native runtime evidence, independent of update or activation state.

Callers own authorization, publication, stop intent and ordering. These
primitives observe only named native units and supplied witnesses; they do
not discover consumers, acquire an update lock, save sessions or start jobs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .supervision_inventory import Adapter


class RuntimeEvidenceError(RuntimeError):
    """A named runtime operation failed or its required evidence is unavailable."""

    def __init__(self, operation: str, target: str, detail: str):
        self.operation, self.target, self.detail = operation, target, detail
        super().__init__(f"{operation} {target}: {detail}")


@dataclass(frozen=True)
class RuntimeEvidence:
    operation: str
    target: str
    details: dict

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                              separators=(",", ":")).encode()).hexdigest()


def _call(adapter, function, target, *args, timeout=30):
    try:
        result = adapter.call(function, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeEvidenceError(function, target, "native observation failed or timed out") from exc
    if result.returncode:
        raise RuntimeEvidenceError(function, target, f"native refusal ({result.returncode})")
    return result.stdout.strip()


def unit_start_budget(file: Path, content: bytes) -> int:
    """Seconds a native call that starts this unit may block: 30 plus its boot delay.

    systemctl start and restart block through ExecStartPre, so a published Linux
    unit's /bin/sleep stagger is spent inside the call (#2087).
    """
    if file.suffix != ".service":
        return 30
    try:
        lines = [line.strip() for line in content.decode("utf-8").splitlines()
                 if line.strip().startswith("ExecStartPre=")]
    except UnicodeDecodeError as exc:
        raise RuntimeEvidenceError("start", file.name, "published unit is not UTF-8") from exc
    if not lines:
        return 30
    if len(lines) != 1 or not re.fullmatch(r"ExecStartPre=/bin/sleep (0|[1-9][0-9]{0,3})", lines[0]):
        raise RuntimeEvidenceError("start", file.name, "unsupported published boot delay")
    delay = int(lines[0].rsplit(" ", 1)[-1])
    if delay > 3600:
        raise RuntimeEvidenceError("start", file.name, "published boot delay exceeds bound")
    return 30 + delay


def assert_private_endpoint_quiet(socket_path: Path, *, target: str) -> str:
    """Prove only this owned endpoint absent/refused; never signal or unlink it."""
    socket_path = Path(socket_path)
    if not socket_path.is_absolute():
        raise RuntimeEvidenceError("quiescence", target, "socket witness must be absolute")
    try:
        info = socket_path.lstat()
    except FileNotFoundError:
        socket_state = "absent"
    except OSError as exc:
        raise RuntimeEvidenceError("quiescence", target, "socket observation unavailable") from exc
    else:
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeEvidenceError("quiescence", target, "socket witness is foreign or not a socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            try:
                probe.connect(str(socket_path))
            except ConnectionRefusedError:
                socket_state = "refused"
            except OSError as exc:
                raise RuntimeEvidenceError("quiescence", target, "socket observation unavailable") from exc
            else:
                raise RuntimeEvidenceError("quiescence", target, "known socket still accepts connections")
    return socket_state


def assert_quiescent(adapter: Adapter, *, installed_file: Path, target: str,
                     socket_path: Path | None = None, known_pids: tuple[int, ...] = (),
                     known_cgroup: str = "", settle_s: int = 0) -> RuntimeEvidence:
    """Observe only the named unit and pre-stop witnesses; never a host-wide proof.

    A missing/refused owned Unix socket and absent known PIDs are evidence.
    Permission errors, unknown native states and occupied sockets refuse.
    Detached processes outside the supplied witnesses remain the caller's
    coverage responsibility. No process is signalled and no socket is removed.

    ``settle_s`` is for a caller that has just stopped the unit: the native
    check then waits up to that many seconds for processes still exiting to
    leave the unit's cgroup (#2227). Without it the cgroup is read once.
    """
    if type(settle_s) is not int or not 0 <= settle_s <= 999:
        raise RuntimeEvidenceError("quiescence", target, "invalid settle window")
    observed = _call(adapter, "svc_activation_quiet", target,
                     installed_file, target, known_cgroup,
                     *((str(settle_s),) if settle_s else ()), timeout=30 + settle_s)
    if observed not in {"inactive\tcgroup-empty", "inactive\tno-cgroup-witness"}:
        raise RuntimeEvidenceError("quiescence", target, "unrecognized native evidence")
    for pid in known_pids:
        if type(pid) is not int or pid <= 1:
            raise RuntimeEvidenceError("quiescence", target, "invalid pre-stop PID")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise RuntimeEvidenceError("quiescence", target, "PID observation unavailable") from exc
        raise RuntimeEvidenceError("quiescence", target, f"known process {pid} remains")
    socket_state = (assert_private_endpoint_quiet(socket_path, target=target)
                    if socket_path is not None else "not-supplied")
    return RuntimeEvidence("quiescent", target, {
        "native": observed, "known_pids": list(known_pids), "known_cgroup": known_cgroup,
        "socket_path": str(socket_path) if socket_path is not None else None,
        "socket_state": socket_state, "coverage": "named-unit-and-supplied-witnesses-only"})


def assert_native_absent(adapter, *, manager: str, domain: str, target: str, installed_file: Path,
                         search_directories, socket_path: Path | None = None) -> RuntimeEvidence:
    """Reobserve frozen catalog coverage and positive absence of one new unit.

    The inventory owner supplies its previously validated search directories.
    This checks those witnesses, without discovering or adopting consumers.
    """
    file = Path(installed_file)
    directories = tuple(Path(item) for item in search_directories)
    if (manager not in {"Linux", "Darwin"} or not directories or file.parent not in directories
            or any(not item.is_absolute() or item.resolve() != item for item in directories)):
        raise RuntimeEvidenceError("absence", target, "invalid frozen native placement")
    catalog = _call(adapter, "svc_inventory_catalog", target)
    lines = catalog.splitlines()
    expected_domain = target.rsplit("/", 1)[0] if manager == "Darwin" else ""
    if domain != expected_domain:
        raise RuntimeEvidenceError("absence", target, "native target differs from frozen domain")
    if ([line for line in lines if line.startswith("manager\t")] != ["manager\t" + manager]
            or [line for line in lines if line.startswith("directory\t")] !=
               ["directory\t" + str(item) for item in directories]
            or manager == "Darwin" and [line for line in lines if line.startswith("domain\t")] != ["domain\t" + expected_domain]):
        raise RuntimeEvidenceError("absence", target, "native catalog placement changed")
    if any((directory / file.name).exists() or (directory / file.name).is_symlink() for directory in directories):
        raise RuntimeEvidenceError("absence", target, "added native definition already exists")
    template = manager == "Linux" and "@." in file.name
    if template:
        prefix = "loaded\t" + file.name.split("@", 1)[0] + "@"
        if any(line.startswith(prefix) for line in lines):
            raise RuntimeEvidenceError("absence", target, "added native template has loaded instances")
        observed = "template absent"
    else:
        observed = _call(adapter, "svc_inventory_state", target, file, target)
        expected = "not-found not-found inactive" if manager == "Linux" else "unchanged unloaded inactive"
        if observed != expected:
            raise RuntimeEvidenceError("absence", target, "added native target absence is unproved")
    if socket_path is not None and (socket_path.exists() or socket_path.is_symlink()):
        raise RuntimeEvidenceError("absence", target, "added bot has a preexisting private session socket")
    return RuntimeEvidence("absence", target, {"manager": manager, "target": target,
                           "destination": str(file), "absent": True, "native": observed})
