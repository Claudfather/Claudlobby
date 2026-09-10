"""Tests for lib/session-pid.sh — the session-scoped self-identity door (#1525).

The defect this door replaces was not that the old expression errored. It was
that it returned a PLAUSIBLE WRONG NUMBER: `pgrep -f 'claude' | head -1` on a
shared-uid host matches every bot on the box and reduces to the earliest-started
match, which is structurally a tmux *server* rather than a Claude session. So
the properties worth pinning are (a) the answer is drawn from the CALLER'S OWN
ancestry, and (b) an unresolvable answer is loud rather than plausible.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

DOOR = Path(__file__).resolve().parent.parent / "lib" / "session-pid.sh"


def run(args, **kw):
    return subprocess.run(
        ["bash", str(DOOR), *args],
        capture_output=True, text=True, **kw
    )


def test_door_exists_and_is_executable():
    assert DOOR.is_file()
    assert os.access(DOOR, os.X_OK), "skills invoke this directly, so it must be 0755"


def test_parses_under_bash():
    r = subprocess.run(["bash", "-n", str(DOOR)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- the load-bearing property: ancestry, not namespace ----------------------

def _fake_tree(tmp_path, script):
    """Run `script` under a process genuinely named `claude`.

    Uses a COPIED shell binary so `comm` really reads `claude` -- a stub that
    merely claims the name would not exercise the comm-basename branch the door
    takes on Linux.

    The trailing `; true` is load-bearing. bash applies an exec optimisation to
    the LAST command of a `-c` string, replacing itself in place; the fake
    `claude` process then becomes the door process and there is no claude
    ancestor left to find. That is not a door defect -- the walk correctly
    continued past it -- but it silently destroys the fixture, so the fixture
    keeps the parent alive on purpose.
    """
    fake = tmp_path / "claude"
    fake.write_bytes(Path("/bin/bash").read_bytes())
    fake.chmod(0o755)
    return subprocess.run([str(fake), "-c", script + "; true"],
                          capture_output=True, text=True)


def test_resolves_to_an_ancestor_named_claude(tmp_path):
    r = _fake_tree(
        tmp_path,
        f'echo "ANCESTOR=$$"; echo "COMM=$(ps -o comm= -p $$)"; '
        f'echo "GOT=$(bash {DOOR} --pid)"',
    )
    assert r.returncode == 0, r.stderr
    out = dict(
        l.split("=", 1) for l in r.stdout.splitlines() if "=" in l
    )
    assert out["COMM"].strip() == "claude", (
        "fixture control failed: the ancestor is not actually named claude, "
        "so a pass here would prove nothing"
    )
    assert out["GOT"].strip() == out["ANCESTOR"].strip(), (
        f"door returned {out['GOT']!r}, caller ancestry was {out['ANCESTOR']!r}"
    )


def test_refuses_when_no_claude_ancestor(tmp_path):
    """No claude in the ancestry -> `unknown` on stdout and rc 3.

    Never a process-table fallback: the whole estate has claude processes
    running, so a fallback would return a confident number here.
    """
    r = run(["--from", "1"])
    assert r.returncode == 3, f"expected rc 3, got {r.returncode}: {r.stdout!r}"
    assert r.stdout.strip() == "unknown"
    assert "1525" in r.stderr


def test_refusal_is_not_a_number():
    """The defect was a plausible number. Assert the refusal cannot be mistaken
    for one -- this is what separates it from the old `head -1` behaviour."""
    for mode in ("--pid", "--etime", "--rss-mb"):
        r = run([mode, "--from", "1"])
        assert r.returncode == 3
        assert not r.stdout.strip().isdigit()
        assert r.stdout.strip() == "unknown"


def test_never_consults_the_process_table():
    """Source-level: the door must contain no namespace scan.

    A fallback scan would be a second copy of the predicate, consulted exactly
    when the two have diverged (the env-tiers.sh refusal, same reasoning).
    """
    body = DOOR.read_text()
    code = "\n".join(
        l for l in body.splitlines() if not l.lstrip().startswith("#")
    )
    for banned in ("pgrep", "pkill", "pidof", "ps -e", "ps aux", "ps -ef"):
        assert banned not in code, f"{banned!r} appears in executable code"


def test_from_walks_the_given_ancestry(tmp_path):
    """--from is the seam that makes the walk testable without a real session."""
    r = _fake_tree(
        tmp_path,
        f'echo "ANCESTOR=$$"; echo "GOT=$(bash {DOOR} --from $$)"',
    )
    assert r.returncode == 0, r.stderr
    out = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert out["GOT"].strip() == out["ANCESTOR"].strip()


def test_rejects_a_non_pid_from():
    r = run(["--from", "not-a-pid"])
    assert r.returncode == 2
    assert "pid" in r.stderr.lower()


def test_summary_shape(tmp_path):
    r = _fake_tree(tmp_path, f'bash {DOOR} --summary')
    assert r.returncode == 0, r.stderr
    out = [l for l in r.stdout.splitlines() if l.startswith("PID ")][-1]
    assert out.startswith("PID ")
    assert " | up " in out
    assert out.endswith("MB") or "unknown" in out


# --- the skills must consume the door, not re-derive the answer -------------

SKILLS = ["selfcheck", "review-status", "status-personal", "eng-status"]


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_uses_the_door(skill):
    p = DOOR.parent.parent / "library" / "skills" / skill / "SKILL.md"
    assert "session-pid.sh" in p.read_text(), f"{skill} does not consume the door"


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_has_no_process_scan_in_executable_lines(skill):
    """Prose may DESCRIBE the old expression; no runnable fence may contain it.

    Asserted on fence contents only, because the fix deliberately documents the
    defect in surrounding prose -- a naive whole-file grep would fail on the
    explanation and pass on a regression that omitted it.
    """
    p = DOOR.parent.parent / "library" / "skills" / skill / "SKILL.md"
    in_fence = False
    offenders = []
    for i, line in enumerate(p.read_text().splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and ("pgrep" in line or "pkill" in line):
            offenders.append(f"{i}: {line.strip()}")
    assert not offenders, f"{skill} still scans the process table: {offenders}"


def test_the_prose_control_is_live():
    """Positive control for the test above: the explanation IS present, so the
    fence-scoping is doing real work rather than passing vacuously."""
    p = DOOR.parent.parent / "library" / "skills" / "selfcheck" / "SKILL.md"
    assert "pgrep -f 'claude' | head -1" in p.read_text(), (
        "explanatory prose missing -- the fence-scoped assertion would then "
        "pass for the wrong reason"
    )


def test_ambient_claude_processes_do_not_leak_in():
    """The estate has dozens of live claude processes. A namespace scan would
    find one; an ancestry walk from pid 1 must not.

    This is the assertion that separates the door from the expression it
    replaces, and it is only meaningful because the host really is populated --
    so the population is asserted rather than assumed.
    """
    ps = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True)
    live = [l for l in ps.stdout.splitlines() if l.strip() == "claude"]
    if not live:
        pytest.skip("no live claude processes on this host: control unavailable")
    r = run(["--from", "1"])
    assert r.returncode == 3
    assert r.stdout.strip() == "unknown"
