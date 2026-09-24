"""The Claude Code version, as the compositor sees it (#1772).

**This module owns no predicate.** It asks ``lib/claude-version.sh``, which is
``measure_claude_version`` in ``lib-common.sh``: the reader the update job gates
on, and the one every bash consumer of the version shares. It reports what that
reader says. A Python copy of "run it, check the exit, parse the first line"
would be cheaper and is the failure #1772 exists to end: the registry scan kept
one that recorded the literal ``"unavailable"``, and a stub's error text, as the
host's version.

**Could-not-measure is a verdict, never a value.** :class:`Measurement` carries
either a version or the reason there is none, never both, and nothing here
invents a stand-in string. Where the door itself cannot be reached, that is one
more reason the version could not be measured. It is not a failure of the
caller: the registry scan records it and composes on, and it never falls back
to reading the binary some other way, since that fallback would BE the second
copy of the reader.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .paths import Paths

#: Seconds to wait for the door: sourcing lib-common.sh plus one ``--version``
#: of a ~230 MB binary, which a loaded Raspberry Pi can take seconds over.
_DOOR_TIMEOUT_S = 30

_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

#: The door's own prefix on its refusal, stripped so the reason reads once.
_REFUSAL_PREFIX = "claude-version: could not measure: "


class Measurement(NamedTuple):
    """The door's verdict: a version, or the reason there is none."""

    version: str | None  #: X.Y.Z, only when the binary ran and printed one
    why: str | None  #: why it could not be measured; None when it was

    @property
    def measured(self) -> bool:
        return self.version is not None


def door_path(paths: Paths) -> Path:
    return paths.root / "lib" / "claude-version.sh"


def measure(paths: Paths, binary: str | None = None) -> Measurement:
    """Measure ``binary``, or with ``None`` the binary the fleet launches.

    The child's environment is built, not inherited, so a caller's own session
    cannot redirect the answer. ``CLAUDE_BIN`` passes through when set, because
    start-bot.sh launches it when set and the answer must be the runtime's.
    """
    script = door_path(paths)
    if not script.is_file():
        return Measurement(None, f"the version door {script} is not there")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "CLAUDLOBBY_ROOT": str(paths.root),
    }
    if os.environ.get("CLAUDE_BIN"):
        env["CLAUDE_BIN"] = os.environ["CLAUDE_BIN"]
    argv = ["bash", str(script)] + ([] if binary is None else [binary])
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=_DOOR_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Measurement(None, f"could not run the version door {script}: {exc}")
    out = proc.stdout.strip()
    if proc.returncode == 0 and _VERSION.fullmatch(out):
        return Measurement(out, None)
    err = proc.stderr.strip()
    if proc.returncode == 3 and err.startswith(_REFUSAL_PREFIX):
        return Measurement(None, err[len(_REFUSAL_PREFIX) :][:400])
    return Measurement(
        None,
        f"the version door {script} exited {proc.returncode}"
        f"{': ' + err[:400] if err else ''}"
        f"{' and printed ' + repr(out[:80]) if out else ''}",
    )
