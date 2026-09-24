"""update-claude-code.sh, staged (#1768) — an unverified install never reaches a bot.

The in-place update writes over the one binary every bot launches, so a broken
install (npm exit 0 leaving a 500-byte stub, 2026-09-23) is an outage the moment
it lands. Armed with CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1, the job installs each
version into its OWN npm prefix under state/claude/versions, measures it THERE
(size above a floor, AND it ran and printed a version), and only then repoints
the one link the fleet launches, state/bin/claude, in a single rename, keeping
the previous version. No step needs sudo.

What these pin, each against the failure it exists for:
  - the stub arm: a stub is staged, verification fails, the link does NOT move,
    the operator is alerted — the property the whole issue asks for;
  - each half of the verification on its own, so neither can be dropped;
  - the previous version survives a swap (the rollback);
  - pruning never deletes a version a live process executes, with the positive
    control that it DOES delete the same version once nothing runs it;
  - a process started through the link keeps its version after the swap — the
    running-session property: the kernel resolved the link at exec;
  - switch OFF is the old job, byte for byte: nothing staged, nothing linked.

Hermetic the same way as test_update_claude_code_verify.py (constructed env, no
real sudo or npm reachable, alerts captured under a throwaway root), whose
fixtures this module reuses rather than copies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import _write_exec, constructed_env, plane_emit_env
from tests.test_maintenance_jobs import _captured, _signal_root
from tests.test_update_claude_code_verify import (
    SCRIPT,
    SUDO_STUB,
    _event_types,
    _path_without,
    broken_stub,
    healthy,
)

EXE_REL = Path("lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")

# The test floor. Production's is 10 MB (a real binary is ~226 MB, the stub
# 500 bytes); a test binary is a script, so the floor comes down to meet it and
# every fixture below is built relative to it.
FLOOR = 4096

# `view` answers the version to stage; `install --prefix P …@V` lays out an npm
# prefix the way npm does, with $NPM_BODIES/V (else /default) as the binary; a
# plain `install` (the in-place path) copies $NPM_STAGE over $NPM_STAGE_TARGET.
NPM_STUB = r"""#!/bin/bash
echo "npm-stub called: $*" >> "$NPM_CALLS"
if [ "$1" = view ]; then
    [ -n "${NPM_VIEW_RC:-}" ] && exit "$NPM_VIEW_RC"
    printf '%s\n' "$NPM_LATEST"
    exit 0
fi
prefix="" spec="" prev=""
for a in "$@"; do
    [ "$prev" = --prefix ] && prefix="$a"
    case "$a" in @anthropic-ai/claude-code@*) spec="$a" ;; esac
    prev="$a"
done
if [ -n "$prefix" ]; then
    v="${spec##*@}"
    d="$prefix/lib/node_modules/@anthropic-ai/claude-code/bin"
    mkdir -p "$d" "$prefix/bin"
    src="$NPM_BODIES/$v"; [ -f "$src" ] || src="$NPM_BODIES/default"
    if [ -f "$src" ]; then cp "$src" "$d/claude.exe"; chmod 755 "$d/claude.exe"; fi
    ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$prefix/bin/claude"
    # Only once the prefix holds files: a racing run then has something to lose.
    [ -n "${NPM_STARTED:-}" ] && : > "$NPM_STARTED"
elif [ -n "${NPM_STAGE:-}" ]; then
    cp "$NPM_STAGE" "$NPM_STAGE_TARGET"
fi
if [ -n "${NPM_SLEEP:-}" ]; then sleep "$NPM_SLEEP"; fi
exit "${NPM_RC:-0}"
"""


def padded(script: str) -> str:
    """The script, then an exit, then a comment past the floor: bash never
    reads that far, so behaviour is the script's and only the SIZE changes."""
    return script.rstrip("\n") + "\nexit $?\n# " + "x" * (FLOOR + 100) + "\n"


def healthy_big(version: str) -> str:
    return padded(healthy(version))


