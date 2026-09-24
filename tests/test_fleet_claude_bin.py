"""fleet_claude_bin — the one answer to "which claude does this fleet launch?" (#1768).

Before this, four scripts each spelled the answer themselves
(`${CLAUDE_BIN:-claude}`, or a bare `command -v claude`), so a staged binary
could only ever reach the ones somebody remembered. The resolver is:
CLAUDE_BIN when set, else the staged fleet link $CLAUDLOBBY_ROOT/state/bin/claude
when it resolves to something executable, else the bare name, left to PATH
exactly as before. The link exists only once an ARMED staged update has verified
a version, so with the switch off every consumer resolves what it always did.

Also here: atomic_link_swap, the one rename that moves the link, because the
swap's whole claim is that a bot starting at any instant finds a link.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env, _write_exec

LIB = Path(__file__).resolve().parent.parent / "lib"
LINK_REL = Path("state/bin/claude")


def _bash(snippet: str, **env) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
    full.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}/lib-common.sh"; {snippet}'],
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _resolve(**env) -> str:
    r = _bash("fleet_claude_bin", **env)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "state" / "bin").mkdir(parents=True)
    return r


def _staged(tmp_path, root, body="#!/bin/bash\necho staged\n") -> Path:
    exe = tmp_path / "versions" / "2.1.281" / "claude.exe"
    exe.parent.mkdir(parents=True)
    _write_exec(exe, body)
    (root / LINK_REL).symlink_to(exe)
    return exe


# --- resolution -------------------------------------------------------------------


def test_no_link_resolves_the_bare_name_as_before(root):
    assert _resolve(CLAUDLOBBY_ROOT=root) == "claude"


def test_no_root_resolves_the_bare_name(tmp_path):
    assert _resolve() == "claude"


def test_a_staged_link_is_what_the_fleet_launches(tmp_path, root):
    _staged(tmp_path, root)
    # The LINK, never its target: the kernel resolves it at exec, so the next
    # swap moves new starts without touching a session already running.
    assert _resolve(CLAUDLOBBY_ROOT=root) == str(root / LINK_REL)


def test_claude_bin_outranks_the_staged_link(tmp_path, root):
    _staged(tmp_path, root)
    assert (
        _resolve(CLAUDLOBBY_ROOT=root, CLAUDE_BIN="/opt/pinned/claude")
        == "/opt/pinned/claude"
    )


def test_an_empty_claude_bin_is_not_a_pin(tmp_path, root):
    _staged(tmp_path, root)
    assert _resolve(CLAUDLOBBY_ROOT=root, CLAUDE_BIN="") == str(root / LINK_REL)


def test_a_dangling_link_falls_back_rather_than_launching_nothing(tmp_path, root):
    exe = _staged(tmp_path, root)
    exe.unlink()
    assert _resolve(CLAUDLOBBY_ROOT=root) == "claude"


def test_a_link_to_a_non_executable_falls_back(tmp_path, root):
    exe = _staged(tmp_path, root)
    exe.chmod(0o644)
    assert _resolve(CLAUDLOBBY_ROOT=root) == "claude"


def test_a_regular_file_where_the_link_goes_is_not_the_fleet_binary(root):
    # Only the staged update writes there, and it only ever writes a link.
    _write_exec(root / LINK_REL, "#!/bin/bash\necho imposter\n")
    assert _resolve(CLAUDLOBBY_ROOT=root) == "claude"


# --- the swap -----------------------------------------------------------------------


def test_the_swap_creates_the_link_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "a"
    _write_exec(target, "#!/bin/bash\n")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    r = _bash(f'atomic_link_swap "{link}" "{target}"')
    assert r.returncode == 0, r.stderr
    assert os.readlink(link) == str(target)
    assert [p.name for p in link.parent.iterdir()] == ["claude"]


def test_the_swap_replaces_an_existing_link(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        _write_exec(p, "#!/bin/bash\n")
    link = tmp_path / "claude"
    link.symlink_to(a)
    r = _bash(f'atomic_link_swap "{link}" "{b}"')
    assert r.returncode == 0, r.stderr
    assert os.readlink(link) == str(b)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b", "claude"]


def test_the_swap_refuses_a_directory_and_leaves_it_alone(tmp_path):
    # A plain `mv tmp dir` would move the link INTO the directory and exit 0.
    target = tmp_path / "a"
    _write_exec(target, "#!/bin/bash\n")
    d = tmp_path / "claude"
    d.mkdir()
    (d / "keep").write_text("x")
    r = _bash(f'atomic_link_swap "{d}" "{target}"')
    # A deliberate refusal, not a 127 from a helper that does not exist.
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "atomic_link_swap" in r.stderr, r.stderr
    assert d.is_dir() and not d.is_symlink()
    assert [p.name for p in d.iterdir()] == ["keep"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "claude"]


def test_a_reader_never_finds_the_link_missing_across_swaps(tmp_path):
    """The swap's claim, measured: a bot starting at ANY instant finds a link.
    A reader polls in a tight loop while the link flips back and forth. A
    two-step `rm; ln -s` opens a window it lands in (red in 5 of 5 runs,
    measured); GNU `ln -sfn` renames a temporary itself (strace, coreutils 9.1),
    so that mutant is the directory test's to catch, not this one's. The pass
    direction cannot flake: rename(2) leaves no window to find."""
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        _write_exec(p, "#!/bin/bash\n")
    link, stop, misses = tmp_path / "claude", tmp_path / "stop", tmp_path / "misses"
    script = f"""
