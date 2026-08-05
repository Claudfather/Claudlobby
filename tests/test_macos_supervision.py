"""macOS supervision coverage — run what can run here, gate only what cannot (#1012).

The gap this module marks, measured rather than assumed:

  * `lib/install-bot.sh` — the launchd bot enroller — was named by **no test at
    all**.
  * `lib/install_fleet_timer_launchd.sh` is named by exactly one assertion
    (`test_setup_system.py:103`), which checks that the STRING appears in
    another file's text. Nothing runs it.
  * Corrupting `compose_launchd_plist` outright fails only 3 of 2268 tests, all
    of them composition assertions — a `.plist` is just text, so those run
    anywhere and are not evidence about launchd.

On the platform side there is no macOS CI (ubuntu-only), no macOS canary host,
no automated macOS suite, and a manual baseline that is red at 34 failures.
`vera` hit exactly this being unable to canary #983.

GATE ONLY WHAT THE PLATFORM ACTUALLY WITHHOLDS. The first cut of this file
gated all three tests behind `platform.system() != "Darwin"`, and two of them
never needed it: a filesystem stat and a `plistlib` parse are both pure POSIX +
portable stdlib. Over-gating is conservative rather than wrong, which is
precisely why it would never have been revisited — and the cost is real, since
an over-gated test runs NOWHERE on ubuntu-only CI while looking accounted-for
in the skip list. Ungated, they run on every CI run forever, which shrinks the
blind spot instead of relabelling it.

The tell was already on the record: the plist body had been executed on Linux
by hand to check its API calls, passed, and was left gated anyway — and its
docstring described a `plutil -lint` platform verdict the body never invoked.
Caught by `vera` going assertion-by-assertion instead of accepting that the
tests skipped cleanly.

WHY THIS IS NOT THE DEFECT #1012 DESCRIBED, so the next reader does not
re-derive it: #1012 predicted platform-specific tests that run off-platform and
trivially pass. Mutation testing found none — the stub-the-binary + force-`_OS`
pattern gives those real teeth (breaking `bridge_state`'s macOS branch fails 2
of its 4 `ps-eww` cases; breaking `service_is_active`'s Darwin arm fails 2 of
its 3). What was missing is not a lying test but an ABSENT one, which is
quieter still, because a test that does not exist cannot even be counted.

WHAT THE force-`_OS` PATTERN DOES AND DOES NOT BUY, since a green `[ps-eww]`
invites the wrong conclusion: it exercises the macOS *branch logic* on Linux.
It cannot reproduce another kernel's *behaviour* — #973 was macOS `ps` column
formatting, which by construction never executed until someone ran it on a Mac
(see the native-host block in `tests/test_bridge_state.py`, PR #981).

DELIBERATELY NOT BUILT: cross-platform `ps`/`launchctl` fixtures captured from
both kernels. Better long-term, and it does not pass YAGNI at zero confirmed
carriers (Chris's ruling on #1012).
"""

from __future__ import annotations

import platform
import plistlib
import subprocess
from pathlib import Path

import pytest

from claudlobby.composer import compose_launchd_plist
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

# Reserved for what the platform genuinely withholds — a launchctl binary that
# does not exist off Darwin. The reason names `launchctl` specifically rather
# than "macOS", so a reader of a skipped run learns exactly which rung is
# missing instead of guessing at the whole platform. Paired with
# `addopts = "-rs"` (pyproject.toml) this prints on every run rather than
# collapsing into a bare `s`.
needs_launchctl = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason=(
        "needs macOS `launchctl` — the binary does not exist on Linux, so this "
        "rung cannot execute here; the estate has no macOS CI, canary host, or "
        "automated suite (#1012)"
    ),
)


# --- runs everywhere: pure POSIX / portable stdlib ---------------------------


def test_launchd_bot_enroller_is_executable():
    """The most basic thing no test asserted about the launchd path.

    A `stat` needs no launchd. If this enroller loses its exec bit, macOS
    supervision breaks — and that is detectable from Linux, so it is checked
    from Linux.
    """
    enroller = LIB / "install-bot.sh"
    assert enroller.is_file(), f"{enroller} missing"
    assert enroller.stat().st_mode & 0o111, f"{enroller} is not executable"


def test_composed_plist_is_structurally_valid(tmp_path):
    """The composed plist parses as a plist, and carries the right Label.

    `plistlib` is portable stdlib, so this is a real structural assertion on
    every platform — it catches malformed XML and a wrong Label wherever it
    runs. It is NOT the platform's acceptance verdict: only `plutil`/launchd on
    a Mac can say launchd would load it, and nothing here claims otherwise.
    """
    bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
    fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
    (tmp_path / "runtime" / "bots" / "w").mkdir(parents=True)

    text = compose_launchd_plist(bot, fleet, Paths(root=tmp_path, fleet_dir=tmp_path))
    parsed = plistlib.loads(text.encode())

    assert parsed["Label"] == "p.w"
    assert parsed["ProgramArguments"], "plist declares no ProgramArguments"


# --- genuinely gated: needs a binary Linux does not have ---------------------


@needs_launchctl
def test_launchctl_is_reachable():
    """If this fails on a Mac, every launchd rung below it is moot.

    This body has never run — there is no macOS host in the estate — and saying
    so is the point of the file rather than an apology for it.
    """
    rc = subprocess.run(["launchctl", "version"], capture_output=True).returncode
    assert rc == 0, "launchctl not reachable on a Darwin host"


def test_the_gate_names_the_binary_not_just_the_platform():
    """Guard the guard: a skip reason that only says "macOS" makes the reader
    guess which rung is missing, and a thin reason is barely louder than a pass
    — the failure mode #1012 is about. Runs everywhere."""
    reason = needs_launchctl.kwargs["reason"]
    assert "launchctl" in reason
    assert len(reason) > 40, "reason too thin to tell a reader what is missing"
