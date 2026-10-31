"""The hints that send an operator to re-stage the host name a command that stages (#2575).

`claudlobby brief` and `claudlobby checkin` end two of their refusals with the command
that re-stages the host: the active selection is unavailable, or the fleet's bots or
manager changed after activation. `config plan` covers the whole host, so it refuses
--fleet ("host preparation rejects --fleet and --seed") and requires --release; a hint
that carried either mistake sent the reader to a command that stages nothing.

Each test takes the hint a real refusal raises, cuts out the `config plan` command it
tells the reader to run, and runs that command with this tree's CLI: the command must
get past argument checking, so the next thing it reports is the missing release.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure

REPO = Path(__file__).resolve().parent.parent
# The shape of a release id: "r-" and 64 hex digits. No such release exists in the test root.
RELEASE = "r-" + "0" * 64
# From the program name to the end of the `config plan` command: a hint closes it with a
# comma, a semicolon or a parenthesis.
_STAGING = re.compile(r"claudlobby\s[^,;(]*?\bconfig\s+plan\b[^,;(]*")


def _scope(root: Path):
    fleet = SimpleNamespace(name="fl", manager="mgr", bots={"mgr": None, "wrk": None})
    return SimpleNamespace(paths=SimpleNamespace(root=root, seed=False, package=None), fleet=fleet)


def _active(monkeypatch, root: Path, *, selection, bindings):
    """The refusals read three owners; each is imported inside the function that raises."""
    scope = _scope(root)
    monkeypatch.setattr("claudlobby.operation_context.resolve_operation_scope",
                        lambda **_: (scope, None))
    monkeypatch.setattr("claudlobby.activation_state.read_selection", lambda _root: selection)
    monkeypatch.setattr("claudlobby.activation_identity.read_selected_identity_bindings",
                        lambda *_, **__: bindings)


def _brief_hint(root: Path) -> str:
    from claudlobby.commands import brief_read

    args = argparse.Namespace(boot=False, json=False, usage_since=None, seed=False,
                              root=str(root), fleet="fl", bot=None)
    with pytest.raises(CommandFailure) as caught:
        brief_read.dispatch(args)
    return caught.value.error.hint


def _checkin_hint(root: Path) -> str:
    from claudlobby.commands import checkin

    args = argparse.Namespace(seed=False, root=str(root), fleet="fl")
    with pytest.raises(CommandFailure) as caught:
        checkin._scope(args)
    return caught.value.error.hint


_DRIVERS = {"brief": _brief_hint, "check-in": _checkin_hint}
# (selection, bindings): no active selection; a selection whose bindings name other bots.
_REFUSALS = {
    "selection-unavailable": (None, None),
    "bots-changed-after-activation": ({"release_id": RELEASE, "plan_id": "p"},
                                      {"manager": "mgr", "bots": {"mgr": 1, "other": 2}}),
}


def _run_staging(hint: str):
    """Run the `config plan` command the hint names, with this tree's CLI."""
    found = _STAGING.search(hint)
    assert found, f"the hint names no `config plan` command: {hint!r}"
    argv = shlex.split(found.group(0).replace("RELEASE_ID", RELEASE))
    assert argv[0] == "claudlobby", argv
    return subprocess.run([sys.executable, "-m", "claudlobby", *argv[1:]], capture_output=True,
                          text=True, timeout=180, cwd=str(REPO))


@pytest.mark.parametrize("refusal", sorted(_REFUSALS))
@pytest.mark.parametrize("door", sorted(_DRIVERS))
def test_the_staging_command_a_hint_names_gets_past_argument_checking(door, refusal, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    selection, bindings = _REFUSALS[refusal]
    _active(monkeypatch, root, selection=selection, bindings=bindings)
    hint = _DRIVERS[door](root)
    assert str(root) in hint, hint
    # RELEASE_ID is a placeholder, and the reader still has to activate the plan it stages.
    assert "host releases" in hint and "host activate" in hint, hint
    run = _run_staging(hint)
    output = run.stdout + run.stderr
    assert run.returncode != 2 and "rejects --fleet" not in output, (
        f"the {door} hint for {refusal} names a staging command the CLI refuses "
        f"(rc {run.returncode}): {hint!r}\n{output}")
    assert "release not found" in output, (
        f"the {door} hint for {refusal} names a staging command that never reached the release lookup: "
        f"{hint!r}\n{output}")


@pytest.mark.parametrize("mistake, refusal", [
    ("--fleet fl config plan --release RELEASE_ID", "rejects --fleet"),
    ("config plan", "command syntax"),    # argparse: the required --release is missing
], ids=["fleet", "no-release"])
def test_the_two_mistakes_the_old_hints_made_are_refused_by_the_check_that_runs_them(mistake, refusal, tmp_path):
    """The control: each mistake the hints made before #2575 stages nothing, and the check says so."""
    root = tmp_path / "root"
    root.mkdir()
    run = _run_staging(f"run claudlobby --root {shlex.quote(str(root))} {mistake}, then host activate")
    output = run.stdout + run.stderr
    assert run.returncode == 2 and refusal in output, (run.returncode, output)
