"""macOS supervision has no executable rung — say so out loud (#1012).

This module exists to be SKIPPED on Linux, visibly, naming the platform it
needs. That is its primary job.

The gap it marks, measured rather than assumed:

  * `lib/install-bot.sh` — the launchd bot enroller — is named by **no test at
    all**.
  * `lib/install_fleet_timer_launchd.sh` is named by exactly one assertion
    (`test_setup_system.py:103`), which checks that the STRING appears in
    another file's text. Nothing runs it.
  * Corrupting `compose_launchd_plist` outright fails only 3 of 2268 tests, all
    of them composition assertions — a `.plist` is just text, so those run
    anywhere and are not evidence about launchd.

So on the platform side there is: no macOS CI (ubuntu-only), no macOS canary
host, no automated macOS suite, and a manual baseline that is red at 34
failures. `vera` hit exactly this being unable to canary #983 — a launchd
change with nothing anywhere that could execute it.

WHY THIS IS NOT THE DEFECT #1012 DESCRIBED, stated plainly so the next reader
does not re-derive it: #1012 predicted platform-specific tests that run
off-platform and trivially pass. Mutation testing found none. This codebase
already solved that with the stub-the-binary + force-`_OS` pattern, and those
tests have real teeth — breaking the macOS branch of `bridge_state` fails 2 of
its 4 `ps-eww` cases, and breaking `service_is_active`'s Darwin arm fails 2 of
its 3 Darwin cases. What is actually missing is not a lying test; it is an
ABSENT one, which is quieter still, because a test that does not exist cannot
even be counted.

WHAT THE force-`_OS` PATTERN DOES AND DOES NOT BUY, since a green `[ps-eww]`
invites the wrong conclusion: it exercises the macOS *branch logic* on Linux.
It cannot reproduce another kernel's *behaviour* — #973 was macOS `ps` column
formatting, which by construction never executed until someone ran it on a Mac
(see the native-host block in `tests/test_bridge_state.py`, PR #981).

DELIBERATELY NOT BUILT: cross-platform `ps`/`launchctl` fixtures captured from
both kernels. That is the better long-term answer and it does not pass YAGNI at
zero confirmed carriers (Chris's ruling on #1012).

**The bodies below have never run.** There is no macOS host in the estate, so
their first execution will be on whoever runs this on a Mac. They are
deliberately minimal for that reason — the load-bearing output of this file
today is the skip line, not the assertions.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

# The reason string is the deliverable: it names the platform, so a reader of a
# green run learns that macOS supervision went unexercised rather than assuming
# it passed. Paired with `addopts = "-rs"` (pyproject.toml), this prints on
# every run instead of collapsing into a bare `s`.
needs_macos = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason=(
        "needs macOS: launchd/launchctl does not exist on Linux, so this rung "
        "cannot execute here — the estate has no macOS CI, canary host, or "
        "automated suite (#1012)"
    ),
)


@needs_macos
def test_launchd_bot_enroller_is_executable():
    """The most basic thing no test currently asserts about the launchd path."""
    enroller = LIB / "install-bot.sh"
    assert enroller.is_file(), f"{enroller} missing"
    assert enroller.stat().st_mode & 0o111, f"{enroller} is not executable"


@needs_macos
def test_launchctl_is_reachable():
    """If this fails on a Mac, every launchd rung below it is moot."""
    rc = subprocess.run(["launchctl", "version"], capture_output=True).returncode
    assert rc == 0, "launchctl not reachable on a Darwin host"


@needs_macos
def test_composed_plist_is_parseable_by_the_platform():
    """`plutil -lint` is the platform's own verdict on a plist.

    The three existing composition tests assert on the plist's TEXT, which is
    exactly as true on Linux. Only macOS can say whether launchd would accept
    it.
    """
    import plistlib

    from claudlobby.composer import compose_launchd_plist
    from claudlobby.config import BotConfig, FleetConfig
    from claudlobby.paths import Paths

    bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
    fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
    root = REPO
    (root / "runtime" / "bots" / "w").mkdir(parents=True, exist_ok=True)
    text = compose_launchd_plist(bot, fleet, Paths(root=root, fleet_dir=root))

    parsed = plistlib.loads(text.encode())
    assert parsed.get("Label") == "p.w"


def test_this_module_is_visibly_unrun_off_darwin():
    """Guard the guard: the skip must NAME the platform.

    A skip whose reason is empty or generic is barely louder than a pass, which
    is the whole failure mode #1012 is about. This runs everywhere.
    """
    reason = needs_macos.kwargs["reason"]
    assert "macOS" in reason
    assert "launchd" in reason or "launchctl" in reason
    assert len(reason) > 40, "reason too thin to tell a reader what is missing"
