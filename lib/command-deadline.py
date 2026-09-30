#!/usr/bin/env python3
"""Private version-probe execution mechanism; no version predicate lives here.

stdout is ``exit <child-status>\n<captured bytes>`` at rc 0, or empty at
rc 124 (deadline). Mechanism failures use rc 125 and explain on stderr.
Ordinary child exit 124 is therefore distinct from our deadline. Each command
gets its own session/group; expiry kills that group once and reaps its leader.
Descendants that deliberately escape the group are outside this contract.
"""
from __future__ import annotations

import math
import os
import signal
import subprocess
import sys


class Interrupted(Exception):
    """Not InterruptedError: selectors retries that OS-level exception."""


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] not in {"stdout", "combined"}:
        print("command-deadline: expected seconds stdout|combined command [args]", file=sys.stderr)
        return 125
    try:
        seconds = float(sys.argv[1])
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
    except ValueError:
        print("command-deadline: deadline must be positive finite seconds", file=sys.stderr)
        return 125

    proc = None
    interrupted = False
    waiting = False

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True
        # During Popen, defer until its handle is assigned so cleanup cannot
        # lose the child between spawning and recording its owned group.
        if waiting and proc.returncode is None:
            raise Interrupted("interrupted")

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            proc = subprocess.Popen(
                sys.argv[3:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if sys.argv[2] == "combined" else subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            # Preserve ordinary shell execution failures as child outcomes.
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            sys.stdout.buffer.write(f"exit {code}\n{exc}".encode())
            return 0
        try:
            waiting = True
            if interrupted:
                raise Interrupted("interrupted")
            output, _ = proc.communicate(timeout=seconds)
        except (subprocess.TimeoutExpired, Interrupted) as cause:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            # Do not poll/reap before this one signal: the unreaped leader
            # keeps the group owned, including a child holding stdout open.
            # Permission errors stay loud; no shared-uid scan or signal retry.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"command-deadline: could not clean up owned group: {exc}", file=sys.stderr)
                return 125
            finally:
                # An escaped descendant might retain stdout. Do not wait for
                # that pipe again; escaped groups are outside this contract.
                proc.stdout.close()
            if isinstance(cause, Interrupted):
                print("command-deadline: interrupted", file=sys.stderr)
                return 125
            return 124
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    code = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
    sys.stdout.buffer.write(f"exit {code}\n".encode() + output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
