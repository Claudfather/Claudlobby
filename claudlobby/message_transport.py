"""One bounded tracked submission through the selected native tmux owner.

The caller freezes destination identity/scope and records intent before calling.
This adapter validates literal native targets, not roster membership or runtime
health. It never records Plane facts, proves receipt, or retries a payload.
``submitted`` means the native payload/Enter call returned successfully; receiver
receipt plus wire-integrity evidence belong to the messaging operation owner.
Any started send failure is unknown unless the native owner explicitly reports
that its session precheck or occupied-input check dropped the send before any
pane write.

``press_held_enter`` is the second native call, made only by the operation owner
after a receipt wait found no receipt (#2105): it reads the pane, and presses one
Enter only when ``held_delivery_match`` finds this message there: its text, or one
paste chip in a box that read_box found empty just before the send.
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
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # Give EXIT traps a bounded chance to remove credential files, then
            # enforce the deadline even if a child ignores TERM or holds a pipe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
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
    inability to launch, native no-session preflight, and observed occupied input
    refused before writing are definite failures.
    A timeout, partial send, malformed response, or interrupted native call is unknown.
    Wire proof describes prepared native bytes, not delivery, and may be absent
    even after submission.
    Null bytes cannot cross Bash's string boundary and are refused before effects.
    PANE_SEND_VERIFY_TICKS=0 disables both post-send Enter and blind-payload
    repairs. Native chunk/settle defaults remain intact; readiness/health admission
    and the receipt-gated idle Enter repair (press_held_enter) belong to the
    operation owner.
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
        return TransportOutcome("failed",
                                reason="native session precheck dropped the send")
    if (rc == result.returncode == 4
            and b"pane_send: recipient-input-held; no payload or Enter was sent\n" in stderr):
        # The helper computes the wire proof before admission. These prepared
        # bytes never crossed the pane, and must not be reported as sent proof.
        return TransportOutcome("failed",
                                reason="recipient input already held text; nothing was sent")
    return TransportOutcome("unknown", digest, length, result.returncode,
                            "native submission did not complete with a valid success result")


@dataclass(frozen=True)
class EnterRepairOutcome:
    """One look at the recipient's box, and whether one Enter followed it.

    verdict is held_delivery_match's (text, chip, busy, not-held, not-shown, glued,
    chips, chip-lines, chip-unproven), ``changed`` when the box no longer holds what an earlier
    look matched, or ``unknown`` when the pane could not be read or the native call
    failed. match names what was matched (``text``, or ``chip#N`` with the chip's
    number) so a later look can require the same; pressed is true only when the
    Enter keystroke itself was sent."""
    verdict: str
    pressed: bool
    match: str | None = None
    reason: str | None = None


# Read the exact pane, ask the shipped predicate, and press one Enter on its
# text/chip verdict only, and, when an earlier look is named ($5), only if this
# look matches the same thing: the same message's text, or the same chip number
# (a chip's "+N lines" can drop by one once a first Enter strips a kept CR).
_REPAIR_SCRIPT = r'''
set -uo pipefail
. "$1" >/dev/null 2>&1 || { printf 'repair-v1\tunknown\t0\t-\n'; exit 3; }
_repair_look_and_press() {
    local pane verdict ok match pressed
    pane=$(bot_tmux "$2" capture-pane -p -t "=$3:" 2>/dev/null) || { printf 'repair-v1\tunknown\t0\t-\n'; return 3; }
    verdict=$(held_delivery_match "$pane" "$4" "${6:-}") && ok=1 || ok=0
    match=-
    if [ "$ok" = 1 ]; then
        match="$verdict"
        if [ "$verdict" = chip ]; then
            match="chip$(printf '%s\n' "$(pane_input_region "$pane")" | LC_ALL=C grep -oE '\[Pasted text #[0-9]+' | head -1 | LC_ALL=C sed 's/.*#/#/')"
        fi
        if [ -n "${5:-}" ] && [ "$match" != "$5" ]; then verdict=changed; ok=0; fi
    fi
    pressed=0
    if [ "$ok" = 1 ]; then
        bot_tmux "$2" send-keys -t "=$3:" Enter >/dev/null 2>&1 && pressed=1
    fi
    printf 'repair-v1\t%s\t%s\t%s\n' "${verdict:-unknown}" "$pressed" "$match"
}
# The look and its Enter hold the pane's send lock (#2036), so no other sender
# types between them and the Enter never lands inside another sender's chunks.
# The wait, 5 s, covers a send in flight (a 4 KB one holds the lock about 2 s)
# inside this call's own 15 s bound. Refused, it looks at nothing and presses
# nothing.
rc=0
_pane_with_send_lock enter 5 "$2" "$3" _repair_look_and_press "$@" || rc=$?
[ "$rc" -ne 75 ] || printf 'repair-v1\tunknown\t0\t-\n'
_lc_cleanup >/dev/null 2>&1
[ "$rc" -ne 3 ] || exit 3
exit 0
'''

_VERDICTS = frozenset({"text", "chip", "busy", "not-held", "not-shown", "glued", "chips", "chip-lines",
                       "chip-unproven", "changed", "unknown"})


def press_held_enter(package: PackageResources, destination: TransportDestination, *,
                     message_id: str, expect: str | None = None, before: str | None = None,
                     timeout: float = 15, runner=None) -> EnterRepairOutcome:
    """Press ONE Enter in the recipient's box when it holds this message.

    Never sends the payload, never a second key. ``expect`` is an earlier look's
    match: this look presses only if it matches the same. ``before`` is read_box's
    answer from just before the send: a lone paste chip carries no message id, so
    it is pressed only when the box was ``empty`` then. The operation owner
    decides whether a second look is due. The same fixed native environment as
    send(): no inherited shell state, the Plane unreachable from the native side
    (the owner records the repair), the exact =session: target."""
    if not isinstance(destination, TransportDestination):
        raise ValueError("a frozen TransportDestination is required")
    if not isinstance(message_id, str) or not re.fullmatch(ID_PATTERNS["msg"], message_id):
        raise ValueError("canonical message ID required")
    if expect is not None and not re.fullmatch(r"text|chip#[0-9]+", expect):
        raise ValueError("expect names an earlier match: text or chip#N")
    if before is not None and before not in _BOX_STATES:
        raise ValueError("before is read_box's answer: empty, held or unknown")
    if isinstance(timeout, bool) or not 0 < timeout <= 60:
        raise ValueError("repair timeout must be finite and in (0, 60]")
    native = package.native / "lib-common.sh"
    if not native.is_absolute() or not native.is_file():
        return EnterRepairOutcome("unknown", False, reason="selected native helper unavailable")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
           "LC_ALL": "C", "CLAUDLOBBY_ROOT": str(destination.root),
           "FLEET_NAME": destination.fleet, "TMUX_TMPDIR": str(destination.tmux_tmpdir),
           "TMPDIR": str(destination.tmux_tmpdir), "PLANE_EMIT_DISABLED": "1"}
    command = ["/bin/bash", "--noprofile", "--norc", "-c", _REPAIR_SCRIPT, "message-enter-repair",
               str(native), destination.socket, destination.session, message_id, expect or "",
               before or ""]
    try:
        result = (runner or _run)(command, input=b"", env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # The keystroke may or may not have gone: report what was printed, never guess.
        line = _repair_line(exc.output)
        if line is None:
            return EnterRepairOutcome("unknown", False, reason="native repair timed out")
        return EnterRepairOutcome(*line)
    except (OSError, _StartedFailure):
        return EnterRepairOutcome("unknown", False, reason="native repair could not run")
    line = _repair_line(result.stdout)
    if line is None:
        return EnterRepairOutcome("unknown", False, reason="native repair gave no valid result")
    verdict, pressed, match = line
    return EnterRepairOutcome(verdict, pressed, match,
                              "pane could not be read" if verdict == "unknown" else None)


# One read of the recipient's box just before a send (#2105, chip identity): does
# it already hold text, a person's or an earlier delivery's? The same exact pane
# and the shared pane_is_held as the repair's look. Nothing is sent.
_BOX_SCRIPT = r'''
set -uo pipefail
. "$1" >/dev/null 2>&1 || { printf 'box-v1\tunknown\n'; exit 3; }
pane=$(bot_tmux "$2" capture-pane -p -t "=$3:" 2>/dev/null) || { printf 'box-v1\tunknown\n'; _lc_cleanup >/dev/null 2>&1; exit 3; }
if pane_is_held "$pane"; then printf 'box-v1\theld\n'; else printf 'box-v1\tempty\n'; fi
_lc_cleanup >/dev/null 2>&1
exit 0
'''

_BOX_STATES = frozenset({"empty", "held", "unknown"})


def read_box(package: PackageResources, destination: TransportDestination, *,
             timeout: float = 5, runner=None) -> str:
    """``empty`` when the recipient's box holds no text before a send, ``held`` when
    it does, ``unknown`` when it could not be read. The owner reads it just before
    its send and hands it to press_held_enter: a lone paste chip afterwards can then
    only be this send's, or a sender's racing between the read and the keystrokes."""
    if not isinstance(destination, TransportDestination):
        raise ValueError("a frozen TransportDestination is required")
    if isinstance(timeout, bool) or not 0 < timeout <= 30:
        raise ValueError("box read timeout must be finite and in (0, 30]")
    native = package.native / "lib-common.sh"
    if not native.is_absolute() or not native.is_file():
        return "unknown"
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
           "LC_ALL": "C", "CLAUDLOBBY_ROOT": str(destination.root),
           "FLEET_NAME": destination.fleet, "TMUX_TMPDIR": str(destination.tmux_tmpdir),
           "TMPDIR": str(destination.tmux_tmpdir), "PLANE_EMIT_DISABLED": "1"}
    command = ["/bin/bash", "--noprofile", "--norc", "-c", _BOX_SCRIPT, "message-box-read",
               str(native), destination.socket, destination.session]
    try:
        result = (runner or _run)(command, input=b"", env=env, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, _StartedFailure):
        return "unknown"
    found = re.fullmatch(rb"box-v1\t(empty|held|unknown)\n", result.stdout or b"")
    return found.group(1).decode() if found else "unknown"


def _repair_line(output: bytes | None) -> tuple[str, bool, str | None] | None:
    found = re.fullmatch(rb"repair-v1\t([a-z-]+)\t([01])\t(-|text|chip#[0-9]+)\n", output or b"")
    if not found or found.group(1).decode() not in _VERDICTS:
        return None
    match = found.group(3).decode()
    return found.group(1).decode(), found.group(2) == b"1", None if match == "-" else match
