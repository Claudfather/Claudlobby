"""Private exact-target runtime operations for the activation coordinator.

The caller owns the host EX lock, verified publication, complete pre-stop
process/socket witnesses and serial ordering. No state-machine completion,
enrollment, discovery, migration, or fallback start happens here. Evidence is
returned for the coordinator to retain; native acknowledgement is not health.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess

from .activation_state import ActivationError, ActivationStore
from .releases import read_release
from .runtime_admission import RuntimeIdentity, UnitStart, activation_start, parse_unit_argv
from .supervision_inventory import Adapter

BOT_READY_KINDS = frozenset({"bridge-ready", "session-ready"})


class RuntimeEvidenceError(ActivationError):
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

    systemctl start and restart block through ExecStartPre, so a sealed Linux
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


def _start_budget(unit: UnitStart, file: Path, content: bytes) -> int:
    """Cover the sealed Linux bot boot rung before its admission continuation."""
    return unit_start_budget(file, content) if unit.phase == "bots" else 30


def assert_quiescent(adapter: Adapter, *, installed_file: Path, target: str,
                     socket_path: Path | None = None, known_pids: tuple[int, ...] = (),
                     known_cgroup: str = "") -> RuntimeEvidence:
    """Observe only the named unit and pre-stop witnesses; never a host-wide proof.

    A missing/refused owned Unix socket and absent known PIDs are evidence.
    Permission errors, unknown native states and occupied sockets refuse.
    Detached processes outside the supplied witnesses remain the caller's
    coverage responsibility. No process is signalled and no socket is removed.
    """
    observed = _call(adapter, "svc_activation_quiet", target,
                     installed_file, target, known_cgroup)
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
    socket_state = "not-supplied"
    if socket_path is not None:
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
    return RuntimeEvidence("quiescent", target, {
        "native": observed, "known_pids": list(known_pids), "known_cgroup": known_cgroup,
        "socket_path": str(socket_path) if socket_path is not None else None,
        "socket_state": socket_state, "coverage": "named-unit-and-supplied-witnesses-only"})


def start_unit(store: ActivationStore, activation_id: str, *, installed_file: Path,
               target: str, unit: UnitStart, sha256: str, adapter: Adapter,
               readiness=None, before_start=None) -> RuntimeEvidence:
    """Start one frozen published target and keep its grant through readiness.

    Call sequentially. The publication owner must have checked effective native
    ownership and the caller must have proved old processes quiet. ``sha256``
    is the frozen publication digest. Ingest requires a bounded read-only
    ``readiness() -> dict`` callback from its health owner, raising on failure.
    Bot readiness is the existing derived-ceiling/fresh bridge marker policy.
    Producers without a callback report only the native observed state; that
    is not a claim that a one-shot ran or that its application is healthy.
    """
    store.assert_locked()
    if parse_unit_argv(unit.argv) != unit or unit.root != store.root:
        raise RuntimeEvidenceError("start", target, "unit context differs from frozen admission")
    file = Path(installed_file)
    family = file.stem
    if (not file.is_absolute() or file.suffix not in {".service", ".timer", ".plist"}
            or family != unit.unit
            or (file.suffix == ".plist" and target.split("/")[-1] != family)
            or (file.suffix != ".plist" and target != file.name)):
        raise RuntimeEvidenceError("start", target, "target differs from published unit family")
    if unit.phase == "ingest" and readiness is None:
        raise RuntimeEvidenceError("start", target, "matching ingest readiness evidence is required")
    try:
        published_content = file.read_bytes()
        actual_digest = hashlib.sha256(published_content).hexdigest()
    except OSError as exc:
        raise RuntimeEvidenceError("start", target, "published unit is unreadable") from exc
    if actual_digest != sha256:
        raise RuntimeEvidenceError("start", target, "published bytes changed")
    release = read_release(store.root, unit.release_id)
    if adapter.package.native != release.native_path:
        raise RuntimeEvidenceError("start", target, "adapter belongs to another release")
    identity = RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)
    admission = {"kind": "unit-start-v1", "unit": unit.unit, "argv": list(unit.argv)}
    start_budget = _start_budget(unit, file, published_content)
    with activation_start(store, activation_id, identity=identity, unit=admission,
                          timeout=start_budget) as admitted:
        bot = None
        fence_details = None
        if unit.phase == "bots":
            bot = Path(unit.command[1])
            fence = _call(adapter, "svc_activation_bot_fence", target, store.root, bot)
            fields = fence.split("\t")
            if (len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) <= 0
                    or not fields[1].startswith("RR_FENCE_")):
                raise RuntimeEvidenceError("start", target, "readiness fence evidence unavailable")
            ceiling, token = int(fields[0]), fields[1]
            fence_details = {"ceiling": ceiling, "fence": token}
        if before_start is not None:
            before_start(fence_details)
        response = _call(adapter, "svc_activation_start", target, file, target,
                         timeout=start_budget)
        if response != "start-requested":
            raise RuntimeEvidenceError("start", target, "native start acknowledgement unavailable")
        # launchctl bootstrap/kickstart acknowledges a request before the
        # spawned wrapper reaches its release-bound admission. A native PID
        # snapshot can therefore see that wrapper while it is still doomed to
        # exit when this one-shot grant closes. A timer is not awaited here, but
        # an already due one can fire at once: its Linux service never uses this
        # grant and instead waits for the lock, then ordinary selected admission
        # (runtime_admission._scheduled_candidate).
        immediate = unit.mode == "exec" or file.suffix == ".service"
        if file.suffix == ".plist" and not immediate:
            try:
                immediate = plistlib.loads(published_content).get("RunAtLoad") is True
            except (ValueError, TypeError) as exc:
                raise RuntimeEvidenceError("start", target, "published launch definition is invalid") from exc
        if immediate and not admitted.wait():
            raise RuntimeEvidenceError("start", target, "native unit admission was not observed")
        result = observe_started_unit(store, installed_file=file, target=target, unit=unit,
                                      sha256=sha256, adapter=adapter,
                                      fence=fence_details, readiness=readiness)
    return result


def observe_started_unit(store: ActivationStore, *, installed_file: Path, target: str,
                         unit: UnitStart, sha256: str, adapter: Adapter,
                         fence: dict | None, readiness=None) -> RuntimeEvidence:
    """Recheck a previously issued exact start without another native start/send."""
    store.assert_locked()
    file = Path(installed_file)
    try:
        actual = hashlib.sha256(file.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeEvidenceError("start", target, "published unit is unreadable") from exc
    release = read_release(store.root, unit.release_id)
    if (actual != sha256 or adapter.package.native != release.native_path
            or unit.root != store.root or parse_unit_argv(unit.argv) != unit):
        raise RuntimeEvidenceError("start", target, "published unit or admission identity changed")
    details = {"release_id": unit.release_id, "sha256": sha256, "readiness": "not-observed"}
    if unit.phase == "bots":
        if (fence is None or type(fence.get("ceiling")) is not int or fence["ceiling"] <= 0
                or not isinstance(fence.get("fence"), str)
                or not fence["fence"].startswith("RR_FENCE_")):
            raise RuntimeEvidenceError("readiness", target, "recorded bot fence is unavailable")
        ready = _call(adapter, "svc_activation_bot_ready", target,
                      store.root, Path(unit.command[1]), fence["ceiling"], fence["fence"],
                      timeout=fence["ceiling"] + 10)
        if ready not in BOT_READY_KINDS:
            raise RuntimeEvidenceError("readiness", target, "bot readiness evidence unavailable")
        details["readiness"] = {"kind": ready, **fence}
    elif fence is not None:
        raise RuntimeEvidenceError("start", target, "non-bot start has a bot fence")
    if readiness is not None:
        try:
            evidence = readiness()
        except Exception as exc:
            raise RuntimeEvidenceError("readiness", target, "health owner refused or could not observe readiness") from exc
        if not isinstance(evidence, dict) or not evidence:
            raise RuntimeEvidenceError("readiness", target, "health owner supplied no evidence")
        details["health"] = evidence
    observed = _call(adapter, "svc_activation_snapshot", target, file, target)
    fields = observed.split()
    active_required = (file.suffix == ".timer" or unit.mode == "exec"
                       and not (unit.phase == "bots" and file.suffix == ".plist"))
    if (len(fields) != 3 or fields[1] != "loaded" or fields[2] not in {"active", "inactive"}
            or (active_required and fields[2] != "active")):
        raise RuntimeEvidenceError("start", target, "native started-state evidence unavailable")
    details["native"] = observed
    return RuntimeEvidence("started", target, details)