class StagedHost:
    """A throwaway host running the job armed (or not): a system claude on the
    fleet PATH, an npm that stages prefixes, the root's own plane and alert
    channel."""

    def __init__(self, tmp_path, system: str | None = None):
        self.tmp = tmp_path
        self.root = _signal_root(tmp_path)
        self.home = tmp_path / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        _write_exec(self.home / ".local" / "bin" / "npm", NPM_STUB)
        _write_exec(self.home / ".local" / "bin" / "sudo", SUDO_STUB)
        self.path = _path_without(tmp_path / "hostbin")
        self.sysdir = tmp_path / "sysbin"
        self.sysdir.mkdir()
        self.system = self.sysdir / "claude"
        if system is not None:
            _write_exec(self.system, system)
        self.bodies = tmp_path / "bodies"
        self.bodies.mkdir()
        self.calls = tmp_path / "npm.calls"
        self.sudo_calls = tmp_path / "sudo.calls"

    # --- layout ---------------------------------------------------------------
    @property
    def link(self) -> Path:
        return self.root / "state" / "bin" / "claude"

    @property
    def versions(self) -> Path:
        return self.root / "state" / "claude" / "versions"

    def exe(self, version: str) -> Path:
        return self.versions / version / EXE_REL

    def body(self, version: str, content: str | None = None, copy_of=None):
        """What `npm install …@version` will lay down as the binary."""
        dest = self.bodies / version
        if copy_of is not None:
            shutil.copy(copy_of, dest)
            os.chmod(dest, 0o755)
        else:
            _write_exec(dest, content)

    # --- running --------------------------------------------------------------
    def env(self, latest="2.1.281", armed=True, **extra):
        base = dict(
            PATH=self.path,
            HOME=self.home,
            CLAUDLOBBY_ROOT=self.root,
            TG_CAPTURE=self.tmp / "tg-capture",
            NPM_CALLS=self.calls,
            SUDO_CALLS=self.sudo_calls,
            NPM_BODIES=self.bodies,
            NPM_LATEST=latest,
            CLAUDE_UPDATE_FLEET_PATH=self.sysdir,
            CLAUDE_MIN_BINARY_BYTES=str(FLOOR),
            FLEET_EVENT_EMIT_TIMEOUT_S="120",
            **plane_emit_env(),
        )
        if armed:
            base["CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"] = "1"
        base.update(extra)
        return constructed_env(**base)

    def run(self, latest="2.1.281", armed=True, **extra):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=self.env(latest, armed, **extra),
            capture_output=True,
            text=True,
            timeout=300,
        )

    def log(self) -> str:
        p = self.root / "state" / "claude-update.log"
        return p.read_text() if p.exists() else ""

    def npm_calls(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    def events(self) -> list[str]:
        from tests.conftest import read_fleet_events

        return _event_types(read_fleet_events(self.root))

    def sent(self) -> list[str]:
        return _captured(self.tmp).splitlines()

    def staging_left(self) -> list[str]:
        if not self.versions.exists():
            return []
        return [
            p.name for p in self.versions.iterdir() if p.name.startswith(".staging")
        ]


def _target(link: Path) -> str:
    return os.readlink(link)


def _link_to(h: StagedHost, version: str) -> None:
    """Arm a host already on `version` through the job itself, never by hand:
    the fixture then carries whatever the real first run leaves behind."""
    h.body(version, healthy_big(version))
    r = h.run(latest=version)
    assert r.returncode == 0, (r.returncode, r.stderr, h.log())
    assert _target(h.link) == str(h.exe(version)), h.log()


# --- the normal arm --------------------------------------------------------------


def test_normal_arm_stages_verifies_and_links_without_sudo(tmp_path):
    h = StagedHost(tmp_path, system=healthy("2.1.278"))
    h.body("2.1.281", healthy_big("2.1.281"))
    r = h.run(latest="2.1.281")

    assert r.returncode == 0, (r.stderr, h.log())
    assert h.link.is_symlink()
    assert _target(h.link) == str(h.exe("2.1.281"))
    # The link is what a bot execs: it must run the staged version.
    out = subprocess.run([str(h.link), "--version"], capture_output=True, text=True)
    assert out.stdout.startswith("2.1.281"), out
    # Staged into its own prefix, by the user: no sudo anywhere.
    assert f"--prefix {h.versions}/.staging-2.1.281-" in h.npm_calls(), h.npm_calls()
    assert "@anthropic-ai/claude-code@2.1.281" in h.npm_calls()
    assert not h.sudo_calls.exists(), h.sudo_calls.read_text()
    assert h.staging_left() == []
    assert "linked" in h.log(), h.log()
    assert "binary_update_failed" not in h.events()
    # The system binary is not the job's to touch any more.
    assert h.system.read_text() == healthy("2.1.278")


# --- the stub arm: fails closed ------------------------------------------------


def test_stub_arm_leaves_the_link_unmoved_and_alerts(tmp_path):
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    h.body("2.1.281", broken_stub("stderr"))  # the 09-23 shape: small AND failing
    r = h.run(latest="2.1.281")

    assert r.returncode == 1, (r.stderr, h.log())
    # THE property: the fleet still launches the version it launched before.
    assert _target(h.link) == str(h.exe("2.1.280"))
    assert h.exe("2.1.280").exists()
    # The stub never becomes a version, and its staging is gone.
    assert not (h.versions / "2.1.281").exists()
    assert h.staging_left() == []
    log = h.log()
    assert "UPDATE FAILED" in log, log
    assert "NOT moved" in log, log
    # ... and told why in the stub's own words, which name the cause.
    assert "claude native binary not installed" in log, log
    assert "binary_update_failed" in h.events()
    assert any("FLEET ALERT [binary_update_failed]" in s for s in h.sent()), h.sent()
    # The failure path must not ALSO trip install_error_trap: a script_error
    # row would be a second, misleading alert for a condition already raised.
    assert "script_error" not in h.events(), h.events()


def test_stub_arm_on_the_first_armed_run_creates_no_link(tmp_path):
    # Fleet still on the system binary: a failed first staging must not leave a
    # link behind, or bots would start launching something unverified.
    h = StagedHost(tmp_path, system=healthy("2.1.278"))
    h.body("2.1.281", broken_stub("stdout"))
    r = h.run(latest="2.1.281")

    assert r.returncode == 1, (r.stderr, h.log())
    assert not os.path.lexists(h.link)
    assert "binary_update_failed" in h.events()


# --- each half of the verification, on its own -----------------------------------


def test_a_binary_above_the_floor_that_cannot_run_is_not_linked(tmp_path):
    # The size half alone passes this one: it is big. Only running it tells.
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    h.body("2.1.281", padded(broken_stub("stderr")))
    r = h.run(latest="2.1.281")

    assert r.returncode == 1, h.log()
    assert _target(h.link) == str(h.exe("2.1.280"))
    assert "exited 1: Error: claude native binary not installed." in h.log()


def test_a_binary_under_the_floor_is_not_linked_even_when_it_runs(tmp_path):
    # The run half alone passes this one: it prints a version and exits 0.
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    h.body("2.1.281", healthy("2.1.281"))  # unpadded: under the floor
    r = h.run(latest="2.1.281")

    assert r.returncode == 1, h.log()
    assert _target(h.link) == str(h.exe("2.1.280"))
    assert "floor" in h.log(), h.log()


# --- the swap ---------------------------------------------------------------------


def test_the_swap_keeps_the_previous_version(tmp_path):
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    h.body("2.1.281", healthy_big("2.1.281"))
    r = h.run(latest="2.1.281")

    assert r.returncode == 0, h.log()
    assert _target(h.link) == str(h.exe("2.1.281"))
    # The rollback is still on disk, and recorded as the rollback.
    assert h.exe("2.1.280").exists()
    assert (h.versions / ".previous").read_text().strip() == str(h.exe("2.1.280"))
    assert not (h.link.parent / ("claude.new")).exists()
    assert [p.name for p in h.link.parent.iterdir()] == ["claude"]


def test_a_rerun_on_the_linked_version_is_a_no_op_that_keeps_the_rollback(tmp_path):
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    h.body("2.1.281", healthy_big("2.1.281"))
    assert h.run(latest="2.1.281").returncode == 0
    before = h.npm_calls().count("install")
    r = h.run(latest="2.1.281")

    assert r.returncode == 0, h.log()
    assert h.npm_calls().count("install") == before, "a no-op must not reinstall"
    assert "no-op" in h.log()
    # Re-linking the same version must not overwrite the rollback with itself.
    assert (h.versions / ".previous").read_text().strip() == str(h.exe("2.1.280"))
    assert h.exe("2.1.280").exists()


def test_a_relink_of_the_linked_version_never_records_itself_as_the_rollback(tmp_path):
    """Reachable when the linked binary fails its no-op check once and then passes
    the re-verify (a --version that flakes under load): the run re-links the SAME
    version, and must not overwrite the rollback with the version it replaces."""
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    h.body("2.1.281", healthy_big("2.1.281"))
    assert h.run(latest="2.1.281").returncode == 0, h.log()
    # Call 1 is the job's opening measurement, call 2 the no-op check: fail
    # exactly that one, so the run falls through to the re-verify and the swap.
    calls = tmp_path / "calls"
    h.exe("2.1.281").write_text(padded(
        f'#!/bin/bash\necho x >> "{calls}"\n'
        f'if [ "$(wc -l < "{calls}")" -eq 2 ]; then exit 1; fi\n'
        'echo "2.1.281 (Claude Code)"\n'))
    before = len(h.log().splitlines())
    r = h.run(latest="2.1.281")
    this_run = "\n".join(h.log().splitlines()[before:])

    assert r.returncode == 0, this_run
    # Precondition: this run really re-linked. A no-op would pass the checks
    # below without ever reaching the code under test.
    assert "UPDATE linked" in this_run and "no-op" not in this_run, this_run
    assert (h.versions / ".previous").read_text().strip() == str(h.exe("2.1.280"))
    assert h.exe("2.1.280").exists()


# --- the running session, and pruning ------------------------------------------


@pytest.mark.skipif(not Path("/proc/self/exe").exists(), reason="needs /proc")
def test_a_running_process_keeps_its_version_across_swaps_and_pruning(tmp_path):
    """A real executable (a copy of bash, which prints an X.Y.Z version and can
    run a loop without exec-ing away) stands in for claude.exe, so /proc/<pid>/exe
    names the versioned path exactly as a bot session's does."""
    h = StagedHost(tmp_path)
    for v in ("2.1.280", "2.1.281", "2.1.282"):
        h.body(v, copy_of="/bin/bash")
    assert h.run(latest="2.1.280").returncode == 0, h.log()
    a_exe = h.exe("2.1.280")

    # A "session" launched the way start-bot launches: through the link.
    proc = subprocess.Popen([str(h.link), "-c", "while :; do sleep 0.2; done"])
    try:
        time.sleep(0.5)
        assert os.readlink(f"/proc/{proc.pid}/exe") == os.path.realpath(a_exe)

        assert h.run(latest="2.1.281").returncode == 0, h.log()
        assert _target(h.link) == str(h.exe("2.1.281"))
        # The session did not move with the link, and what it re-executes (its
        # grep/rg run CLAUDE_CODE_EXECPATH, the resolved path) still runs.
        assert proc.poll() is None
        assert os.readlink(f"/proc/{proc.pid}/exe") == os.path.realpath(a_exe)
        assert subprocess.run([str(a_exe), "--version"]).returncode == 0

        # One more version: 2.1.280 is now neither current nor previous, so
        # only the running process keeps it.
        assert h.run(latest="2.1.282").returncode == 0, h.log()
        assert a_exe.exists(), h.log()
        assert "running process" in h.log()
    finally:
        proc.kill()
        proc.wait()

    # The positive control: with nothing running it, the same prune removes it.
    # Without this, "it survived" could mean the prune never deletes anything.
    assert h.run(latest="2.1.282").returncode == 0, h.log()
    assert not (h.versions / "2.1.280").exists(), h.log()
    assert h.exe("2.1.281").exists() and h.exe("2.1.282").exists()


@pytest.mark.skipif(not Path("/proc/self/exe").exists(), reason="needs /proc")
def test_a_root_reached_through_a_symlink_still_protects_a_running_version(tmp_path):
    """The process table names an executable by its REAL path; the versions dir
    is spelled through CLAUDLOBBY_ROOT, which may run through a symlink. Compared
    raw, the two never match and a version a session runs would be deleted."""
    h = StagedHost(tmp_path)
    via = tmp_path / "via-link"
    via.symlink_to(h.root)
    for v in ("2.1.280", "2.1.281", "2.1.282"):
        h.body(v, copy_of="/bin/bash")
    assert h.run(latest="2.1.280", CLAUDLOBBY_ROOT=via).returncode == 0, h.log()
    proc = subprocess.Popen(
        [str(via / "state" / "bin" / "claude"), "-c", "while :; do sleep 0.2; done"])
    try:
        time.sleep(0.5)
        for v in ("2.1.281", "2.1.282"):
            assert h.run(latest=v, CLAUDLOBBY_ROOT=via).returncode == 0, h.log()
        # 2.1.280 is now neither current nor previous: only the process keeps it.
        assert h.exe("2.1.280").exists(), h.log()
    finally:
        proc.kill()
        proc.wait()


# --- the prune FAILS CLOSED (ravi's review of #1784) ---------------------------------
# Deleting a version a session still runs is the 09-23 break, so the prune may
# delete ONLY on a complete, successful read of everything it protects: the link,
# .previous, and the process table. Every arm that fails a read must keep
# everything and say so loudly, and each is paired with a control that differs in
# ONE fact and shows the same prune deleting: a keep with no delete beside it
# would also be what a prune that never deletes produces.


def _prunable(h: StagedHost, version: str) -> Path:
    """A staged version nothing links, records or runs: the one a live prune
    removes, and so the positive control's subject."""
    d = h.versions / version
    (d / EXE_REL).parent.mkdir(parents=True)
    _write_exec(d / EXE_REL, healthy_big(version))
    return d


@pytest.mark.parametrize("first,second", [
    ("real", "real"),  # the control: one spelling throughout
    ("real", "via-symlink"), ("via-symlink", "real"),
    ("real", "trailing-slash"), ("trailing-slash", "real"),
])
def test_a_respelled_root_never_costs_the_linked_or_previous_version(tmp_path, first, second):
    """Two runs link 2.1.280 then 2.1.281 through one spelling of the root, so the
    link AND .previous carry it; a no-op run then spells the root the other way.
    Nothing runs either version (the state right after a swap), so being
    RECOGNISED is their only protection, and recognition must resolve BOTH sides:
    each direction is an arm. The composer stamps the resolved root on the unit
    while lib-common keeps the logical pwd, so a hand run through a symlinked
    checkout spells it differently."""
    h = StagedHost(tmp_path)
    via = tmp_path / "via-link"
    via.symlink_to(h.root)
    spell = {"real": h.root, "via-symlink": via, "trailing-slash": f"{h.root}/"}
    for v in ("2.1.280", "2.1.281"):
        h.body(v, healthy_big(v))
        assert h.run(latest=v, CLAUDLOBBY_ROOT=spell[first]).returncode == 0, h.log()
    stale = _prunable(h, "2.1.270")
    r = h.run(latest="2.1.281", CLAUDLOBBY_ROOT=spell[second])

    assert r.returncode == 0, h.log()
    assert "no-op" in h.log(), h.log()
    assert h.exe("2.1.281").exists(), h.log()  # linked
    assert h.exe("2.1.280").exists(), h.log()  # previous
    out = subprocess.run([str(h.link), "--version"], capture_output=True, text=True)
    assert out.stdout.startswith("2.1.281"), (out, h.log())
    # The positive control: the same prune, in every arm, is live.
    assert not stale.exists(), h.log()


@pytest.mark.parametrize("stray", [None, "2.1.280 copy", "2.1.280 (copy)", "backup"])
def test_a_directory_the_job_never_staged_costs_nothing(tmp_path, stray):
    """ravi's third-round probe on #1784. The plan is read line by line in bash, so
    a name with a space split: `delete 2.1.280 copy` read as `delete 2.1.280`, and
    the LINKED version went. A Finder duplicate is `2.1.280 copy`, a GNOME one
    `2.1.280 (copy)`; `backup` is anything else a person parks there. The prune
    touches only names the job stages. POSITIVE CONTROL: a stale version, which
    must go in every arm, so a keep here cannot come from a prune that is dead."""
    h = StagedHost(tmp_path)
    h.body("2.1.280", healthy_big("2.1.280"))
    assert h.run(latest="2.1.280").returncode == 0, h.log()
    stale = _prunable(h, "2.1.270")
    if stray:
        (h.versions / stray).mkdir()
    assert h.run(latest="2.1.280").returncode == 0, h.log()  # the no-op path, then the prune

    assert h.exe("2.1.280").exists(), h.log()
    out = subprocess.run([str(h.link), "--version"], capture_output=True, text=True)
    assert out.stdout.startswith("2.1.280"), (out, h.log())
    if stray:
        assert (h.versions / stray).is_dir(), h.log()
    assert not stale.exists(), h.log()


FAIL_CLOSED = ["absent", "unlistable", "no-readable-process", "reader-crashes",
               "previous-unreadable"]


@pytest.mark.parametrize("arm", ["readable"] + FAIL_CLOSED)
def test_a_prune_that_cannot_read_its_inputs_deletes_nothing(tmp_path, arm):
    h = StagedHost(tmp_path)
    for v in ("2.1.281", "2.1.282"):
        h.body(v, healthy_big(v))
        assert h.run(latest=v).returncode == 0, h.log()
    # Linked 2.1.282, previous 2.1.281; one version a (fake) process runs, and
    # one nothing links, records or runs.
    running, stale = _prunable(h, "2.1.260"), _prunable(h, "2.1.270")
    fake = tmp_path / "fakeproc"
    (fake / "self").mkdir(parents=True)
    (fake / "self" / "exe").symlink_to("/bin/bash")
    if arm != "no-readable-process":
        (fake / "4242").mkdir()
        (fake / "4242" / "exe").symlink_to(os.path.realpath(running / EXE_REL))
    proc = tmp_path / "no-such-proc" if arm == "absent" else fake
    if arm == "reader-crashes":
        # Crash the planner (it reads its program from stdin: `python3 -`) and
        # nothing else. A python3 broken host-wide takes the plane down with it,
        # since the emit shim finalizes every batch through python3, and then
        # the log line is the only record left. Should the planner's invocation
        # ever change, this stops biting and the arm goes RED (the stale version
        # is deleted), never quietly green.
        _write_exec(h.home / ".local" / "bin" / "python3",
                    f'#!/bin/sh\n[ "$1" = - ] && exit 1\nexec {shutil.which("python3")} "$@"\n')
    previous = h.versions / ".previous"
    try:
        if arm == "unlistable":
            os.chmod(fake, 0o111)  # traversable, so self/exe resolves; not listable
        if arm == "previous-unreadable":
            os.chmod(previous, 0)
        r = h.run(latest="2.1.282", CLAUDE_UPDATE_PROC_DIR=proc)  # a no-op: prune only
    finally:
        os.chmod(fake, 0o755)
        os.chmod(previous, 0o644)

    assert r.returncode == 0, h.log()
    assert "no-op" in h.log(), h.log()
    for kept in (running, h.versions / "2.1.281", h.versions / "2.1.282"):
        assert kept.exists(), (arm, kept.name, h.log())
    if arm == "readable":
        # The positive control: this very prune deletes, and asks nobody.
        assert not stale.exists(), h.log()
        assert "binary_prune_skipped" not in h.events()
    else:
        assert stale.exists(), (arm, h.log())
        assert "nothing deleted" in h.log(), h.log()
        assert "binary_prune_skipped" in h.events(), h.events()
        # Refusing is not an error: no script_error beside the notice.
        assert "script_error" not in h.events(), h.events()


@pytest.mark.parametrize("link_dir", ["writable", "read-only"])
def test_a_failed_swap_leaves_the_rollback_pointer_as_it_was(tmp_path, link_dir):
    h = StagedHost(tmp_path)
    for v in ("2.1.280", "2.1.281", "2.1.282"):
        h.body(v, healthy_big(v))
    for v in ("2.1.280", "2.1.281"):
        assert h.run(latest=v).returncode == 0, h.log()
    previous = h.versions / ".previous"
    assert previous.read_text().strip() == str(h.exe("2.1.280"))
    try:
        if link_dir == "read-only":
            os.chmod(h.link.parent, 0o555)  # the swap cannot create its temporary
        r = h.run(latest="2.1.282")
    finally:
        os.chmod(h.link.parent, 0o755)

    if link_dir == "writable":
        # The control: a swap that lands moves the pointer to what it replaced.
        assert r.returncode == 0, h.log()
        assert previous.read_text().strip() == str(h.exe("2.1.281"))
    else:
        assert r.returncode == 1, h.log()
        assert _target(h.link) == str(h.exe("2.1.281")), h.log()
        assert previous.read_text().strip() == str(h.exe("2.1.280")), h.log()
        assert "binary_update_failed" in h.events()


# --- the switch --------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "yes", "true"])
def test_switch_off_is_the_in_place_job_unchanged(tmp_path, value):
    h = StagedHost(tmp_path, system=healthy("2.1.278"))
    h.body("default", healthy_big("2.1.281"))
    _write_exec(tmp_path / "inplace", healthy("2.1.281"))
    extra = {"NPM_STAGE": tmp_path / "inplace", "NPM_STAGE_TARGET": h.system}
    if value is not None:
        extra["CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"] = value
    r = h.run(armed=False, **extra)

    assert r.returncode == 0, (r.stderr, h.log())
    assert "--prefix" not in h.npm_calls()
    assert "install -g @anthropic-ai/claude-code@latest" in h.npm_calls()
    assert not (h.root / "state" / "claude").exists()
    assert not os.path.lexists(h.link)
    assert "version changed: 2.1.278 → 2.1.281" in h.log(), h.log()


def test_switch_off_with_a_staged_link_left_behind_installs_nothing_and_says_so(
    tmp_path,
):
    h = StagedHost(tmp_path, system=healthy("2.1.278"))
    _link_to(h, "2.1.280")
    calls_before = h.npm_calls()
    r = h.run(armed=False)

    assert r.returncode == 0, h.log()
    assert h.npm_calls() == calls_before, (
        "an in-place install would update a binary no bot runs"
    )
    assert _target(h.link) == str(h.exe("2.1.280"))
    # The job's own version check went through the fleet link, like start-bot.
    assert f"current: 2.1.280, target: {h.link}" in h.log(), h.log()
    assert "binary_update_skipped" in h.events()
    assert "UPDATE skipped" in h.log()


def test_a_pinned_claude_bin_is_not_overridden_by_the_staged_update(tmp_path):
    h = StagedHost(tmp_path)
    pinned = tmp_path / "pinned-claude"
    _write_exec(pinned, healthy("2.1.278"))
    r = h.run(CLAUDE_BIN=pinned)

    assert r.returncode == 0, h.log()
    assert "install" not in h.npm_calls()
    assert not os.path.lexists(h.link)
    assert "CLAUDE_BIN" in h.log()


# --- choosing the version ---------------------------------------------------------


def test_an_unresolvable_version_alerts_and_moves_nothing(tmp_path):
    h = StagedHost(tmp_path)
    _link_to(h, "2.1.280")
    r = h.run(NPM_VIEW_RC=1)

    assert r.returncode == 1, h.log()
    assert _target(h.link) == str(h.exe("2.1.280"))
    assert "binary_update_failed" in h.events()
    assert "could not resolve a version" in h.log()


def test_a_pinned_version_is_staged_instead_of_latest(tmp_path):
    h = StagedHost(tmp_path)
    h.body("2.1.279", healthy_big("2.1.279"))
    r = h.run(latest="2.1.281", CLAUDE_UPDATE_VERSION="2.1.279")

    assert r.returncode == 0, h.log()
    assert _target(h.link) == str(h.exe("2.1.279"))
    assert "view" not in h.npm_calls()


# --- two runs at once ---------------------------------------------------------------


def test_a_concurrent_run_waits_rather_than_deleting_the_first_runs_staging(tmp_path):
    """The start of a run clears staging a crashed run left behind; without the
    lock that would include the staging a LIVE run is installing into."""
    h = StagedHost(tmp_path)
    h.body("2.1.281", healthy_big("2.1.281"))
    started = tmp_path / "npm-started"
    first = subprocess.Popen(
        ["bash", str(SCRIPT)],
        env=h.env(NPM_SLEEP="4", NPM_STARTED=started),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not started.exists():
            assert first.poll() is None, first.communicate()
            assert time.monotonic() < deadline, "the first run never reached npm"
            time.sleep(0.1)
        second = h.run()
    finally:
        out, err = first.communicate(timeout=300)

    assert first.returncode == 0, (err, h.log())
    assert second.returncode == 0, (second.stderr, h.log())
    assert _target(h.link) == str(h.exe("2.1.281"))
    assert "binary_update_failed" not in h.events()


# --- the knob is registered ------------------------------------------------------------


def test_the_switch_is_registered_opt_in_on_the_update_job():
    from claudlobby import switches as sw

    row = sw.by_key("claude-staged-update")
    assert row.polarity == sw.OPT_IN
    # A lane INSIDE the enrolled claude-update job, never the job's own switch.
    assert row.scope == sw.DOOR
    assert row.env == "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"
    assert row.job == "claude-update"
    assert row.why_opt_in.strip()


# --- the flag reaches the timer ----------------------------------------------------
# A host timer starts with a closed environment, so an armed tier is only armed
# if the composer stamps it onto the unit (#1383's shape); and an unarmed host's
# unit must compose exactly as it did, or merging would change something.


def _compose_update_unit(tmp_path, monkeypatch, resolved: dict) -> str:
    import yaml

    import claudlobby.env_tiers as et
    from claudlobby.composer import compose_host_timers
    from claudlobby.env_tiers import Resolution
    from claudlobby.paths import Paths

    repo = Path(__file__).resolve().parent.parent
    job = yaml.safe_load((repo / "claudlobby" / "system.yaml").read_text())[
        "host"]["jobs"]["claude-update"]
    root = tmp_path / "r"
    (root / "claudlobby").mkdir(parents=True)
    (root / "claudlobby" / "system.yaml").write_text(
        yaml.safe_dump({"host": {"jobs": {"claude-update": job}}}))
    monkeypatch.setattr(et, "read_tiers", lambda paths, bot_name=None, fleet_name=None: [])
    monkeypatch.setattr(et, "cascade", lambda tiers: {
        k: Resolution(name=k, value=v, tier="host", path=None) for k, v in resolved.items()})
    out = compose_host_timers(Paths(root=root))
    return (out / "claudlobby-claude-update.service").read_text()


def test_an_armed_host_stamps_the_flag_onto_the_update_timer(tmp_path, monkeypatch):
    unit = _compose_update_unit(
        tmp_path, monkeypatch, {"CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED": "1"})
    assert "Environment=CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED=1" in unit, unit


def test_an_unarmed_host_composes_the_update_timer_without_it(tmp_path, monkeypatch):
    unit = _compose_update_unit(tmp_path, monkeypatch, {})
    assert "CLAUDLOBBY_STAGED" not in unit, unit
    assert "update-claude-code.sh" in unit, unit