atomic_link_swap "{link}" "{a}"
( m=0; while [ ! -e "{stop}" ]; do [ -L "{link}" ] || m=$((m+1)); done; echo "$m" > "{misses}" ) &
reader=$!
for i in $(seq 1 40); do
    atomic_link_swap "{link}" "{b}" || exit 7
    atomic_link_swap "{link}" "{a}" || exit 7
done
: > "{stop}"
wait "$reader"
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert misses.read_text().strip() == "0"


# --- the consumers go through it --------------------------------------------------------

# Every script that launches or drives the FLEET's claude. start-bot.sh hands its
# resolved binary on to plugin_ensure, which takes it as an argument.
CONSUMERS = (
    "start-bot.sh",
    "reload-fleet.sh",
    "transcript-digest.sh",
    "update-claude-code.sh",
)

# Measurement harnesses choose their binary on purpose ("the real one is the
# point") and are not fleet consumers. A new entry here is a decision, not a fix.
OWN_CHOICE = {
    "boot-strand-sampler.sh",
    "freshbox-boot-gate.sh",
    "rehearse-permissions-ladder.sh",
    "send-size-probe.sh",
}


def test_the_fleet_consumers_resolve_through_the_one_helper():
    for name in CONSUMERS:
        text = (LIB / name).read_text()
        assert "fleet_claude_bin" in text, (
            f"{name} does not resolve through fleet_claude_bin"
        )


def test_no_other_script_hand_rolls_the_claude_fallback():
    """A ratchet, not a behavioural gate: the behaviour is pinned by the tests
    that run the consumers. This stops the NEXT script from spelling its own
    `${CLAUDE_BIN:-claude}` and silently launching a binary the fleet does not."""
    pattern = re.compile(r"\$\{CLAUDE_BIN:-claude\}")
    offenders = {
        p.name
        for p in LIB.iterdir()
        if p.is_file() and pattern.search(p.read_text(errors="replace"))
    }
    assert offenders <= OWN_CHOICE, sorted(offenders - OWN_CHOICE)


def test_reload_fleet_updates_plugins_through_the_staged_link(tmp_path):
    root = tmp_path / "root"
    libdir = root / "lib"
    libdir.mkdir(parents=True)
    for script in ("reload-fleet.sh", "lib-common.sh", "supervisor.sh"):
        _write_exec(libdir / script, (LIB / script).read_text())
    _write_exec(libdir / "check-npx-cache.sh", "#!/bin/bash\nexit 0\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("claude", "claudlobby"):
        _write_exec(
            bindir / tool, f'#!/bin/bash\necho "{tool} $*" >> "$CALL_LOG"\nexit 0\n'
        )
    staged = tmp_path / "versions" / "claude.exe"
    staged.parent.mkdir()
    _write_exec(staged, '#!/bin/bash\necho "staged $*" >> "$CALL_LOG"\nexit 0\n')
    (root / "state" / "bin").mkdir(parents=True)
    (root / LINK_REL).symlink_to(staged)
    bot = root / "runtime" / "bots" / "tbot"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('export FLEET_PLUGINS_REQUIRED="somepkg@Somewhere"\n')
    env = _scrubbed_env(
        CLAUDLOBBY_ROOT=str(root),
        CALL_LOG=str(tmp_path / "calls.log"),
        PATH=f"{bindir}:{os.environ['PATH']}",
        TMUX_TMPDIR=str(tmp_path / "no-tmux"),
    )
    env.pop("CLAUDE_BIN", None)
    r = subprocess.run(
        ["bash", str(libdir / "reload-fleet.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert "staged plugin update somepkg@Somewhere" in calls, calls
    assert not [c for c in calls if c.startswith("claude plugin")], calls
