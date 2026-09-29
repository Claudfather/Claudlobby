"""One bounded tracked submission through the selected native tmux owner.

The caller freezes destination identity/scope and records intent before calling.
This adapter validates literal native targets, not roster membership or runtime
health. It never records Plane facts, proves receipt, repairs Enter, or retries.
``submitted`` means the native payload/Enter call returned successfully; receiver
receipt plus wire-integrity evidence belong to the messaging operation owner.
Any started send failure is unknown unless the native owner explicitly reports
that its session precheck dropped the send before any pane write.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Literal

from .plane.ids import ID_PATTERNS
from .resources import PackageResources


@dataclass(frozen=True)
class TransportDestination:
    """Frozen context-owner output; no environment or bot-directory discovery.

    tmux_tmpdir names the server namespace fixed by composition, independently
    of the invoking shell's TMPDIR/TMUX_TMPDIR. Paths are canonicalized once.
    """

    root: Path
    fleet: str
    socket: str
    session: str
    tmux_tmpdir: Path

    def __post_init__(self):
        for field in ("root", "tmux_tmpdir"):
            value = getattr(self, field)
            if not isinstance(value, Path) or not value.is_absolute() or not value.is_dir():
                raise ValueError(f"transport {field} requires an existing absolute directory")
            object.__setattr__(self, field, value.resolve())
        for field, pattern in (("fleet", r"[A-Za-z0-9_-]+"),
                               ("socket", r"[A-Za-z0-9][A-Za-z0-9_.-]*"),
                               ("session", r"[A-Za-z0-9_-]+")):
            value = getattr(self, field)
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError(f"transport {field} must be a literal configured name")


@dataclass(frozen=True)
class TransportOutcome:
    status: Literal["submitted", "failed", "unknown"]
    wire_sha256: str | None = None
    wire_bytes: int | None = None
    native_returncode: int | None = None
    reason: str | None = None


# All data crosses positional argv or stdin. The selected helper alone sanitizes,
# chunks, appends the tracked trailer and computes the pre-trailer wire proof.
# EXIT reports its globals even on a failed chunk. Its own cleanup remains owned
# by lib-common; no payload or trace file is created by this adapter.
_SCRIPT = r'''
set -euo pipefail
. "$1" >/dev/null
_transport_result() {
    local rc=$?
    trap - EXIT
    printf 'transport-v1\tresult\t%s\t%s\t%s\n' "$rc" "${PLANE_WIRE_SHA256:-}" "${PLANE_WIRE_BYTES:-}"
    _lc_cleanup >/dev/null 2>&1
    exit "$rc"
}
trap _transport_result EXIT
body=''
IFS= read -r -d '' body || :
printf 'transport-v1\tinvoked\n'
bot_tmux_send "$2" "=$3:" "$body" >&2
'''


class _StartedFailure(RuntimeError):
    """The process existed; a failed observation is not a no-effect proof."""


def _run(command, *, input, env, timeout):
    """Bound the shell and all its native children, including cleanup waits."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException as exc:
        # No process-table scan: only this invocation's new process group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # An escaped descendant retaining a pipe cannot make cleanup unbounded.
            stdout, stderr = getattr(exc, "output", None), getattr(exc, "stderr", None)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if isinstance(exc, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
        if isinstance(exc, OSError):
            raise _StartedFailure("native process observation failed") from exc
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _proof(output: bytes | None) -> tuple[int | None, str | None, int | None]:
    match = re.fullmatch(rb"transport-v1\tinvoked\ntransport-v1\tresult\t([0-9]+)\t"
                         rb"(sha256:[0-9a-f]{64})?\t([0-9]+)?\n", output or b"")
    if not match:
        return None, None, None
    rc, digest, length = match.groups()
    if (digest is None) != (length is None):
        return None, None, None
    return int(rc), digest.decode() if digest else None, int(length) if length else None


def send(package: PackageResources, destination: TransportDestination, *, message_id: str,
         body: str, timeout: float = 30, runner=None) -> TransportOutcome:
    """Attempt exactly one native submission; no automatic payload/Enter retry.

    The runner seam has subprocess byte-input semantics. Only pre-call validation
    inability to launch, and native no-session preflight are definite failures.
    A timeout, partial send, malformed response, or interrupted native call is unknown.
    Wire proof describes prepared native bytes, not delivery, and may be absent
    even after submission.
    Null bytes cannot cross Bash's string boundary and are refused before effects.
    PANE_SEND_VERIFY_TICKS=0 disables both post-send Enter and blind-payload
    repairs. Native chunk/settle defaults remain intact; readiness/health admission
    and a later receipt-gated idle Enter repair belong to the operation owner.
    """
    if not isinstance(destination, TransportDestination):
        raise ValueError("a frozen TransportDestination is required")
    if not isinstance(message_id, str) or not re.fullmatch(ID_PATTERNS["msg"], message_id):
        raise ValueError("canonical message ID required")
    if not isinstance(body, str) or not body or "\0" in body:
        raise ValueError("transport body must be nonempty text without null bytes")
    payload = body.encode("utf-8")
    if isinstance(timeout, bool) or not 0 < timeout <= 120:
        raise ValueError("transport timeout must be finite and in (0, 120]")
    native = package.native / "lib-common.sh"
    if not native.is_absolute() or not native.is_file():
        return TransportOutcome("failed", reason="selected native helper unavailable")
    # No inherited shell startup/functions, tool overrides, trace/output paths,
    # recording controls or transport IDs. The adapter owns these exact controls.
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
           "LC_ALL": "C", "CLAUDLOBBY_ROOT": str(destination.root),
           "FLEET_NAME": destination.fleet, "TMUX_TMPDIR": str(destination.tmux_tmpdir),
           "TMPDIR": str(destination.tmux_tmpdir), "PLANE_MSG_ID": message_id,
           "PLANE_EMIT_DISABLED": "1", "PANE_SEND_VERIFY_TICKS": "0"}
    command = ["/bin/bash", "--noprofile", "--norc", "-c", _SCRIPT, "message-transport",
               str(native), destination.socket, destination.session]
    try:
        result = (runner or _run)(command, input=payload, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _, digest, length = _proof(exc.output)
        return TransportOutcome("unknown", digest, length, reason="native submission timed out")
    except OSError:
        return TransportOutcome("failed", reason="native process could not be launched")
    except _StartedFailure:
        return TransportOutcome("unknown", reason="native process observation failed")
    rc, digest, length = _proof(result.stdout)
    if rc == result.returncode == 0:
        return TransportOutcome("submitted", digest, length, 0)
    stderr = result.stderr or b""
    if (rc == result.returncode == 1 and digest is None and length is None
            and b"bot_tmux_send: session '" in stderr
            and b" not found on socket '" in stderr
            and b"send dropped (logged)" in stderr):
        return TransportOutcome("failed", native_returncode=1,
                                reason="native session precheck dropped the send")
    return TransportOutcome("unknown", digest, length, result.returncode,
                            "native submission did not complete with a valid success result")
